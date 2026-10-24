"""智能任务规划工具 —— LLM 规划 + 真实执行（Plan-Execute），规则降级

2026-09-12 升级：
  - 规则 if-else → LLM Planner：产出结构化步骤 JSON
  - Plan → Execute：逐步调用真实工具并汇总，结果可追溯
  - LLM 不可用 / 解析失败时保留 _rule_plan 降级路径
"""
import os, sys, json, re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

# 规划器允许调用的动作白名单（防止 LLM 幻觉出不存在的能力）
ACTION_DESC = {
    "kb_search": "检索计量标准知识库（参数 query）",
    "read_sensors": "读取当前环境传感器数据（无参数）",
    "anomaly_detect": "对当前摄像头画面做缺陷/异常检测（无参数）",
    "inspect_image": "对指定图片做结构化视觉分析（参数 image_path）",
    "calc_uncertainty": "计算测量不确定度（参数 mv, sp, temp, hum）",
    "comp_3d": "由视差计算三维坐标（参数 disp, bl, fl）",
    "diag": "诊断传感器数据（参数 t, h, ax, ay, az）",
}

_PLAN_PROMPT = (
    "你是精密测量任务的规划器。针对用户需求，输出一个可执行的分步计划。\n\n"
    "可用动作（action 只能从下列取值）：\n"
    + "\n".join(f"- {k}: {v}" for k, v in ACTION_DESC.items())
    + "\n\n要求：\n"
    "1. 步骤 2~5 步，顺序合理（先查标准 → 再取数/测量 → 最后计算或判定）\n"
    '2. 参数尽量从用户描述中提取；确实无法确定的参数填空字符串 ""\n'
    "3. 只输出 JSON，不要 markdown，不要任何解释文字：\n"
    '{"steps":[{"step":1,"action":"kb_search","params":{"query":"轴类工件 直径测量"},'
    '"reason":"先明确适用的计量标准"}]}\n'
)


# ── LLM 规划 ─────────────────────────────────────────────

def _llm_plan(task: str, max_steps: int = 5):
    """请 LLM 产出步骤列表。失败返回 None。"""
    try:
        from openai import OpenAI
        client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": _PLAN_PROMPT},
                {"role": "user", "content": task},
            ],
            temperature=0.2,
            max_tokens=600,
            timeout=45,
            extra_body={"enable_thinking": False},  # 规划属结构化任务，关思考提速
        )
        raw = resp.choices[0].message.content or ""
    except Exception as e:
        print(f"[Planner] LLM 规划失败: {e}")
        return None

    data = _parse_json_loose(raw)
    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, list):
        return None

    clean = []
    for s in steps[:max_steps]:
        if not isinstance(s, dict):
            continue
        action = str(s.get("action", "")).strip()
        if action not in ACTION_DESC:
            continue  # 白名单过滤
        params = s.get("params") if isinstance(s.get("params"), dict) else {}
        clean.append({
            "action": action,
            "params": params,
            "reason": str(s.get("reason", ""))[:80],
        })
    return clean or None


from tools.json_output import parse_json_object as _parse_json_loose


# ── 步骤执行 ─────────────────────────────────────────────

def _to_float(v, default=None):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _pick_device() -> str:
    """选择推理设备：有 CUDA 用 cuda，否则 cpu"""
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def _execute_step(action: str, params: dict, measurement_context=None, query: str = "") -> str:
    """执行单步，返回文本结果（异常不外抛，保证计划整体可完成）"""
    try:
        if action in ("calc_uncertainty","comp_3d"):
            from tools.measurement_executor import execute_tool
            return execute_tool(action,query,measurement_context,args=params)
        if action == "kb_search":
            from tools.kb_tools import search_knowledge_base
            q = str(params.get("query") or "").strip()
            if not q:
                return "[跳过] kb_search 缺少 query 参数"
            return search_knowledge_base(q, top_k=3)[:900]

        if action == "read_sensors":
            from perception.measurement_context import bound_sensor
            reading = bound_sensor(measurement_context)
            return json.dumps(reading,ensure_ascii=False) if reading else "[不可用] 本事件没有有效传感器快照"

        if action == "diag":
            from tools.measurement_tools import sensor_data_diagnose
            from perception.measurement_context import bound_sensor
            reading = bound_sensor(measurement_context)
            if reading is None:
                return "[不可用] 诊断需要本事件的有效传感器快照，不能使用默认读数"
            return sensor_data_diagnose(
                reading["t"],reading["h"],reading["ax"],reading["ay"],reading["az"],
            )



        if action == "anomaly_detect":
            from perception.measurement_context import detect_bound_anomaly
            return json.dumps(detect_bound_anomaly(measurement_context), ensure_ascii=False)

        if action == "inspect_image":
            from tools.multimodal_tools import inspect_image
            from perception.measurement_context import bound_image_path
            p = bound_image_path(measurement_context)
            if not p:
                return "[不可用] inspect_image 需要本事件绑定且未修改的图像"
            return inspect_image(p)[:900]

        return f"[跳过] 未知动作 {action}"
    except Exception as e:
        return f"[执行异常] {action}: {type(e).__name__}: {str(e)[:120]}"


# ── 对外主入口 ───────────────────────────────────────────

def plan_measurement(task_description: str, measurement_context=None) -> str:
    """智能测量任务规划（Plan-Execute）。

    优先 LLM 规划并逐步执行真实工具；失败时降级为规则计划。
    """
    steps = _llm_plan(task_description)
    if steps:
        lines = [f"📋 智能任务规划（Plan-Execute，共 {len(steps)} 步）\n"]
        for i, s in enumerate(steps, 1):
            lines.append(f"【第 {i} 步】{s['action']} — {s['reason'] or ACTION_DESC[s['action']]}")
            out = (_execute_step(s["action"], s["params"],measurement_context=measurement_context,
                                 query=task_description) or "").strip()
            lines.append(out)
            lines.append("")
        return "\n".join(lines).strip()

    return _rule_plan(task_description)


# ── 规则降级路径（保留原实现，LLM 不可用时使用）──────────

def _rule_plan(task_description: str) -> str:
    """规则式规划（降级方案）"""
    from tools.kb_tools import search_knowledge_base

    plan = []
    plan.append("📋 智能任务规划如下（规则模式）：\n")
    task_lower = task_description.lower()

    if any(w in task_lower for w in ["轴", "直径", "圆柱", "外径"]):
        plan.append("🔍 第1步：检索轴类尺寸测量标准")
        kb1 = search_knowledge_base("轴类工件 直径测量 外径千分尺", top_k=2)
        if "未找到" not in kb1 and "【错误】" not in kb1:
            plan.append(kb1[:600])
        plan.append("")
        plan.append("📐 第2步：推荐测量工具 → 外径千分尺（测量范围100-125mm）")
        plan.append("   · 精度：0.01mm")
        plan.append("   · 规程：JJG 21-2008 千分尺检定规程")
        plan.append("")

    elif any(w in task_lower for w in ["温度", "温", "环境"]):
        plan.append("🌡️ 第1步：检索温湿度测量标准")
        kb1 = search_knowledge_base("温度测量 环境条件 温湿度计", top_k=2)
        if "未找到" not in kb1 and "【错误】" not in kb1:
            plan.append(kb1[:600])

    elif any(w in task_lower for w in ["称重", "质量", "重量", "天平"]):
        plan.append("⚖️ 第1步：检索质量测量标准")
        kb1 = search_knowledge_base("电子天平 质量测量 称重", top_k=2)
        if "未找到" not in kb1 and "【错误】" not in kb1:
            plan.append(kb1[:600])

    elif any(w in task_lower for w in ["压力", "压强"]):
        plan.append("🔄 第1步：检索压力测量标准")
        kb1 = search_knowledge_base("压力表 压力测量 数字压力计", top_k=2)
        if "未找到" not in kb1 and "【错误】" not in kb1:
            plan.append(kb1[:600])

    else:
        plan.append(f"🔍 第1步：检索「{task_description[:20]}」相关标准")
        kb1 = search_knowledge_base(task_description, top_k=3)
        if "未找到" not in kb1 and "【错误】" not in kb1:
            plan.append(kb1[:800])

    plan.append("")
    plan.append("📊 第3步：不确定度评估建议")
    if any(w in task_lower for w in ["温度", "温"]):
        plan.append("   · 主要来源：温度传感器精度、环境波动、热平衡时间")
        plan.append("   · 建议：记录环境温度，评估温度偏差对结果的影响")
    elif any(w in task_lower for w in ["轴", "直径", "长度", "尺寸"]):
        plan.append("   · 主要来源：仪器精度、温度效应（热膨胀）、操作人员读数")
        plan.append("   · 公式举例：ΔL = α·L·ΔT（钢轴α≈11.5×10⁻⁶/℃）")
        plan.append("   · 建议：在20℃恒温环境中测量，或进行温度补偿")
    elif any(w in task_lower for w in ["称重", "质量"]):
        plan.append("   · 主要来源：天平准确度、空气浮力、环境振动")
        plan.append("   · 建议：预热30分钟，远离振动源，使用防震台")
    else:
        plan.append("   · 请说明具体测量参数（被测对象、精度要求、环境条件），以便精确评估")

    plan.append("")
    plan.append("✅ 第4步：执行建议")
    plan.append("   1）准备上述标准文件供参考")
    plan.append("   2）检查测量设备是否在校准有效期内")
    plan.append("   3）记录环境条件（温度、湿度）")
    plan.append("   4）进行至少3次重复测量，计算平均值和标准偏差")

    return "\n".join(plan)
