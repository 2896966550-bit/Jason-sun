"""
Equis 多智能体协调器 — "三元"子Agent + Coordinator
规元(查标准) · 索元(搜新知) · 度元(算结果)

架构: Coordinator分析意图 → 并发调度三元 → 合并输出
所有结果通过 on_status 回调实时同步前端动画
"""
import sys, os, json, re, threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed

# 路径修复：与 server.py 一致，注册 agent/ 与项目根目录
_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_AGENT_DIR)
for _p in (_AGENT_DIR, _PROJECT_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

# ======== 子Agent定义 ========

# 引用纪律：区分"知识库检索所得"与"模型自身知识"，保证"可追溯"承诺真正落地。
# 背景：系统的核心卖点是全链路可追溯的合规判定，但 LLM 会不自觉补充库外知识
# （自造标准编号、像素数、阈值等），若不标注，评审追问依据时会自相矛盾。
_CITATION_RULE = (
    "【引用纪律】"
    "1) 只能陈述知识库检索结果中出现的内容，并标注规程编号；"
    "2) 严禁输出知识库未列出的具体数值参数——包括相机像素数/分辨率、可检出的最小缺陷尺寸、"
    "准确率与召回率阈值、设备型号参数、精度指标等；这类数字一律不得自行补充；"
    "3) 确需补充库外常识性内容时，必须在该句末尾显式标注『（非知识库内容，建议核实）』；"
    "4) 严禁编造标准编号与标准名称；"
    "5) 【版本冲突】若检索结果中同时出现同一标准的**不同版本**（如 JJG 34-2008 与 "
    "JJG 34-2022、JJG 99-2006 与 JJG 99-2022），必须明确指出存在新旧两版、列出各自的关键"
    "指标差异，并以**版本年代较新的现行版**为准作答；不得把两版指标混在一句里陈述。"
    "如不确定哪一版现行有效，应提示用户向标准发布机构核实。"
)

SUB_AGENTS = {
    "guiyuan": {
        "name": "规元",
        "icon": "📋",
        "emoji": "📚",
        "desc": "检索JJF/JJG/GB计量标准",
        "system_prompt": (
            "你是「规元」，Equis的计量标准检索专家。你只做一件事：查询知识库中的JJF/JJG/GB等国家计量检定规程和校准规范。"
            "根据用户问题，从知识库中找到最相关的规程，提炼关键内容。"
            "回复简洁专业，每条引用标注规程编号。纯文本格式。"
            + _CITATION_RULE
        ),
        "tools": ["kb_search"],
    },
    "suoyuan": {
        "name": "索元",
        "icon": "🌐",
        "emoji": "🔍",
        "desc": "联网搜索最新行业动态",
        "system_prompt": (
            "你是「索元」，Equis的联网搜索专家。你只做一件事：用互联网搜索获取最新信息、新闻、行业动态。"
            "严格按照用户问题搜索，返回结构化结果。标注信息来源。"
            "纯文本格式，每条信息附简短摘要。"
        ),
        "tools": ["search_web"],
    },
    "duyuan": {
        "name": "度元",
        "icon": "📐",
        "emoji": "⚙️",
        "desc": "图像理解、计算不确定度、三维坐标、诊断、缺陷检测",
        "system_prompt": (
            "你是「度元」，Equis的精密计算与检测专家。你只做一件事：执行测量相关的科学计算与视觉分析。"
            "包括：图像/工件视觉理解、测量不确定度评定、三维坐标换算、传感器数据诊断、工件表面缺陷/异常检测。"
            "输出计算结果时展示公式和步骤。纯文本格式。"
            # P1-15：度元也会引用规程（如"依据 GB/T 18779.1 判定"），
            # 且其输出同样被前端当作"检测依据"展示，故必须遵守同一套引用纪律
            + _CITATION_RULE
        ),
        "tools": ["image_inspect", "calc_uncertainty", "comp_3d", "diag", "read_sensors", "anomaly_detect"],
    },
}

# ======== 意图分析 ========

# 意图缓存。
# P5-49 修复：原实现是无限增长的 dict（`_intent_cache[cache_key] = result`，
# 无容量上限、无 TTL），key 是完整用户问题，长跑会持续吃内存。
# 现改为带上限的 LRU（OrderedDict），淘汰最久未用项。
_intent_cache = OrderedDict()
_intent_cache_max = int(os.getenv("EQUIS_INTENT_CACHE", "512"))
_intent_lock = threading.Lock()


def _intent_cache_put(key: str, value: dict) -> None:
    """写入意图缓存并按 LRU 淘汰，保证容量有界"""
    with _intent_lock:
        _intent_cache[key] = value
        _intent_cache.move_to_end(key)
        while len(_intent_cache) > _intent_cache_max:
            _intent_cache.popitem(last=False)


def _intent_cache_get(key: str):
    """读取意图缓存（命中即置为最近使用）"""
    with _intent_lock:
        if key in _intent_cache:
            _intent_cache.move_to_end(key)
            return _intent_cache[key]
    return None

def analyze_intent(message: str, has_image: bool = False) -> dict:
    """用 LLM 判断需要哪些子Agent。

    P1-12 改造：本函数原先自行拼 prompt 调一次 LLM，与 ``rewrite_query``、
    ``_extract_params`` 构成每轮三次独立请求。现统一收敛到
    ``agent_router.route()`` —— **一次调用**同时得到意图、指代消解后的
    检索问题、需要调用的工具（白名单校验）、以及"是否应直接作答"。
    LLM 不可用时同样降级到关键词路由。

    返回形状保持不变（``{"agents": [...], "query": ...}``），并额外附带
    ``rewritten_query`` / ``tool_calls`` / ``direct_answer`` 等字段，
    供 graph 节点与调用方按需取用，因此老调用点无需改动。
    """
    cache_key = f"{message}|img{int(has_image)}"
    _hit = _intent_cache_get(cache_key)
    if _hit is not None:
        return _hit

    dec = {"intent": [], "rewritten_query": "", "tool_calls": [],
           "direct_answer": False, "need_camera": False, "need_sensor": False}
    try:
        from agent_router import route as _route
        dec = _route(message, history=None, has_image=has_image)
    except Exception as e:
        print(f"[Coordinator] 结构化路由失败({type(e).__name__}: {str(e)[:100]})，降级关键词")
        try:
            from agent_router import keyword_route
            dec = keyword_route(message, has_image)
        except Exception:
            dec = {"intent": [], "tool_calls": [], "direct_answer": False,
                   "need_camera": False, "need_sensor": False, "rewritten_query": ""}

    # intent/tool 名 -> 三元子 Agent 名
    names = {c.get("name") for c in (dec.get("tool_calls") or [])}
    intents = set(dec.get("intent") or [])
    agents = []
    if ({"kb_search"} & names) or "kb" in intents:
        agents.append("guiyuan")
    if ({"web_search"} & names) or "web" in intents:
        agents.append("suoyuan")
    if ({"anomaly_detect", "capture_camera", "calc_uncertainty",
          "compliance_judge", "comp_3d", "calib_guide"} & names) or ({"compute", "vision"} & intents):
        agents.append("duyuan")
    if not agents:
        agents = ["guiyuan"]

    # 概念类问题：明确不需要任何工具时，也保留规元兜底（它只查库、代价可控）
    result = {
        "agents": agents,
        "query": message,
        "rewritten_query": dec.get("rewritten_query") or "",
        "tool_calls": dec.get("tool_calls") or [],
        "direct_answer": bool(dec.get("direct_answer")),
        "need_camera": bool(dec.get("need_camera")),
        "need_sensor": bool(dec.get("need_sensor")),
        "missing_params": dec.get("missing_params") or [],
        "_router_source": dec.get("_source", ""),
    }
    _intent_cache_put(cache_key, result)
    return result


def _keyword_intent(message: str) -> list:
    """关键词匹配（LLM 不可用时的降级方案）"""
    msg = message.lower()
    agents = []

    kb_words = ["jjf", "jjg", "gbt", "gb/t", "iso", "检定", "校准",
                "计量标准", "规程", "规范", "msa"]
    if any(w in msg for w in kb_words):
        agents.append("guiyuan")

    web_words = ["搜索", "搜一下", "联网", "最新", "新闻", "202"]
    if any(w in msg for w in web_words):
        agents.append("suoyuan")

    calc_words = ["计算", "测量", "传感器", "诊断", "三维", "标定",
                  "精度", "测", "直径", "温度", "压力", "轴", "读数", "硬件",
                  "缺陷", "异常", "表面", "划痕", "裂纹", "检测", "合格", "外观"]
    if any(w in msg for w in calc_words):
        agents.append("duyuan")

    if not agents:
        agents.append("guiyuan")
    return agents


# ======== 子Agent执行 ========

_llm_client = None
_llm_client_lock = threading.Lock()


def _get_llm_client():
    """OpenAI 兼容客户端单例（P1-16）

    原实现每次 _llm_chat 都 `OpenAI(base_url=..., api_key=...)`，
    而每轮对话会发起 3-6 次调用 —— 等于每轮重建 3-6 次连接池与 TLS 握手。
    实测这是 supervisor/retrieval/merge 各环节的固定开销之一。
    OpenAI SDK 的 client 本身线程安全，可安全复用。
    """
    global _llm_client
    if _llm_client is not None:
        return _llm_client
    with _llm_client_lock:
        if _llm_client is None:
            # 数据不出境 Gate（P4-42 接线）：OpenAI 兼容 SDK 走 httpx 自己发请求，
            # 不经过 guard_request，因此必须在**客户端创建处**断言一次目标域名，
            # 并计入出站台账。否则 LLM 调用完全绕过 Gate，
            # 合规报告里的 outbound 台账会漏掉最高频的那类出站。
            try:
                from compliance.gate import assert_host_allowed, record_outbound
                assert_host_allowed(LLM_BASE_URL)
                record_outbound(LLM_BASE_URL)
            except Exception as e:
                # 越界配置必须硬失败：宁可启动时报错，也不要静默把数据发出去
                raise RuntimeError(
                    "数据不出境 Gate 拦截：LLM_BASE_URL=%r 不合法（%s）"
                    % (LLM_BASE_URL, e))
            from openai import OpenAI
            # C2：逐请求出站计量。
            # 原先只在**客户端创建时** record_outbound 一次，
            # 之后所有 LLM 调用都不再记账 —— 出站台账里这一类最高频的
            # 出站永远只有 1 条，无法回答"这一轮到底对外发了几次请求"。
            # 用 httpx 的 request 事件钩子，在真正发出请求时逐次记录。
            # 钩子内部吞掉所有异常：记账是旁路，绝不能影响主链路。
            def _on_request(request):
                try:
                    from compliance.gate import record_outbound
                    record_outbound(str(request.url))
                except Exception:
                    pass

            try:
                import httpx
                _http_client = httpx.Client(
                    event_hooks={"request": [_on_request]},
                    timeout=60.0,
                )
            except Exception:
                _http_client = None

            _llm_client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY,
                                 timeout=60, max_retries=2,
                                 http_client=_http_client) if _http_client else \
                OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY,
                       timeout=60, max_retries=2)
    return _llm_client


def _llm_chat(system_prompt: str, user_message: str, max_tokens: int = 600,
              fast: bool = True, model: str = None, timeout: int = 60,
              temperature: float = 0.3) -> str:
    """轻量LLM单次调用(非Agent模式)，只推理不调工具。

    fast=True 时关闭思考模式（enable_thinking=False）。意图分类、参数提取、
    结果融合均属结构化任务，关思考可显著提速且不损质量。

    P1-19：新增 model 参数。实测（百炼兼容模式，enable_thinking=False）：
        qwen-flash      279 ms
        qwen-turbo      301 ms
        qwen-plus       750 ms
        qwen3.8-max    1103 ms   <- 原先所有调用都用这个主力大模型
    参数抽取（_extract_params，纯 JSON 抽取）属机械任务，用 qwen-flash 即可，
    实测可把该步从约 1.8s 压到 0.3s 量级。
    注意：面向用户作答的调用（规元/索元/度元正文、最终融合）仍用主力模型，
    保证回答质量不受影响。
    """
    client = _get_llm_client()
    kwargs = {}
    if fast:
        kwargs["extra_body"] = {"enable_thinking": False}
    resp = client.chat.completions.create(
        model=(model or LLM_MODEL),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        **kwargs,
    )
    # P5-51：把本次调用的 token / 成本记入 trace（此前生产代码从不记账，
    # tokens_total 与 cost_cny 恒为 0，用量维度形同虚设）。
    # 节点名用实际模型名（区分主控 qwen3.8-max 与轻量 qwen-flash），
    # 便于在 /api/trace 里定位"哪类调用最贵"。
    try:
        from observability.trace import record_usage_from_resp
        record_usage_from_resp(resp, node=(model or LLM_MODEL))
    except Exception:
        pass
    return resp.choices[0].message.content.strip()


# 机械性任务（参数抽取/格式化）用的轻量模型，可用环境变量覆盖
FAST_MODEL = os.getenv("EQUIS_FAST_MODEL", "qwen-flash")

# 最终融合用模型（维护 2026-10-17）
# 原实现固定用 LLM_MODEL（qwen3.8-max）。实测融合这一步在图片类请求里
# 稳定占 10–13.8s：它要生成 400–800 token 的自然语言，而 max 模型生成速度
# 远慢于轻量模型。融合本质是"改写/组织已有要点"（事实都来自各子 Agent），
# 对模型推理深度要求不高。
#
# 同提示词实测（max_tokens=800，enable_thinking=False）：
#     qwen3.8-max   8.01s   428 字   关键信息 3/3
#     qwen-plus     5.89s   367 字   关键信息 3/3
#     qwen-flash    1.84s   320 字   关键信息 3/3   <- 4.3x
# 默认改用 qwen-flash；同时加"信息保留守门"（见 _llm_merge）：
# 融合结果若丢失原文中的标准编号，就退回确定性拼装，避免降质。
# 如需回到主力模型：EQUIS_MERGE_MODEL=qwen3.8-max
MERGE_MODEL = os.getenv("EQUIS_MERGE_MODEL", "").strip() or "qwen-flash"
# 融合输出上限（原 800，实测融合答案多在 320–430 字，600 足够且更快）
MERGE_MAX_TOKENS = int(os.getenv("EQUIS_MERGE_MAX_TOKENS", "600"))
# 融合超时（超时即退回确定性拼装，不让一次改写拖垮整轮）
MERGE_TIMEOUT = int(os.getenv("EQUIS_MERGE_TIMEOUT", "20"))
# 确定性拼装阈值：合并正文不超过该字数时直接拼装，不调 LLM（省一次往返）
MERGE_FAST_CHARS = int(os.getenv("EQUIS_MERGE_FAST_CHARS", "900"))


def rewrite_query(query: str, history: list) -> str:
    """指代消解：结合对话历史把"它/该/这个"改写为可独立检索的完整问题。

    这是多轮 RAG 的关键一步：kb 向量检索对"它适用于什么范围"这类
    含代词的查询必然召回失败（"它"无语义），必须先改写。
    """
    if not history:
        return query
    hist_txt = "\n".join(
        f"{'用户' if h.get('role') == 'user' else 'Equis'}: {str(h.get('content', ''))[:150]}"
        for h in history[-4:]
    )
    prompt = (
        f"对话历史：\n{hist_txt}\n\n当前问题：{query}\n\n"
        "任务：把当前问题改写为不依赖上下文、可独立检索的完整问题"
        "（把'它/该/这个'等指代替换为具体对象名称，保留原意）。\n"
        "只输出改写后的问题本身，不要解释。若问题已独立完整，原样输出。"
    )
    try:
        # P5-51：复用 _get_llm_client() 单例。原实现此处每次 `OpenAI(...)` 新建
        # 客户端，等于每轮多轮对话都重建一次连接池与 TLS 握手；
        # 顺带把 token / 成本记入 trace。
        client = _get_llm_client()
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0, max_tokens=120, timeout=30,
            extra_body={"enable_thinking": False},
        )
        try:
            from observability.trace import record_usage_from_resp
            record_usage_from_resp(resp, node=LLM_MODEL + "/rewrite")
        except Exception:
            pass
        out = (resp.choices[0].message.content or "").strip().strip('"').strip('「」"')
        if out and 0 < len(out) <= 200:
            return out
    except Exception as e:
        print(f"[Rewrite] 查询改写失败: {e}")
    return query


def _run_guiyuan(query: str, on_status=None, context: str = "", image_path: str = None,
                 search_query: str = None) -> str:
    """规元：查知识库 → LLM总结

    search_query: 可选，独立检索词（多轮指代消解后的问题）；缺省用 query
    """
    if on_status: on_status("guiyuan", "active")
    from tools.kb_tools import search_knowledge_base

    sq = (search_query or query).strip()
    kb_result = search_knowledge_base(sq, top_k=3)
    # P5-44：知识库命中留痕 —— 这是"引用可追溯"最直接的证据链
    try:
        from compliance.gate import audit_kb_hit
        _srcs = re.findall(r"来源[:：]\s*([^\n(（]+)", kb_result)
        audit_kb_hit(sq, _srcs, level=("miss" if "未找到" in kb_result else "hit"))
    except Exception:
        pass
    if "未找到" in kb_result or "【错误】" in kb_result:
        if on_status: on_status("guiyuan", "done")
        return f"【规元】知识库未找到相关规程"

    try:
        ctx = f"\n\n对话历史：\n{context}" if context else ""
        prompt = (
            f"用户问题：{query}{ctx}\n\n知识库检索结果（检索词：{sq}）：\n{kb_result[:1500]}\n\n"
            "请根据检索结果简明回答用户问题，引用规程编号。\n" + _CITATION_RULE
        )
        answer = _llm_chat(SUB_AGENTS["guiyuan"]["system_prompt"], prompt)
        if on_status: on_status("guiyuan", "done")
        return f"【规元 · 知识库检索】\n{answer}"
    except Exception as e:
        if on_status: on_status("guiyuan", "done")
        return kb_result[:600]


def _run_suoyuan(query: str, on_status=None, context: str = "", image_path: str = None) -> str:
    """索元：联网搜索 → LLM总结

    P5-51：把 on_status 透传给 web_search，使其在"开始检索/命中缓存/
    检索完成/降级"各阶段上报进度。实测该分支单次最长 30.2s（占整轮 95%），
    此前前端只看到"卡住"。
    """
    if on_status: on_status("suoyuan", "active")
    from tools.web_search import web_search

    def _search_status(msg: str):
        """把搜索阶段反馈映射到前端状态（同一 agent 的 running 提示）"""
        if on_status:
            try:
                on_status("suoyuan", f"active|{msg}")
            except Exception:
                pass

    web_result = web_search(query, on_status=_search_status)

    # P5-52：相关性守门。web_search 判定为"自我介绍/寒暄"时返回 _REASK
    # （不含"【联网搜索】"前缀），此时**直接透传拒识文本**，
    # 不能再送给 LLM 总结——否则"请补充具体关键词"这段话
    # 会被 LLM 二次加工成一段像模像样的回答，拒识就失效了。
    from tools.web_search import _REASK as _SEARCH_REASK
    if _SEARCH_REASK in web_result:
        if on_status: on_status("suoyuan", "done")
        return web_result

    if "【联网搜索】" not in web_result:
        if on_status: on_status("suoyuan", "done")
        return f"【索元】联网搜索未获取到有效结果"

    # 提取纯文本
    clean = web_result.replace("【联网搜索】", "").strip()
    try:
        ctx = f"\n\n对话历史：\n{context}" if context else ""
        prompt = f"用户问题：{query}{ctx}\n\n联网搜索结果：\n{clean[:1500]}\n\n请整理搜索结果，以简洁方式回答。"
        answer = _llm_chat(SUB_AGENTS["suoyuan"]["system_prompt"], prompt)
        if on_status: on_status("suoyuan", "done")
        return f"【索元 · 联网搜索】\n{answer}"
    except Exception as e:
        if on_status: on_status("suoyuan", "done")
        return clean[:600]


def _run_duyuan_image(query: str, image_path: str, on_status=None, context: str = "") -> str:
    """图片输入分支：结构化视觉分析 → 依据识别结果自动检索计量标准（多模态闭环）

    若协调器已做预理解（context 携带"图像理解结果：{...}"），直接复用，
    避免重复调用视觉模型（省一次 ~2s 的调用）。
    """
    import os as _os
    try:
        data = None
        if context:
            idx = context.find("图像理解结果")
            if idx >= 0:
                brace = context.find("{", idx)
                if brace >= 0:
                    try:
                        data, _ = json.JSONDecoder().raw_decode(context[brace:])
                    except Exception:
                        data = None

        if data is None:
            from tools.multimodal_tools import inspect_image
            if on_status: on_status("du_visual", "active")
            raw = inspect_image(image_path, context=context)
            if on_status: on_status("du_visual", "done")
            try:
                data = json.loads(raw)
            except Exception:
                data = {"raw": raw[:500]}

        if data.get("error"):
            if on_status: on_status("duyuan", "done")
            return f"【度元 · 图像理解】分析失败：{data['error']}"

        obj = str(data.get("object") or "未识别")
        material = str(data.get("material_hint") or "unknown")
        surface = str(data.get("surface_state") or "")
        defects = data.get("visible_defects") or []
        features = data.get("measurable_features") or []
        suggestion = str(data.get("inspection_suggestion") or "")
        conf = data.get("confidence")

        # 依据识别结果自动检索标准（合规闭环）
        reg_text = ""
        try:
            if on_status: on_status("du_judge", "active")
            from tools.kb_tools import search_knowledge_base
            q = f"{obj} 尺寸 测量 检验 判定".strip()
            if suggestion and suggestion != "unknown":
                q = f"{obj} {suggestion}".strip()
            reg_text = search_knowledge_base(q, top_k=4)
            if on_status: on_status("du_judge", "done")
        except Exception:
            if on_status: on_status("du_judge", "done")

        # 结构化卡片（前端渲染为视觉分析卡）
        card = {
            "image": _os.path.basename(image_path),
            "object": obj,
            "material_hint": material,
            "surface_state": surface,
            "visible_defects": defects,
            "measurable_features": features,
            "inspection_suggestion": suggestion,
            "confidence": conf,
        }
        out = "```vision-card\n" + json.dumps(card, ensure_ascii=False) + "\n```"

        lines = ["【度元 · 图像理解】"]
        lines.append(f"· 识别对象：{obj}")
        if material and material != "unknown":
            lines.append(f"· 材质推测：{material}")
        if surface:
            lines.append(f"· 表面状态：{surface}")
        if defects:
            lines.append("· 可见缺陷：" + "、".join(str(d) for d in defects))
        if features:
            lines.append("· 可测特征：" + "、".join(str(f) for f in features))
        if suggestion:
            lines.append(f"· 检测建议：{suggestion}")
        out += "\n\n" + "\n".join(lines)

        if reg_text and "未找到" not in reg_text and "【错误】" not in reg_text:
            out += f"\n\n📋 **【规元 · 标准依据】**\n{reg_text[:600]}"

        if on_status: on_status("duyuan", "done")
        return out
    except Exception as e:
        if on_status: on_status("duyuan", "done")
        return f"【度元 · 图像理解】异常：{type(e).__name__}: {str(e)[:150]}"


# v4 维护：三元 Agent 并发调度时保护 init_predictor 单例
_predictor_lock = threading.Lock()


def _run_duyuan(query: str, on_status=None, context: str = "", image_path: str = None,
                measurement_context=None, router_decision=None) -> str:
    """Execute grounded tools; descriptive vision never replaces numeric assessment."""
    import json
    from perception.measurement_context import new_context, detect_bound_anomaly
    from tools.measurement_executor import execute_request
    if on_status: on_status("duyuan", "active")
    try:
        measurement_context = measurement_context or new_context(query=query)
        result = execute_request(query,measurement_context,router_decision)
        if result is not None:
            return result
        calls=(router_decision or {}).get("tool_calls") or []
        tools={call.get("name") for call in calls}
        if "anomaly_detect" in tools or any(word in query for word in ("缺陷","异常","表面","划痕","裂纹","检测工件","外观")):
            result=detect_bound_anomaly(measurement_context)
            return "```anomaly-card\n"+json.dumps(result,ensure_ascii=False,allow_nan=False)+"\n```"
        if image_path:
            return _run_duyuan_image(query,image_path,on_status,context)
        if "calib_guide" in tools or any(word in query for word in ("标定步骤","标定指南","标定板")):
            from tools.measurement_tools import calibrate_camera_guide
            return calibrate_camera_guide()
        if any(word in query for word in ("不确定度","测量","精度","合格","合规")):
            return "暂不计算：请明确任务，并提供实测值、器具精度/MPE和环境温度；参考一致性比较还需要参考值。当前不执行工件公差符合性判定。"
        return "本轮未识别到可执行的测量任务，请补充任务与输入来源。"
    finally:
        if on_status: on_status("duyuan", "done")


def _extract_params(query: str, defaults: dict, aliases: dict = None) -> dict:
    """LLM从自然语言中提取参数JSON。失败时返回默认值。

    P1-19：改用轻量模型（FAST_MODEL，默认 qwen-flash）。这是纯机械的
    JSON 抽取任务，用主力大模型（qwen3.8-max，实测 1103ms）属浪费；
    实测 qwen-flash 279ms，且该步原先每轮约 1.8s。

    稳定性修复（实测发现）：同一句
    "实测值10.014mm，标准值10.00mm，精度0.01mm…" 连续两次抽取，
    第一次得 rv=10.0、第二次得 rv=100.0（退回了默认值）——轻量模型在
    0.3 温度下对字段语义映射不稳定。两处加固：
      1) temperature 降到 0（抽取是确定性任务，不该有采样随机性）；
      2) 在 prompt 里给出字段语义说明（aliases），把"标准值/标称值→rv"
         这类映射讲清楚，而不是让模型从键名猜。
    并记录失败/回退情况便于观测。
    """
    schema = json.dumps(defaults, ensure_ascii=False)
    alias_txt = ""
    if aliases:
        alias_txt = "字段含义：" + "；".join(
            f"{k} = {v}" for k, v in aliases.items()) + "\n"
    try:
        raw = _llm_chat(
            (
                "从用户问题中提取参数，只输出 JSON。\n"
                f"参数模板(带默认值): {schema}\n"
                + alias_txt +
                "规则：\n"
                "1) 用户明确给出的数值必须填入对应字段（例如\"标准值10.00mm\"→ rv=10.0，"
                "\"实测值10.014mm\"→ mv=10.014）；\n"
                "2) 未提及的参数才保留默认值；\n"
                "3) 数值只取数字，不要带单位或文字；\n"
                "4) 不加 markdown 代码块，不要解释。"
            ),
            query,
            max_tokens=120,
            model=FAST_MODEL,
            timeout=20,
            temperature=0.0,          # 抽取需确定性，禁用采样随机性
        )
        data = json.loads(raw.strip().replace("```json", "").replace("```", ""))
        # 保留类型：默认值是 float/int 时确保结果同类型。
        # 注意默认值可能是 None（表示"必需参数，不许代入默认值"），
        # 此时 type(None)(x) 会抛 TypeError，故需跳过并做数值解析。
        for k, v in defaults.items():
            if k not in data:
                continue
            raw_v = data[k]
            if raw_v is None or raw_v == "":
                data[k] = None if v is None else v
                continue
            if v is None:
                # 无默认值 → 尽量解析为数值；解析不出来就置 None 交由调用方拒算
                if isinstance(raw_v, (int, float)):
                    data[k] = float(raw_v)
                else:
                    try:
                        data[k] = float(str(raw_v).strip())
                    except (TypeError, ValueError):
                        data[k] = None
                continue
            try:
                data[k] = type(v)(raw_v)
            except (TypeError, ValueError):
                print(f"[度元] 参数 {k}={raw_v!r} 无法转换为 {type(v).__name__}，保留原值")
        return {**defaults, **data}
    except Exception as e:
        print(f"[度元] 参数提取失败({e})，使用默认值 {defaults}")
        return defaults


# ======== 并发调度 ========

AGENT_RUNNERS = {
    "guiyuan": _run_guiyuan,
    "suoyuan": _run_suoyuan,
    "duyuan": _run_duyuan,
}


def coordinator_chat(message: str, history: list = None, on_status=None, image_path: str = None,
                     measurement_context=None) -> str:
    """Coordinator 主入口：分析 → 并发 → 融合。
    history: [{"role":"user","content":"..."},{"role":"assistant","content":"..."}]
    image_path: 可选，用户上传图片的本地路径（多模态入链）

    注意：本函数不写聊天历史，统一由调用方（agent_core.chat）保存，
    避免同一条回复被重复入库。
    """
    has_image = bool(image_path)
    from perception.measurement_context import new_context, bind_percepts
    from perception.bus import collect, to_state
    bound_context = new_context(measurement_context,query=message)
    percepts = to_state(collect(query=message,image_path=image_path,
                               want_camera=not has_image and any(w in message for w in ("摄像头","拍照","缺陷","外观")),
                               want_sensor=any(w in message for w in ("传感器","温度","环境"))))
    for percept in percepts:
        if percept.get("type") in ("image","camera"):
            path = (percept.get("payload") or {}).get("path")
            if path:
                from compliance.gate import redact_faces
                processed, _faces, processing_status = redact_faces(path)
                percept["payload"].update(path=processed,processing_status=processing_status)
                image_path, has_image = processed, True
                break
    bound_context = bind_percepts(bound_context,percepts)

    # 图片预理解：先做一次结构化视觉分析，结果供全部子 Agent 共享
    # （规元据此检索对应标准；度元直接复用，避免重复调用视觉模型）
    vision_raw = ""
    if has_image:
        try:
            from tools.multimodal_tools import inspect_image
            if on_status: on_status("du_visual", "active")
            vision_raw = inspect_image(image_path)
            if on_status: on_status("du_visual", "done")
        except Exception as e:
            vision_raw = ""
            print(f"[Coordinator] 图片预理解失败: {e}")

    intent = analyze_intent(message, has_image=has_image)
    agents = intent["agents"]

    if on_status:
        # P5-52 修复：原实现发的是原始 agent id（guiyuan/suoyuan/duyuan），
        # 而前端时间轴只认中文节点名，导致这些状态被丢弃或错标。
        # 现直接发中文名，并且**不再使用 "status" 这个保留 id** ——
        # 前端会把它当作面板标题，从而把内部实现名暴露给用户。
        _CN = {"guiyuan": "规元·标准检索", "suoyuan": "索元·联网检索", "duyuan": "度元·计算判定"}
        names = [_CN.get(a, SUB_AGENTS[a]["name"]) for a in agents]
        on_status("supervisor", f"active|已调度 {len(names)} 个子智能体：{'、'.join(names)}")

    # 构建上下文提示词
    ctx_parts = []
    if has_image:
        ctx_parts.append("本轮用户上传了一张图片。")
        if vision_raw:
            ctx_parts.append(f"图像理解结果（供检索标准与判定参考）：{vision_raw}")
    if history:
        for h in history[-4:]:  # 最近2轮(4条)
            role = "用户" if h["role"] == "user" else "Equis"
            ctx_parts.append(f"{role}: {h['content'][:200]}")
    context_prompt = "\n".join(ctx_parts) if ctx_parts else ""

    # 并发执行（P5-44：每次子智能体调用都留痕，含耗时与成败）
    results = {}
    import time as _t
    with ThreadPoolExecutor(max_workers=3) as pool:
        _t0 = {}
        futures = {}
        for a in agents:
            _t0[a] = _t.time()
            if a == "duyuan":
                future = pool.submit(AGENT_RUNNERS[a],message,on_status,context_prompt,image_path,
                                     measurement_context=bound_context)
            else:
                future = pool.submit(AGENT_RUNNERS[a],message,on_status,context_prompt)
            futures[future] = a
        for future in as_completed(futures):
            agent_id = futures[future]
            try:
                results[agent_id] = future.result(timeout=90)
                _ok = True
            except Exception as e:
                results[agent_id] = f"【{SUB_AGENTS[agent_id]['name']}】执行异常: {e}"
                _ok = False
            try:
                from compliance.gate import audit_tool_call
                audit_tool_call(agent_id, _ok,
                                "%dms" % int((_t.time() - _t0.get(agent_id, _t.time())) * 1000))
            except Exception:
                pass

    # 单 Agent 时直接返回，避免多余 LLM 调用与措辞漂移
    if len(agents) == 1:
        only = agents[0]
        raw = results.get(only, "") or ""
        if raw.lstrip().startswith("```"):
            # 含结构化卡片（如 vision-card）时不做二次融合，保留卡片原样
            merged = raw
        else:
            merged = (f"【{SUB_AGENTS[only]['name']}】\n" + raw) if not raw.startswith("【") else raw
    else:
        # LLM 融合三元结果
        # P5-52：不再用保留 id "status"（前端会把它当作面板标题，
        # 从而把内部实现名 "Coordinator" 暴露给用户）
        if on_status:
            on_status("merge", "active|正在整合多方分析结果")
        if any("```compliance-card" in value for value in results.values()):
            merged = "\n\n".join(results.values())
        else:
            merged = _llm_merge(message, results, agents, has_image=has_image)

    # 统一附加"检索依据清单"：让"可追溯"落到可见处，评审可逐条核对
    srcs = _collect_sources(results)
    if srcs:
        merged += "\n\n---\n📎 本次知识库检索依据：" + "；".join(srcs[:6])
    return merged


def _collect_sources(results: dict) -> list:
    """从**知识库通道**提取命中条目名（用于生成可核对的依据清单）

    P5-52 根治：原先对所有分支无差别抓 `来源[:：]xxx`，导致：
      ① 索元分支里模型**自我介绍**中的"来源"字样被误收
         （实测出现过"📎 本次知识库检索依据：通义千问"）；
      ② 更本质的问题是：**百炼 enable_search 并不返回可核查的 URL 或来源列表**
         （实测响应仅 content + reasoning_content 两个文本字段），
         所谓"来源"是模型自己编的写进正文的措辞。

    因此"知识库检索依据"这个清单**只能来自知识库通道** ——
    那里有 ChromaDB 索引与 doc_id，是真实可回溯的；
    联网通道无据可查，就不该出现在"依据"里。

    同时保留两道过滤，防御"来源"字样出现在知识库分支正文的边缘情况。
    """
    srcs = []
    for agent_key, txt in (results or {}).items():
        # 只采信知识库通道（规元）；索元无真实可溯源，不进依据清单
        if agent_key not in ("guiyuan", "kb"):
            continue
        t = str(txt or "")
        if not t or "未检索到可用的公开信息" in t:
            continue
        for m in re.finditer(r'来源[:：]\s*([^\n(（]+)', t):
            s = m.group(1).strip().rstrip('（(').strip()
            if not s:
                continue
            # 过滤 2：过短的串不可能是标准条目名（真实依据如
            # "GB/T 18779.1-2002""JJG 21-2008"均明显长于 8 字符）
            if len(s) < 8:
                continue
            # 过滤 3：模型自指名
            if re.search(r'(通义|千问|Qwen|阿里)', s, re.I):
                continue
            if s not in srcs:
                srcs.append(s)
    return srcs


def _std_refs(text: str) -> set:
    """抽取文本中出现的**标准号主体**（用于融合后的"信息保留守门"）。

    只比较"GB/T 1958"这种主体，**刻意忽略年份与破折号写法**：
    模型可能把 "GB/T 1958-2017" 写成 "GB/T 1958"、"GB/T1958—2017"，
    这些都不算"丢失引用"。第一版比较时保留了年份与原始破折号，
    导致 flash 与 max **两个模型都被误判为丢失全部引用**（守门假阳性）。
    """
    import re as _re
    if not text:
        return set()
    pat = _re.compile(
        r"(GB/?T|GB|JJF|JJG|ISO(?:/IEC)?|OIML\s*R?|VDA)\s*([0-9]+(?:\.[0-9]+)*)",
        _re.I)
    out = set()
    for prefix, num in pat.findall(str(text)):
        p = _re.sub(r"[^A-Z]", "", prefix.upper())   # GBT / JJF / ISO ...
        out.add(p + num)
    return out


def _deterministic_merge(query: str, texts: list, cards: list) -> str:
    """确定性拼装：不调 LLM，保留每个分支的原始表述。

    用于三种情形：融合模型失败 / 超时 / 融合结果丢失了标准编号。
    代价是输出会带"【规元】/【度元】"分段标注（不再是一个人的口吻），
    但**信息一条不丢**——在可信度与文风之间，这里优先可信度。
    """
    from tools.cards import merge_card_blocks
    parts = [f"以下是对「{query[:30]}」的综合分析：\n"]
    for t in texts:
        parts.append(t)
        parts.append("")
    body = "\n".join(parts).strip()
    _cb = merge_card_blocks(cards)
    return (_cb + "\n\n" + body) if _cb else body


def _llm_merge(query: str, results: dict, agents: list, has_image: bool = False) -> str:
    """LLM 融合多子 Agent 结果。失效时回退模板拼接。

    结构化卡片（```xxx-card ... ```）不参与 LLM 融合，避免被改写，
    统一在融合结果前原样拼回。

    P1-18：卡片抽取改用 tools/cards.py 的统一实现（此处原先自带一份
    与 graph.runner / agent_core 重复的正则）。
    """
    from tools.cards import extract_cards, merge_card_blocks

    if len(agents) == 1:
        a = agents[0]
        raw = results.get(a, "")
        label = f"【{SUB_AGENTS[a]['name']}】\n"
        return label + raw if not raw.startswith("【") else raw

    # 抽出卡片（结构化），剩余纯文本参与融合
    cards, plain = [], {}
    for a, txt in results.items():
        if not txt:
            continue
        found, cleaned = extract_cards(txt)
        if found:
            cards.extend(found)
        if cleaned:
            plain[a] = cleaned

    texts = []
    for a in ["guiyuan", "duyuan", "suoyuan"]:
        if a in plain and plain[a]:
            texts.append(f"【{SUB_AGENTS[a]['name']}】\n{plain[a]}")
    if not texts:
        return merge_card_blocks(cards) if cards else "多智能体分析未能产生有效结果。"

    combined = "\n\n".join(texts)

    # 快速路径：内容较短时直接结构化拼接，省一次 LLM 往返（演示场景提速明显）
    if len(combined) <= MERGE_FAST_CHARS:
        body = combined
        _cb = merge_card_blocks(cards)
        return (_cb + "\n\n" + body) if _cb else body

    fused = ""
    # 原文里的标准编号集合 —— 用于校验融合是否把引用弄丢
    _src_refs = _std_refs(combined)
    try:
        img_hint = "（本轮用户上传了图片，已由度元完成图像理解）" if has_image else ""
        prompt = (
            f"用户问题：{query}{img_hint}\n\n"
            f"各子Agent分别给出了以下分析结果：\n\n{combined}\n\n"
            "请将以上分析结果融合成一段连贯、专业的回答。"
            '以自然段落形式组织，不要分段标注"规元/度元/索元"，而是像一个人在综合所有信息后给出的完整回答。'
            "保留关键数据和规程引用。纯文本格式。\n" + _CITATION_RULE
        )
        # 显式要求"逐个保留标准编号"——实测这一步能消除轻量模型的引用丢失：
        #   同一输入，flash 用原提示 3.65s 但漏掉 GB/T 18779.1；
        #   加这句后 flash 2.54s 且零丢失（plus/max 同样零丢失）。
        # 仍保留下面的守门作为兜底，防止换个输入又漏。
        if _src_refs:
            prompt += ("\n【重要】原文中出现的每一个标准编号"
                       "（形如 GB/T 1958-2017、JJF 1064-2010）都必须逐一出现在你的回答里，"
                       "一个都不能省略或合并。")
        # 维护 2026-10-17：换用 MERGE_MODEL（默认 qwen-flash，实测 1.84s vs
        # max 的 8.01s，关键信息保留 3/3），并给融合单独的超时上限。
        fused = _llm_chat(
            "你是Equis的Coordinator，负责将多个子Agent的分析结果融合为一段完整回答。",
            prompt,
            max_tokens=MERGE_MAX_TOKENS,
            model=MERGE_MODEL,
            timeout=MERGE_TIMEOUT,
        )
    except Exception as e:
        print(f"[Merge] 融合失败，退回确定性拼装: {type(e).__name__}: {str(e)[:80]}")
        return _deterministic_merge(query, texts, cards)

    # ── 信息保留守门（防止轻量模型融合时丢掉关键引用）──
    # 只在"原文确实带标准编号"时校验：融合结果必须一个不少。
    # 少一个就退回确定性拼装 —— 宁可文风差一点，也不能让引用凭空消失。
    if _src_refs:
        _lost = _src_refs - _std_refs(fused)
        if _lost:
            print("[Merge] 融合丢失引用 %s，退回确定性拼装" % sorted(_lost)[:3])
            try:
                from compliance.gate import audit_log
                audit_log("error", "merge_lost_refs|%s" % ",".join(sorted(_lost))[:120])
            except Exception:
                pass
            return _deterministic_merge(query, texts, cards)

    _cb = merge_card_blocks(cards)
    if _cb:
        return _cb + "\n\n" + fused
    return fused




# ======== 提供给 agent_core 的入口 ========

def is_multi_agent_query(message: str) -> bool:
    """判断是否应该启用多智能体。只要有1个Agent就通过Coordinator调度，获得实时状态反馈。"""
    intent = analyze_intent(message)
    return len(intent["agents"]) >= 1
