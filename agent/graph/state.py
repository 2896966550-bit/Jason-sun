"""LangGraph 状态定义

AgentState 是整张图共享的单一状态对象：每个节点只返回"要更新的字段"，
由 LangGraph 负责合并。trace 字段带 operator.add reducer，
因此并行分支可以同时追加执行轨迹而不会互相覆盖。
"""
import operator
from typing import Annotated, Optional, TypedDict


class AgentState(TypedDict, total=False):
    # ── 输入 ──
    query: str
    image_path: Optional[str]
    has_image: bool
    history: list[dict]           # 多轮对话历史（近几轮 Q/A，修失忆 bug）
    measurement_context: dict     # 本请求的身份、图像和传感器快照；不由模型生成

    # ── 感知（感知总线输出）──
    percepts: list[dict]          # Percept 序列化列表
    percept_summary: str          # 感知摘要（注入提示词）
    vision_pre: str               # 图片预理解结果（JSON 字符串，供各分支共享）

    # ── 记忆 ──
    memories: list[dict]          # 召回的历史案例
    memory_text: str              # 格式化的记忆文本

    # ── 调度 ──
    plan: list[str]               # supervisor 决定本次执行的分支
    rewritten_query: str          # 指代消解后的独立检索问题（多轮 RAG 改写）
    router_decision: dict         # P1-12：结构化路由决策（intent/tool_calls/direct_answer）
    router_notes: list[str]       # P1-12：路由降级说明（如"摄像头不可用，已改排计算分支"）

    # ── 分支结果 ──
    kb_result: str                # 规元：标准检索
    vision_result: str            # 度元：视觉理解（含 vision-card）
    compute_result: str           # 度元：计算/检测
    plan_result: str              # Plan-Execute：规划+逐步执行结果
    web_result: str               # 索元：联网检索
    judge_result: str             # 合规判定汇总

    # ── 结构化卡片（P1-18：一等字段，不再靠正则从文本里抠）──
    # 原实现用 ```xxx-card``` 代码块在文本中传递结构化数据，然后靠
    # re.findall(r"```[a-zA-Z\-]*card\n.*?\n```", txt, re.S) 在三处
    # 各抠一遍（coordinator._llm_merge、graph.runner._strip_cards、
    # agent_core._strip_cards），既脆弱又重复。
    # 现在卡片在产出时就分离出来放进这里，final 只保留自然语言。
    # 用 operator.add 使并行分支可以各自追加而不互相覆盖。
    cards: Annotated[list, operator.add]

    # ── 轨迹（并行安全：list 拼接 reducer）──
    trace: Annotated[list, operator.add]

    # ── 输出 ──
    final: str
    sources: list[str]
    error: str


# 节点中文名（用于前端状态展示与轨迹可读性）
NODE_LABELS = {
    "perceive": "感知采集",
    "supervisor": "调度器",
    "recall": "记忆召回",
    "retrieval": "规元·标准检索",
    "vision": "度元·视觉理解",
    "compute": "度元·计算判定",
    "plan_execute": "规划执行",
    "web": "索元·联网检索",
    "judge": "合规判定",
    "merge": "结果融合",
}

# 所有可被调度的并行分支
PARALLEL_BRANCHES = ["recall", "retrieval", "vision", "compute", "plan_execute", "web"]
