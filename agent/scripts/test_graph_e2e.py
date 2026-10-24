"""图编排端到端测试（D1）

为什么需要：此前 9 套件里，算法层（OOD×GUM 68 条）、工具层、脚本层都有测试，
**唯独图编排（perceive → supervisor → 分支 → judge → merge）没有**。
这部分恰恰是最容易因改名/改动而静默坏掉的地方。

做法：不调真实 LLM（避免花钱与不确定性），而是
  · patch agent_router.route  → 返回固定决策
  · patch coordinator._llm_chat → 返回固定文本
然后跑完整图，断言：
  1. 不抛异常、final 非空
  2. trace 里出现预期节点（retrieval / compute / merge 等）
  3. 决策被正确执行（kb 查询走检索、概念问题不排 compute）

运行：<解释器> -m unittest scripts.test_graph_e2e -v
"""
import os
import sys
import unittest
from unittest.mock import patch

_AGENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _AGENT)
sys.path.insert(0, os.path.dirname(_AGENT))


def _decision(intents, tools=(), direct=False, camera=False, sensor=False):
    return {
        "intent": list(intents),
        "tool_calls": [{"name": t, "args": {}} for t in tools],
        "direct_answer": direct,
        "need_camera": camera,
        "need_sensor": sensor,
        "rewritten_query": "",
        "missing_params": [],
        "_source": "test",
    }


class GraphEndToEndTest(unittest.TestCase):
    """整图执行（mock 掉所有 LLM 调用）"""

    @classmethod
    def setUpClass(cls):
        import coordinator
        import agent_router
        cls.co = coordinator
        cls.router = agent_router
        # Keep graph dispatch/trace real; external capabilities are test doubles.
        for target, value in (
            ("graph.runner._persist_memory", None),
            ("memory.recall.recall", []),
            ("coordinator._run_guiyuan", "测试知识库：JJG 21 千分尺检定规程"),
        ):
            guard = patch(target, return_value=value)
            guard.start()
            cls.addClassCleanup(guard.stop)
        cls._orig_chat = coordinator._llm_chat
        cls._orig_route = agent_router.route
        cls._orig_resolve = agent_router.resolve_plan

        # LLM：任何调用都返回固定文本（模拟"模型已作答"）
        coordinator._llm_chat = lambda *a, **k: "（测试桩）根据 GB/T 18779.1 与 JJF 1059.1 给出结论。"

    @classmethod
    def tearDownClass(cls):
        cls.co._llm_chat = cls._orig_chat
        cls.router.route = cls._orig_route
        cls.router.resolve_plan = cls._orig_resolve

    @staticmethod
    def _executed(steps):
        """真正执行过的节点。

        被跳过的分支也会留一条 step，但 note 是"未调度，跳过"；
        而执行过的分支即使耗时不足 1ms（如 merge 的快速路径），
        note 也会是自己的内容。因此用 note 判断，不用 ms。
        """
        return {s["node"] for s in steps
                if "未调度" not in str(s.get("note") or "")}

    def _run_with(self, decision, query):
        """用给定决策跑一次完整图"""
        self.router.route = lambda *a, **k: dict(decision)
        import graph.runner as R
        # 每次强制重建图，避免缓存住上一次的 patch
        import graph.build as B
        B._graph = None
        out = R.run(query, history=[])
        return out

    def _trace_of_last_run(self):
        from observability import trace
        r = trace.recent(1)
        if not r:
            return None, []
        d = trace.detail(r[0]["id"])
        return r[0], d.get("steps", [])

    # ── 场景 1：知识库检索 ──
    def test_kb_query_runs_retrieval_and_merge(self):
        out = self._run_with(_decision(["kb"], ["kb_search"]), "JJG 21 是什么标准？")
        self.assertTrue(out and len(out) > 5, "final 不应为空")
        run, steps = self._trace_of_last_run()
        self.assertIsNotNone(run, "应产生一条 trace 记录")
        # 注意：被跳过的分支也会写一条 0ms 的"未调度"轨迹，
        # 所以必须按 ms>0 判断"真正执行过"（第一版按出现判断，全部误判）。
        nodes = self._executed(steps)
        self.assertIn("retrieval", nodes, "kb 查询应执行检索分支")
        self.assertIn("merge", nodes, "应执行结果融合")

    # ── 场景 2：概念问题（direct_answer）不应排 compute ──
    def test_concept_question_skips_compute(self):
        out = self._run_with(_decision(["kb"], [], direct=True),
                             "A类评定和B类评定有什么区别？")
        self.assertTrue(out and len(out) > 5)
        run, steps = self._trace_of_last_run()
        nodes = self._executed(steps)
        self.assertNotIn("compute", nodes,
                         "概念问题不应进入计算分支（DEF-1 回归）")

    # ── 场景 3：计算类问题应执行 compute ──
    def test_compute_query_runs_compute(self):
        out = self._run_with(
            _decision(["compute"], ["calc_uncertainty"]),
            "实测值10.014mm 标准值10.00mm 精度0.01mm 帮我算不确定度")
        self.assertTrue(out and len(out) > 5)
        run, steps = self._trace_of_last_run()
        nodes = self._executed(steps)
        self.assertIn("compute", nodes, "计算类问题应执行计算分支")

    # ── 场景 4：图跑完后 trace 完整（有耗时、有结束标记）──
    def test_trace_is_complete(self):
        self._run_with(_decision(["kb"], ["kb_search"]), "JJF 1059.1 主要内容")
        run, steps = self._trace_of_last_run()
        self.assertTrue(run["total_ms"] >= 0)
        self.assertIsNotNone(run["ok"])
        self.assertGreaterEqual(len(steps), 3, "至少应有 perceive/supervisor/merge 等节点")

    # ── 场景 5：空输入不应崩 ──
    def test_empty_query_is_handled(self):
        import graph.runner as R
        out = R.run("", history=[])
        self.assertIsInstance(out, str)
        self.assertTrue(len(out) > 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
