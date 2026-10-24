"""LangGraph 图组装

拓扑：
    START → perceive → supervisor ─┬→ recall ───┐
                                   ├→ retrieval ┤
                                   ├→ vision ───┼→ judge → merge → END
                                   ├→ compute ──┤
                                   └→ web ──────┘

【关键工程决策】并行分支一律全部调度，由节点自身判断"执行/跳过"
------------------------------------------------------------------
LangGraph 的汇聚（fan-in）语义是"所有入边完成才触发下游"。
若 supervisor 只动态调度部分分支，未调度的分支节点会让 judge 的
静态入边永远不满足，导致流程阻塞。因此这里固定返回全部 5 个分支，
每个分支节点先检查 state["plan"]：不在计划中则立刻返回（开销微秒级），
从而在不牺牲确定性的前提下实现"按需执行"。
"""
import os, sys, sqlite3, threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DATA_DIR
from graph.state import AgentState, PARALLEL_BRANCHES
from graph import nodes as N

_CHECKPOINT_DB = os.path.join(DATA_DIR, "graph_checkpoints.db")

_graph = None
_lock = threading.Lock()


def _route_from_supervisor(state: AgentState):
    """返回全部并行分支（执行与否由节点内部依据 plan 判定）"""
    return list(PARALLEL_BRANCHES)


def build_graph(with_checkpointer: bool = True):
    """构建并编译状态图"""
    from langgraph.graph import StateGraph, START, END

    g = StateGraph(AgentState)

    g.add_node("perceive", N.node_perceive)
    g.add_node("supervisor", N.node_supervisor)
    g.add_node("recall", N.node_recall)
    g.add_node("retrieval", N.node_retrieval)
    g.add_node("vision", N.node_vision)
    g.add_node("compute", N.node_compute)
    g.add_node("plan_execute", N.node_plan_execute)
    g.add_node("web", N.node_web)
    g.add_node("judge", N.node_judge)
    g.add_node("merge", N.node_merge)

    g.add_edge(START, "perceive")
    g.add_edge("perceive", "supervisor")

    # 并行 fan-out（固定全量）
    g.add_conditional_edges("supervisor", _route_from_supervisor, PARALLEL_BRANCHES)

    # fan-in → judge → merge → END
    for b in PARALLEL_BRANCHES:
        g.add_edge(b, "judge")
    g.add_edge("judge", "merge")
    g.add_edge("merge", END)

    if with_checkpointer:
        # ── P1-13 检查点策略（代码审计修复）────────────────────
        # 原实现用 SqliteSaver(conn) 持久化检查点，实测有两个问题：
        #
        # 1) **并发被串行化**：SqliteSaver 继承 BaseCheckpointSaver，而后者持有
        #    `self.executor = ThreadPoolExecutor(max_workers=1)`（langgraph 用它把
        #    同步 checkpointer 的 put/get 丢进单线程执行）。六个并行分支的
        #    checkpoint 写入因此排队。实测证据（trace.db 24 次运行）：图像类请求
        #    中 retrieval 自身 8.9-13.0s，而下一节点 merge 却记到 13.0-13.7s
        #    ——第二慢的分支在等 checkpoint 写完。
        #
        # 2) **磁盘无界增长**：thread_id 每次请求都是新的毫秒时间戳
        #    （server.py 的 f"sess_{int(time.time()*1000)}"），检查点**只写不读**，
        #    24 次运行就累积 1.84MB + WAL 4.14MB。断点续跑能力从未被使用。
        #
        # 结论：改用 InMemorySaver —— 进程内、无磁盘 IO、无线程池串行化。
        # 代价：进程重启后无历史检查点（本项目不需要；如需持久化请改
        # AsyncSqliteSaver 并配合 aiospython，而非同步 SqliteSaver）。
        try:
            from langgraph.checkpoint.memory import InMemorySaver
            print("[Graph] 检查点：InMemorySaver（进程内，无 SQLite 串行化）")
            return g.compile(checkpointer=InMemorySaver())
        except Exception as e:
            print(f"[Graph] InMemorySaver 不可用({e})，尝试 SqliteSaver")
            try:
                from langgraph.checkpoint.sqlite import SqliteSaver
                os.makedirs(os.path.dirname(_CHECKPOINT_DB), exist_ok=True)
                conn = sqlite3.connect(_CHECKPOINT_DB, check_same_thread=False)
                return g.compile(checkpointer=SqliteSaver(conn))
            except Exception as e2:
                print(f"[Graph] 检查点不可用({e2})，降级为无检查点编译")
    return g.compile()


def get_graph():
    """获取编译后的图（单例，避免重复编译）"""
    global _graph
    if _graph is not None:
        return _graph
    with _lock:
        if _graph is None:
            _graph = build_graph()
    return _graph


def export_mermaid() -> str:
    """导出 Mermaid 拓扑（用于报告插图 / 前端展示）"""
    try:
        return get_graph().get_graph().draw_mermaid()
    except Exception as e:
        return f"# 导出失败: {e}"
