"""关键路径单测：路由规划 / 不确定度拒算 / 依据归类

为什么补这三个（维护 2026-10-17）：
    项目非测试代码约 13.5k 行、测试仅约 1.5k 行（≈10%），且集中在 OOD×GUM
    算法层与少量脚本；**图编排、coordinator、工具层基本没有单测**。
    结果是"编造测量值""direct_answer 仍排 compute"这类缺陷只能靠评测脚本
    事后发现。这里挑三条最容易出事、且都有真实故障史（DEF-1 / FAIL-3 等）
    的路径补上确定性单测——不依赖 LLM，可进 CI。

运行：
    <解释器> -m unittest scripts.test_core_paths -v
"""
import os
import sys
import unittest

_AGENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _AGENT)
sys.path.insert(0, os.path.dirname(_AGENT))


# ══════════════════════════════════════════════════════════
# 1. resolve_plan：模型意愿必须服从硬件事实
# ══════════════════════════════════════════════════════════

class ResolvePlanTest(unittest.TestCase):
    """线上故障回归：
       · FAIL-2 抓帧失败后落到裸 LLM 兜底，模型答"我没有权限调用您的摄像头"
       · FAIL-3 纯概念问题被排进 compute，用默认值 mv=100.0 白算 11.8s
       · DEF-1 模型给出 intent=compute 且 direct_answer=True 时仍排 compute
    """

    def setUp(self):
        from agent_router import resolve_plan
        self.resolve = resolve_plan

    @staticmethod
    def _dec(intents, tools=(), direct=False, camera=False, sensor=False):
        return {
            "intent": list(intents),
            "tool_calls": [{"name": t} for t in tools],
            "direct_answer": direct,
            "need_camera": camera,
            "need_sensor": sensor,
        }

    # ── 摄像头不可用时仍要有 compute 兜底 ──
    def test_camera_unavailable_keeps_compute(self):
        dec = self._dec(["vision"], ["capture_camera"], camera=True)
        plan, notes = self.resolve(dec, has_image=False,
                                   camera_available=False, sensor_available=None)
        self.assertIn("compute", plan)           # FAIL-2 的修复点
        self.assertTrue(any("摄像头不可用" in n for n in notes))

    # ── 摄像头可用时不该凭空塞 compute ──
    def test_camera_available_no_forced_compute(self):
        dec = self._dec(["vision"], ["capture_camera"], camera=True)
        plan, _ = self.resolve(dec, has_image=False,
                               camera_available=True, sensor_available=None)
        self.assertIn("vision", plan)

    # ── 概念问题（direct_answer）不得排 compute ──
    def test_direct_answer_suppresses_compute(self):
        dec = self._dec(["compute"], [], direct=True)
        plan, notes = self.resolve(dec, has_image=False,
                                   camera_available=True, sensor_available=None)
        self.assertNotIn("compute", plan)        # DEF-1 的修复点
        self.assertTrue(any("direct_answer" in n for n in notes))

    # ── 有数值计算意图时该排 compute ──
    def test_numeric_intent_adds_compute(self):
        dec = self._dec(["compute", "kb"], ["calc_uncertainty"])
        plan, _ = self.resolve(dec, has_image=False,
                               camera_available=True, sensor_available=None)
        self.assertIn("compute", plan)

    # ── 有图时必须排 vision ──
    def test_image_forces_vision(self):
        dec = self._dec(["kb"], ["kb_search"])
        plan, _ = self.resolve(dec, has_image=True,
                               camera_available=False, sensor_available=None)
        self.assertIn("vision", plan)

    # ── 任何情况下至少要有一个信息分支（否则会落到裸 LLM）──
    def test_always_has_some_branch(self):
        cases = [
            self._dec([], []),
            self._dec(["kb"], []),
            self._dec(["vision"], ["capture_camera"], camera=True),
            self._dec(["compute"], [], direct=True),
        ]
        branches = {"retrieval", "vision", "compute", "plan_execute", "web"}
        for i, dec in enumerate(cases):
            plan, _ = self.resolve(dec, has_image=False,
                                   camera_available=False, sensor_available=None)
            self.assertTrue(branches & set(plan),
                            "用例 %d 的 plan 为空：%r" % (i, plan))

    # ── 去重：同一分支不重复出现 ──
    def test_plan_has_no_duplicates(self):
        dec = self._dec(["kb", "web"], ["kb_search", "web_search", "calc_uncertainty"])
        plan, _ = self.resolve(dec, has_image=True,
                               camera_available=False, sensor_available=False)
        self.assertEqual(len(plan), len(set(plan)), "plan 存在重复分支：%r" % plan)


# ══════════════════════════════════════════════════════════
# 2. _run_duyuan：没有数值必须拒算，绝不代入默认值
# ══════════════════════════════════════════════════════════

class DuyuanRefusalTest(unittest.TestCase):
    """正确性回归：原实现在用户**没给任何数值**时会用默认值
       {"mv": 100.0, "sp": 0.01} 真算一遍，并返回
       「实测值: 100.000 mm / U(k=2)=0.0133 mm」——把编造的测量值当结果呈现。
    """

    def setUp(self):
        import coordinator
        self.co = coordinator
        self._orig_extract = coordinator._extract_params

    def tearDown(self):
        self.co._extract_params = self._orig_extract

    def test_no_numeric_voice_refuses(self):
        out = self.co._run_duyuan("请解释一下测量不确定度的传播律")
        self.assertNotIn("100.000", out, "不得出现编造的默认实测值")
        self.assertTrue(("暂不计算" in out) or ("未识别到实测数值" in out),
                        "应明确拒算并说明原因，实际：%r" % out[:120])

    def test_missing_precision_refuses(self):
        self.co._extract_params = lambda *a, **k: {"mv": 10.014, "sp": None, "temp": 25.0}
        out = self.co._run_duyuan("实测值10.014mm，帮我算不确定度")
        self.assertNotIn("100.000", out)
        self.assertIn("缺少必需参数", out)
        self.assertIn("精度", out)

    def test_with_numbers_computes_and_uses_user_value(self):
        self.co._extract_params = lambda *a, **k: {"mv": 10.014, "sp": 0.01, "temp": 28.0}
        out = self.co._run_duyuan("实测值10.014mm，精度0.01mm，环境温度28度")
        self.assertIn("10.014", out, "必须使用用户给的实测值")
        self.assertNotIn("100.000", out)
        self.assertTrue(("u_c" in out) or ("合成标准不确定度" in out),
                        "应给出不确定度结果，实际：%r" % out[:160])

    def test_extract_params_none_default_does_not_crash(self):
        """默认值为 None（表示"必需参数"）时，类型转换不得抛 TypeError"""
        # 直接调用真实 _extract_params，但让它走异常分支返回默认值
        orig = self.co._llm_chat
        self.co._llm_chat = lambda *a, **k: "这不是 JSON"
        try:
            got = self._orig_extract("x", {"mv": None, "sp": None, "temp": 25.0})
            self.assertEqual(got["mv"], None)
            self.assertEqual(got["sp"], None)
        finally:
            self.co._llm_chat = orig


# ══════════════════════════════════════════════════════════
# 3. _collect_sources：依据只能来自知识库通道
# ══════════════════════════════════════════════════════════

class CollectSourcesTest(unittest.TestCase):
    """线上故障回归：索元（联网）分支里模型**自我介绍**中的"来源"字样
       被误收成"📎 本次知识库检索依据：通义千问"。
       根因是百炼 enable_search 不返回可核查的来源列表，所谓"来源"是模型自述。
    """

    def setUp(self):
        from coordinator import _collect_sources
        self.collect = _collect_sources

    def test_only_kb_channel_is_trusted(self):
        results = {
            "guiyuan": "【1】来源: JJG 21-2008 千分尺检定规程  (编号精确匹配)\n内容…",
            "suoyuan": "【联网搜索】\n来源: 某个网页标题",
        }
        srcs = self.collect(results)
        self.assertTrue(any("JJG 21-2008" in s for s in srcs))
        self.assertFalse(any("某个网页标题" in s for s in srcs),
                         "联网通道不应进依据清单：%r" % srcs)

    def test_filters_model_self_reference(self):
        results = {"guiyuan": "来源: 通义千问"}
        srcs = self.collect(results)
        self.assertEqual(srcs, [], "模型自指名不应被当作依据")

    def test_filters_too_short_strings(self):
        results = {"guiyuan": "来源: 短名"}
        srcs = self.collect(results)
        self.assertEqual(srcs, [])

    def test_dedupes(self):
        results = {"guiyuan": "来源: GB/T 18779.1-2002 判定规则\n来源: GB/T 18779.1-2002 判定规则"}
        srcs = self.collect(results)
        self.assertEqual(len(srcs), len(set(srcs)))

    def test_empty_and_none_safe(self):
        self.assertEqual(self.collect({}), [])
        self.assertEqual(self.collect(None), [])
        self.assertEqual(self.collect({"guiyuan": ""}), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
