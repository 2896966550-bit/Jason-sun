"""
Equis 智能体核心 —— 本地工具引擎
"""
import sys, os, re

# 路径修复：与 server.py 一致，注册 agent/ 与项目根目录
_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_AGENT_DIR)
for _p in (_AGENT_DIR, _PROJECT_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, AGENT_ENGINE
from tools.kb_tools import search_knowledge_base, list_kb_topics
from tools.measurement_tools import (
    estimate_uncertainty, calibrate_camera_guide,
    calculate_3d_coordinates, sensor_data_diagnose,
)
from tools.web_search import web_search

# ======== 规则引擎（本地 Fallback，无需 LLM） ========

def _rule_based_route(query: str, measurement_context=None) -> str:
    """Local fallback uses the same grounded calculator as graph and coordinator."""
    from tools.measurement_executor import execute_request
    grounded = execute_request(query,measurement_context)
    if grounded is not None:
        return grounded
    if any(word in query for word in ("标定步骤","标定指南","标定板")):
        return calibrate_camera_guide()
    if any(word in query for word in ("有哪些","什么内容","什么主题","列出")):
        return list_kb_topics()
    if any(word in query for word in ("搜索","查一下","网上搜","最新","新闻","联网")):
        return web_search(query)
    if any(word in query for word in ("缺陷","异常","表面质量","划痕","检测工件","外观")):
        import json
        from perception.measurement_context import detect_bound_anomaly
        return "```anomaly-card\n"+json.dumps(detect_bound_anomaly(measurement_context),ensure_ascii=False)+"\n```"
    if any(word in query.lower() for word in ("标准","规范","知识库","jjf","jjg","gb/t","不确定度","计量","原理","双目","标定","校准","gum")):
        return search_knowledge_base(query)
    return "请说明任务。数值评定需要带来源的实测值、器具精度/MPE和环境温度；参考比较还需要参考值。"


# ======== LLM 模式（有 API Key 时） ========

_agent = None

def _create_llm_agent():
    """创建 LangChain Agent (v1.3 API)"""
    from langchain_openai import ChatOpenAI
    from langchain.agents import create_agent
    from langchain.tools import tool
    from langchain_core.messages import HumanMessage

    @tool
    def kb_search(query: str) -> str:
        """查询计量标准知识库。用于回答计量学、不确定度、标定、传感器融合等专业问题。注意：不用于搜索最新新闻或实时信息。"""
        return search_knowledge_base(query)

    @tool
    def kb_topics() -> str:
        """列出知识库已有主题"""
        return list_kb_topics()

    @tool
    def read_sensors() -> str:
        """读取当前连接的 ESP32 传感器数据（DHT22 温湿度 + MPU6050 加速度/陀螺仪）。当用户问\"传感器数据\"、\"现在温度多少\"、\"环境状态\"、\"硬件读数\"时调用此工具。"""
        from tools.sensor_reader import get_sensor_data
        return get_sensor_data()

    @tool
    def calc_uncertainty(mv: float, sp: float, temp: float = 25, hum: float = 50) -> str:
        """计算不确定度: mv=实测值mm, sp=精度mm, temp=°C, hum=%RH"""
        return estimate_uncertainty(mv, sp, temp, hum)

    @tool
    def calib_guide() -> str:
        """获取双目标定操作步骤"""
        return calibrate_camera_guide()

    @tool
    def comp_3d(disp: float, bl: float = 150, fl: float = 500) -> str:
        """视差算深度: disp=视差px, bl=基线mm, fl=焦距px"""
        return calculate_3d_coordinates(disp, bl, fl)

    @tool
    def diag(t: float, h: float, ax: float, ay: float, az: float) -> str:
        """传感器诊断: t=°C, h=%RH, ax/ay/az=m/s²"""
        return sensor_data_diagnose(t, h, ax, ay, az)

    @tool
    def search_web(query: str) -> str:
        """联网搜索互联网。当用户明确说"搜索xxx"、问最新信息、新闻、实时数据、知识库没有的内容时，必须调用此工具而非kb_search。"""
        return web_search(query)

    @tool
    def plan_task(task: str) -> str:
        """智能测量任务规划。当用户说"帮我测xxx"、"我要测xxx"、"如何测量xxx"时调用，自动编排检测流程：查标准→定方案→执行→出结果。"""
        from tools.task_planner import plan_measurement
        return plan_measurement(task)

    @tool
    def anomaly_detect(class_name: str = "") -> str:
        """对工件表面进行缺陷/异常检测。当用户说"有无缺陷"、"表面质量"、"检测工件"、"这个零件合格吗"、"看看有没有划痕"时调用。使用AI视觉检测引擎。"""
        import json
        from perception.measurement_context import new_context,bind_percepts,detect_bound_anomaly,detection_failure
        from perception.bus import collect,to_state
        if not class_name or class_name == "default":
            return json.dumps(detection_failure("请明确指定已建库类别",status="needs_input",code="missing_class"),ensure_ascii=False)
        context = new_context({"class_id":class_name})
        context = bind_percepts(context,to_state(collect(want_camera=True)))
        return json.dumps(detect_bound_anomaly(context),ensure_ascii=False)

    llm = ChatOpenAI(
        model=LLM_MODEL,
        temperature=0.3,
        base_url=LLM_BASE_URL,
        api_key=LLM_API_KEY,
        max_tokens=2000,
        max_retries=2,
        request_timeout=60,
    )

    tools = [kb_search, kb_topics, read_sensors, anomaly_detect, calc_uncertainty, calib_guide, comp_3d, diag, search_web, plan_task]

    system_prompt = '\n'.join([
        '你是 Equis·AI测量智能体，像侦探一样思考。',
        '- 用户说"帮我测XX""我要测XX""如何测量XX" -> 优先用 plan_task 做智能规划',
        '- 用户说"缺陷""表面质量""划痕""检测工件""合格" -> 用 anomaly_detect 做视觉检测',
        '- 计量专业知识问题 -> 用 kb_search 查知识库',
        '- 用户说"搜索xxx""查一下xxx""最新xxx" -> 必须用 search_web 联网搜索',
        '- 计算类问题 -> 用对应的计算工具(calc_uncertainty/comp_3d/diag)',
        '- 能组合工具就组合：查标准+计算+诊断一起做，不要只用一个',
        '回复展示思考过程：1.查标准 → 2.计算方法 → 3.结论。纯文本格式，不用Markdown。',
    ])

    agent = create_agent(model=llm, tools=tools, system_prompt=system_prompt)
    return agent


# ======== 公共接口 ========

# P1-18：卡片解析收敛到 tools/cards.py 的单一实现
# 此处原先自带一份与 coordinator._llm_merge / graph.runner._strip_cards
# 重复的正则；三份实现改一处漏两处，且对 JSON 内含换行等情况处理不一致。
from tools.cards import strip_cards as _strip_cards


def _strip_cards_legacy(text: str) -> str:
    """（保留旧名以兼容可能的内部引用，实际委托给统一实现）"""
    return _strip_cards(text)


def chat(message: str, history: list = None, on_status=None, image_path: str = None,
         measurement_context=None) -> str:
    """智能体对话入口。Coordinator 优先 → 规则引擎兜底。
    on_status(agent_id, state) - 回调，用于前端展示多Agent思考过程
    image_path - 可选，用户上传图片的本地路径（多模态入链）
    """
    from perception.measurement_context import new_context
    try:
        fallback_context = new_context(measurement_context,query=message)
    except ValueError as exc:
        import json
        return json.dumps({"execution_status":"invalid_input","verdict":None,"error":str(exc)},ensure_ascii=False)
    from chat_history import save_message

    if not message and image_path:
        message = "[上传图片]"
    save_message("user", message)

    # ── 多智能体协同（主路径）──
    # EQUIS_ENGINE=graph  → LangGraph 1.0 状态图编排（P1 升级，默认）
    # EQUIS_ENGINE=legacy → 手写 Coordinator（回滚用）
    try:
        if AGENT_ENGINE == "graph":
            from graph.runner import run as graph_run
            from chat_history import get_recent_pairs
            recent = get_recent_pairs(3)
            result = graph_run(message, image_path=image_path, on_status=on_status, history=recent,
                               measurement_context=measurement_context)
        else:
            from coordinator import coordinator_chat
            from chat_history import get_recent_pairs
            recent = get_recent_pairs(3)
            result = coordinator_chat(message, history=recent,
                                      on_status=on_status, image_path=image_path,
                                      measurement_context=measurement_context)
        save_message("assistant", _strip_cards(result))
        return result
    except Exception as e:
        print(f"[Agent] {AGENT_ENGINE} 引擎异常({e})，降级规则引擎")

    # ── 规则引擎兜底（仅文本；图片场景给出明确提示）──
    if image_path:
        # The failed engine's processed image state is unavailable here. No raw-image retry.
        result = "图像分析未完成，请重新执行分析。"
    else:
        result = _rule_based_route(message,measurement_context=fallback_context)
    save_message("assistant", _strip_cards(result))
    return result
