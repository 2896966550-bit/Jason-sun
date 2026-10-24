#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OOD×GUM 回归测试（P2-26）

运行方式（依赖齐全的解释器）::

    C:\\Users\\Administrator\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe -m unittest discover -s agent -p "test_*.py" -v
    # 或直接:
    ...\\python.exe agent\\omni_ad\\uncertainty\\test_compliance.py

仅使用标准库 unittest（环境中未安装 pytest，已确认）。

覆盖清单（对应任务书 P2-26 的 1~6 条，另加 P2-20/21/23 的补充用例）：
    1. map_anomaly 的 hard/softplus 数值与边界（含 ±30 截断）
    2. compute_u_ood 的方和根合成正确性
    3. 不变式：|d| 超差时 u_ood 增大不得把结论翻成 pass（含全参数扫描）
    4. 三态判定的三个分支各一例
    5. guard_band_factor 生效
    6. YAML 加载的默认值兜底（文件不存在/字段缺失/语法错误都不崩）
    7. 传感器马氏距离异常量（P2-23）
    8. 热膨胀系数与材料表（P2-21）
    9. beta 物理锚点标定（P2-20）
   10. 向后兼容（dataclass 字段追加、coordinator 关键参数组合）
"""

import math
import os
import sys
import tempfile
import unittest

# ---- 路径引导：支持"直接运行本文件"与"-m unittest discover"两种方式 ----
_THIS = os.path.abspath(__file__)                       # .../omni_ad/uncertainty/test_compliance.py
_UNC_DIR = os.path.dirname(_THIS)                        # .../omni_ad/uncertainty
_OMNI_DIR = os.path.dirname(_UNC_DIR)                    # .../omni_ad
_AGENT_DIR = os.path.dirname(_OMNI_DIR)                  # .../agent
_REPO_ROOT = os.path.dirname(_AGENT_DIR)                 # repo root
for _p in (_REPO_ROOT, _AGENT_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent.omni_ad.uncertainty.compliance import (  # noqa: E402
    ComplianceInput,
    ComplianceResult,
    compute_compliance,
    compute_u_ood,
    compute_verdict,
    map_anomaly,
    sensor_anomaly_from_readings,
    calibrate_beta_physical_anchor,
    result_to_dict,
    resolve_temp_coefficient,
    MATERIAL_TEMP_COEFFICIENTS,
    DEFAULT_MATERIAL,
    MAX_OOD_INFLATION,
)
from agent.omni_ad.uncertainty.l3_config import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    load_l3_config,
    build_compliance_input,
    l3_config_to_dict,
)

RANK = {"pass": 0, "indeterminate": 1, "fail": 2}

# coordinator.py 的真实默认参数（改造前实测复算用的同一组）
COORD_DEFAULTS = dict(
    measured_value=100.0,
    reference_value=100.0,
    sensor_precision=0.01,
    env_temp=25.0,
    visual_anomaly_score=-3.0,
    visual_threshold=-4.0,
    beta_vis=0.001,
    a_mapping="hard",
    include_u_ood=True,
    k=2.0,
)


def make_input(**kwargs):
    """以 coordinator 默认参数为底，按需覆盖"""
    cfg = dict(COORD_DEFAULTS)
    cfg.update(kwargs)
    return ComplianceInput(**cfg)


class TestMapAnomaly(unittest.TestCase):
    """用例 1：map_anomaly 的 hard/softplus 数值与边界"""

    def test_hard_below_threshold_is_zero(self):
        self.assertEqual(map_anomaly(-5.0, -4.0, "hard"), 0.0)
        self.assertEqual(map_anomaly(-4.0, -4.0, "hard"), 0.0)  # 边界：等于阈值 → 0

    def test_hard_above_threshold(self):
        self.assertAlmostEqual(map_anomaly(-3.0, -4.0, "hard"), 1.0, places=12)
        self.assertAlmostEqual(map_anomaly(0.0, -4.0, "hard"), 4.0, places=12)

    def test_softplus_matches_log1p_exp(self):
        for x in (-10.0, -1.0, 0.0, 0.5, 3.0, 29.9):
            self.assertAlmostEqual(
                map_anomaly(x, 0.0, "softplus"),
                math.log1p(math.exp(x)),
                places=12,
                msg="softplus 与 log1p(exp(x)) 不一致 @x=%s" % x,
            )

    def test_softplus_at_threshold_is_log2(self):
        # x=0 时 softplus = log(2)
        self.assertAlmostEqual(map_anomaly(-4.0, -4.0, "softplus"), math.log(2.0), places=12)

    def test_softplus_upper_truncation_at_30(self):
        # x>30 直接返回 x（避免 exp 溢出）
        self.assertEqual(map_anomaly(30.0 + 1e-9, 0.0, "softplus"), 30.0 + 1e-9)
        self.assertEqual(map_anomaly(1000.0, 0.0, "softplus"), 1000.0)
        # x=30 走 log1p 分支，与 30 的差应远小于 1e-9（log1p(e^30)≈30）
        self.assertAlmostEqual(map_anomaly(30.0, 0.0, "softplus"), 30.0, places=9)

    def test_softplus_lower_truncation_at_minus_30(self):
        # x<-30 直接返回 0.0
        self.assertEqual(map_anomaly(-30.0 - 1e-9, 0.0, "softplus"), 0.0)
        self.assertEqual(map_anomaly(-1000.0, 0.0, "softplus"), 0.0)
        self.assertAlmostEqual(map_anomaly(-30.0, 0.0, "softplus"), 0.0, places=12)

    def test_softplus_is_always_positive(self):
        # softplus 在阈值处仍 >0（与 hard 的差异，用于 G2b 消融说明）
        # x=-1 时 softplus = log(1+e^-1) ≈ 0.3133
        self.assertGreater(map_anomaly(-5.0, -4.0, "softplus"), 0.0)
        self.assertAlmostEqual(map_anomaly(-5.0, -4.0, "softplus"),
                               math.log1p(math.exp(-1.0)), places=12)
        self.assertLess(map_anomaly(-5.0, -4.0, "softplus"), 0.5)
        # 远离阈值时迅速趋于 0（x=-10 → 4.5e-5）
        self.assertLess(map_anomaly(-14.0, -4.0, "softplus"), 1e-4)
        # x=-30 是 <= -30 的边界，仍走 log1p 分支（返回 e^-30 ≈ 9.36e-14）
        self.assertAlmostEqual(map_anomaly(-34.0, -4.0, "softplus"),
                               math.log1p(math.exp(-30.0)), places=15)
        # x<-30 才走截断分支，严格返回 0.0
        self.assertEqual(map_anomaly(-34.0 - 1e-6, -4.0, "softplus"), 0.0)

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            map_anomaly(0.0, 0.0, "relu")
        with self.assertRaises(ValueError):
            map_anomaly(0.0, 0.0, "")


class TestComputeUOod(unittest.TestCase):
    """用例 2：compute_u_ood 方和根合成正确性"""

    def test_root_sum_square(self):
        u_vis, u_sen, u_ood = compute_u_ood(2.0, 3.0, 0.5, 0.25)
        self.assertAlmostEqual(u_vis, 1.0, places=12)
        self.assertAlmostEqual(u_sen, 0.75, places=12)
        self.assertAlmostEqual(u_ood, math.hypot(1.0, 0.75), places=12)
        # 显式核对 sqrt(a^2+b^2)
        self.assertAlmostEqual(u_ood, math.sqrt(1.0 ** 2 + 0.75 ** 2), places=12)

    def test_zero_sources(self):
        self.assertEqual(compute_u_ood(0.0, 0.0, 0.001, 0.001), (0.0, 0.0, 0.0))

    def test_single_source_dominates(self):
        u_vis, u_sen, u_ood = compute_u_ood(0.0, 5.0, 0.001, 0.002)
        self.assertEqual(u_vis, 0.0)
        self.assertAlmostEqual(u_sen, 0.01, places=12)
        self.assertAlmostEqual(u_ood, 0.01, places=12)

    def test_synthesis_in_main_entry(self):
        """主入口的 u_c 必须严格等于三源方和根"""
        res = compute_compliance(make_input(sensor_anomaly_score=6.0, sensor_threshold=2.8,
                                            beta_sensor=0.002))
        expect = math.sqrt(res.u_a ** 2 + res.u_b ** 2 + res.u_ood ** 2)
        self.assertAlmostEqual(res.u_c, expect, places=12)
        self.assertGreater(res.u_ood_sensor, 0.0)
        # 方差可加性
        self.assertAlmostEqual(
            res.u_c ** 2,
            res.u_a ** 2 + res.u_b ** 2 + res.u_ood_vis ** 2 + res.u_ood_sensor ** 2,
            places=15,
        )

    def test_g0_disables_ood(self):
        res = compute_compliance(make_input(include_u_ood=False))
        self.assertEqual(res.u_ood, 0.0)
        self.assertEqual(res.a_vis, 0.0)
        self.assertEqual(res.mode, "L1")
        self.assertAlmostEqual(res.u_c, math.hypot(res.u_a, res.u_b), places=15)


class TestVerdictBranches(unittest.TestCase):
    """用例 4：三态判定的三个分支各一例"""

    def test_pass_branch(self):
        res = compute_compliance(make_input(measured_value=100.0))
        self.assertEqual(res.verdict, "pass")
        self.assertFalse(res.requires_human_review)
        self.assertTrue(res.in_spec)
        self.assertEqual(res.confidence, 1.0)
        self.assertIn("一致性", res.reason)
        self.assertNotIn("判合格", res.reason)

    def test_indeterminate_branch_inside_critical_band(self):
        # U_pass = 0.8 * U_base ≈ 0.0106558；取 |d| = 0.9 * U_base（band 上界 1.2*U_base 之内）
        u_pass = 0.8 * compute_compliance(make_input()).U_base
        res = compute_compliance(make_input(measured_value=100.0 + u_pass * 1.05))
        self.assertEqual(res.verdict, "indeterminate")
        self.assertTrue(res.requires_human_review)
        self.assertIn("不能确定", res.reason)

    def test_fail_branch_beyond_hard_limit(self):
        u_base = compute_compliance(make_input()).U_base
        res = compute_compliance(make_input(measured_value=100.0 + 1.3 * u_base))
        self.assertEqual(res.verdict, "fail")
        self.assertTrue(res.requires_human_review)
        self.assertFalse(res.in_spec)
        self.assertIn("与参考不一致", res.reason)
        self.assertNotIn("判不合格", res.reason)

    def test_verdict_is_one_of_three(self):
        for dev in (-0.01, 0.0, 0.005, 0.011, 0.014, 0.03, 0.2):
            res = compute_compliance(make_input(measured_value=100.0 + dev))
            self.assertIn(res.verdict, ("pass", "fail", "indeterminate"))

    def test_reason_is_chinese_text(self):
        res = compute_compliance(make_input())
        self.assertTrue(res.reason)
        self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in res.reason))


class TestOODMonotonicityInvariant(unittest.TestCase):
    """用例 3：核心不变式 —— OOD 不得放宽验收"""

    def test_fail_never_becomes_pass_with_huge_ood(self):
        """|d| 明显超差时，无论 u_ood 多大都不判 pass"""
        for dev in (0.05, 0.1, 0.5, 5.0):
            verdicts = []
            for beta in (None, 0.0, 1e-4, 0.001, 0.01, 0.1, 1.0, 100.0, 1e6):
                res = compute_compliance(make_input(
                    measured_value=100.0 + dev,
                    include_u_ood=(beta is not None),
                    beta_vis=(0.001 if beta is None else beta),
                ))
                verdicts.append(res.verdict)
                self.assertNotEqual(
                    res.verdict, "pass",
                    msg="不变式违反：Δ=%.3f mm 且 beta_vis=%s 时判 pass（U=%.6f）"
                        % (dev, beta, res.U),
                )
            self.assertTrue(all(v == "fail" for v in verdicts),
                            msg="Δ=%.3f 的判定序列异常: %s" % (dev, verdicts))

    def test_verdict_never_regresses_as_ood_grows(self):
        """全参数扫描：u_ood 单调增大时 verdict 只能更保守，不能回退到 pass"""
        betas = (None, 0.0, 1e-5, 1e-4, 5e-4, 1e-3, 5e-3, 1e-2, 5e-2, 0.1, 1.0, 10.0, 1e5)
        deviations = (0.0, 0.002, 0.005, 0.008, 0.010, 0.012, 0.013, 0.0135, 0.014,
                      0.016, 0.02, 0.03, 0.1, 0.5)
        checked = 0
        for gbf in (1.0, 0.8, 0.5, 1.2):
            for mapping in ("hard", "softplus"):
                for score in (-5.0, -3.0, 0.0, 10.0):
                    for dev in deviations:
                        seq = []
                        for beta in betas:
                            res = compute_compliance(make_input(
                                measured_value=100.0 + dev,
                                include_u_ood=(beta is not None),
                                beta_vis=(0.001 if beta is None else beta),
                                a_mapping=mapping,
                                guard_band_factor=gbf,
                                visual_anomaly_score=score,
                            ))
                            seq.append(res.verdict)
                        ranks = [RANK[v] for v in seq]
                        self.assertEqual(
                            ranks, sorted(ranks),
                            msg="单调性违反 gbf=%s map=%s score=%s Δ=%s: %s"
                                % (gbf, mapping, score, dev, seq),
                        )
                        seen_nonpass = False
                        for v in seq:
                            if v != "pass":
                                seen_nonpass = True
                            else:
                                self.assertFalse(
                                    seen_nonpass,
                                    msg="出现 nonpass→pass 回退 gbf=%s map=%s Δ=%s: %s"
                                        % (gbf, mapping, dev, seq),
                                )
                        checked += 1
        self.assertGreater(checked, 400)  # 确认扫描规模（不是真空通过）

    def test_compute_verdict_invariant_directly(self):
        """直接对 compute_verdict 施加不变式（不含 dataclass 路径）"""
        u_c_base = 0.00666
        for dev in (0.012, 0.014, 0.02, 0.05):
            prev = -1
            for u_ood in (0.0, 1e-4, 1e-3, 1e-2, 0.1, 1.0, 100.0):
                u_c = math.hypot(u_c_base, u_ood)
                out = compute_verdict(deviation=dev, u_c=u_c, u_c_base=u_c_base, u_ood=u_ood)
                r = RANK[out["verdict"]]
                self.assertGreaterEqual(r, prev, msg="单调性违反 Δ=%s u_ood=%s" % (dev, u_ood))
                prev = r
                if dev > 1.2 * 2.0 * u_c_base:
                    self.assertEqual(out["verdict"], "fail")

    def test_reported_U_grows_but_verdict_does_not_loosen(self):
        """u_ood 确实放大了报告中的 U，但结论不放宽（体现"降置信度而非放宽"）"""
        lo = compute_compliance(make_input(measured_value=100.010, include_u_ood=False))
        hi = compute_compliance(make_input(measured_value=100.010, beta_vis=1.0))
        self.assertGreater(hi.U, lo.U)
        self.assertGreaterEqual(RANK[hi.verdict], RANK[lo.verdict])

    def test_ood_inflation_is_capped(self):
        res = compute_compliance(make_input(beta_vis=1e6))
        self.assertTrue(res.ood_capped)
        self.assertLessEqual(res.U, MAX_OOD_INFLATION * res.U_base + 1e-9)

    def test_zero_base_refuses_valid_assessment(self):
        """无可信基线必须报执行不可用，不能把非零OOD截成零后通过。"""
        with self.assertRaises(ValueError) as caught:
            compute_compliance(make_input(sensor_precision=0.0, env_temp=20.0, beta_vis=1e6))
        self.assertEqual(caught.exception.to_dict()["execution_status"], "unavailable")
        self.assertIsNone(caught.exception.to_dict()["verdict"])


class TestGuardBandFactor(unittest.TestCase):
    """用例 5：guard_band_factor 生效"""

    def test_guard_band_shrinks_pass_limit(self):
        r1 = compute_compliance(make_input(guard_band_factor=1.0))
        r05 = compute_compliance(make_input(guard_band_factor=0.5))
        self.assertAlmostEqual(r05.U_pass, 0.5 * r1.U_pass, places=12)
        self.assertEqual(r1.guard_band_factor, 1.0)
        self.assertEqual(r05.guard_band_factor, 0.5)

    def test_guard_band_changes_verdict(self):
        """同一输入：gbf=1.0 判 pass，gbf=0.5 判 indeterminate（行为确实变化）"""
        dev = 0.008   # 介于 0.8*0.5*U_base≈0.0053 与 0.8*U_base≈0.0107 之间
        r_full = compute_compliance(make_input(measured_value=100.0 + dev, guard_band_factor=1.0))
        r_half = compute_compliance(make_input(measured_value=100.0 + dev, guard_band_factor=0.5))
        self.assertEqual(r_full.verdict, "pass")
        self.assertEqual(r_half.verdict, "indeterminate")
        self.assertNotEqual(r_full.in_spec, r_half.in_spec)

    def test_guard_band_effect_is_symmetric(self):
        """guard_band_factor 的作用方向与量级（实测锁定，避免写反）

        判定限值只由 U_base 锚定（不看被 OOD 抬高的 U）：
            pass 门限 = 0.8 × (gbf × U_base)   ← 随 gbf 单调上升
            fail 门限 = 1.2 × U_base           ← 与 gbf 无关
        因此 gbf 越大 → pass 门限越高 → 结论越宽松；gbf 越小 → 越容易判"不能确定"。
        gbf 只影响 pass/indeterminate 的边界，永远不能把超差变成合格（见下个用例）。

        与 U_base 锚定绑定的固有性质：gbf 小到一定程度后 d 会重新落到门限之下
        回到 pass，所以"gbf 越小越保守"并非全局单调（评审若要求该性质需改判定式，
        本次改造选择了"OOD 不得放宽验收"优先，详见报告"取舍"一节）。
        """
        # 宽松侧：gbf 增大时 pass 门限上升 → 结论只能变宽松或不变（rank 非递增）
        for dev in (0.0090, 0.0110, 0.0130, 0.0150):
            seq = [RANK[compute_compliance(
                make_input(measured_value=100.0 + dev, guard_band_factor=g)).verdict]
                for g in (0.8, 0.9, 1.0, 1.1, 1.2)]
            self.assertEqual(seq, sorted(seq, reverse=True),
                             msg="gbf 增大应单调变宽松或不变，实际 Δ=%s: %s" % (dev, seq))
        # gbf 越保守对比：Δ=0.0130
        #   gbf=0.8 → 门限 0.8×0.8×U_base = 0.008525 < 0.0130 → indeterminate
        #   gbf=1.5 → 门限 0.8×1.5×U_base = 0.015984 > 0.0130 → pass
        # 注意方向：门限随 gbf 上升，"更容易 pass"，因此 gbf 越大越宽松；
        # 下面用扫描锁定单调方向，避免把方向写反。
        r_strict = compute_compliance(make_input(measured_value=100.0130, guard_band_factor=0.8))
        r_loose = compute_compliance(make_input(measured_value=100.0130, guard_band_factor=1.5))
        self.assertEqual(r_strict.verdict, "indeterminate")
        self.assertEqual(r_loose.verdict, "pass")
        self.assertLess(r_strict.U_pass, r_loose.U_pass)  # 门限确实随 gbf 上升

    def test_guard_band_never_loosens_fail(self):
        """gbf 无论如何调整，都不允许把超差洗成合格"""
        dev = 0.02
        for g in (0.2, 0.5, 0.8, 1.0, 1.2, 2.0, 5.0):
            res = compute_compliance(make_input(measured_value=100.0 + dev, guard_band_factor=g))
            self.assertEqual(res.verdict, "fail", msg="gbf=%s 时超差被放宽" % g)

    def test_guard_band_reflected_in_result_dict(self):
        res = compute_compliance(make_input(guard_band_factor=0.8))
        d = result_to_dict(res)
        self.assertAlmostEqual(d["guard_band_factor"], 0.8, places=6)
        self.assertAlmostEqual(d["U_effective"], 0.8 * res.U, places=6)
        self.assertIn("verdict", d)
        self.assertIn("U_pass", d)


class TestYamlConfigFallback(unittest.TestCase):
    """用例 6：YAML 加载的默认值兜底"""

    def test_missing_file_does_not_crash(self):
        cfg = load_l3_config(os.path.join(tempfile.gettempdir(), "definitely_no_such_l3.yaml"))
        self.assertFalse(cfg.loaded)
        self.assertTrue(cfg.warnings)
        self.assertEqual(cfg.get("beta_vis"), 0.001)
        self.assertEqual(cfg.get("guard_band_factor"), 1.0)
        self.assertEqual(cfg.get("material"), "steel")
        self.assertAlmostEqual(cfg.get("temp_coefficient"), 11.5e-6, places=15)
        # 依然可构造输入并跑完判定
        inp = build_compliance_input(100.0, 100.0, 0.01, config=cfg)
        self.assertEqual(compute_compliance(inp).mode, "L3")

    def test_malformed_yaml_does_not_crash(self):
        path = os.path.join(tempfile.gettempdir(), "l3_malformed_test.yaml")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("a_mapping: [unclosed\n  ::: :::\n")
            cfg = load_l3_config(path)
            self.assertFalse(cfg.loaded)
            self.assertTrue(cfg.warnings)
            self.assertEqual(cfg.get("a_mapping"), "hard")
        finally:
            os.path.exists(path) and os.remove(path)

    def test_partial_yaml_fills_defaults_per_field(self):
        path = os.path.join(tempfile.gettempdir(), "l3_partial_test.yaml")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("include_u_ood: false\nbeta_vis: 0.0042\n")
            cfg = load_l3_config(path)
            self.assertTrue(cfg.loaded)
            self.assertFalse(cfg.get("include_u_ood"))
            self.assertAlmostEqual(cfg.get("beta_vis"), 0.0042, places=12)
            # 未出现的字段用默认值
            self.assertEqual(cfg.get("guard_band_factor"), 1.0)
            self.assertEqual(cfg.get("sensor_threshold"), 2.8)
            self.assertIn("t_mean", cfg.sensor_baseline)
        finally:
            os.path.exists(path) and os.remove(path)

    def test_wrong_type_falls_back(self):
        path = os.path.join(tempfile.gettempdir(), "l3_wrongtype_test.yaml")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("beta_vis: not-a-number\nguard_band_factor: 0.75\n")
            cfg = load_l3_config(path)
            self.assertEqual(cfg.get("beta_vis"), 0.001)       # 转换失败 → 默认值
            self.assertAlmostEqual(cfg.get("guard_band_factor"), 0.75, places=12)
            self.assertTrue(any("beta_vis" in w for w in cfg.warnings))
        finally:
            os.path.exists(path) and os.remove(path)

    def test_real_config_loads(self):
        cfg = load_l3_config()
        self.assertTrue(cfg.loaded, msg="仓库内 equis_l3.yaml 应能加载: " + cfg.path)
        self.assertEqual(os.path.abspath(cfg.path), os.path.abspath(DEFAULT_CONFIG_PATH))
        self.assertAlmostEqual(cfg.get("temp_coefficient"), 11.5e-6, places=15)
        self.assertEqual(cfg.get("material"), "steel")
        self.assertEqual(cfg.get("sensor_threshold"), 2.8)
        d = l3_config_to_dict(cfg)
        self.assertTrue(d["_loaded_from_yaml"])
        self.assertEqual(d["_source_path"], cfg.path)

    def test_guard_band_in_yaml_changes_behaviour(self):
        """改 YAML 里的 guard_band_factor，行为确实变化（P2-22 验收）"""
        with open(DEFAULT_CONFIG_PATH, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("guard_band_factor: 1.0", text)
        path = os.path.join(tempfile.gettempdir(), "l3_gbf_override_test.yaml")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text.replace("guard_band_factor: 1.0", "guard_band_factor: 0.5"))
            cfg_half = load_l3_config(path)
            cfg_full = load_l3_config()
            dev = 0.008
            r_full = compute_compliance(build_compliance_input(100.0 + dev, 100.0, 0.01, config=cfg_full))
            r_half = compute_compliance(build_compliance_input(100.0 + dev, 100.0, 0.01, config=cfg_half))
            self.assertEqual(r_full.verdict, "pass")
            self.assertEqual(r_half.verdict, "indeterminate")
        finally:
            os.path.exists(path) and os.remove(path)

    def test_build_input_applies_yaml_values(self):
        cfg = load_l3_config()
        inp = build_compliance_input(100.0, 100.0, 0.01, env_temp=25.0, config=cfg,
                                     include_u_ood=False)
        self.assertAlmostEqual(inp.temp_coefficient, 11.5e-6, places=15)
        self.assertEqual(inp.material, "steel")
        self.assertEqual(inp.guard_band_factor, 1.0)
        self.assertEqual(inp.k, 2.0)
        self.assertFalse(inp.include_u_ood)


class TestSensorAnomaly(unittest.TestCase):
    """用例 7：传感器马氏距离式异常量（P2-23）"""

    def test_baseline_readings_are_near_zero(self):
        score = sensor_anomaly_from_readings(25.0, 50.0, 0.0, 0.0, 9.81)
        self.assertLess(score, 1e-9)

    def test_anomalous_readings_are_large(self):
        score = sensor_anomaly_from_readings(38.0, 80.0, 3.0, 2.0, 9.0)
        self.assertGreater(score, 5.0)

    def test_monotone_in_deviation(self):
        base = sensor_anomaly_from_readings(25.0, 50.0, 0.0, 0.0, 9.81)
        mid = sensor_anomaly_from_readings(27.0, 50.0, 0.0, 0.0, 9.81)
        far = sensor_anomaly_from_readings(35.0, 50.0, 0.0, 0.0, 9.81)
        self.assertLess(base, mid)
        self.assertLess(mid, far)

    def test_matches_manual_mahalanobis_formula(self):
        b = {"t_mean": 20.0, "t_std": 2.0, "h_mean": 40.0, "h_std": 5.0,
             "vib_mean": 9.0, "vib_std": 1.0}
        score = sensor_anomaly_from_readings(24.0, 50.0, 4.0, 0.0, 9.0, baseline=b)
        vib = math.sqrt(16.0 + 81.0)                      # 9.8489
        z = ((24.0 - 20.0) / 2.0, (50.0 - 40.0) / 5.0, (vib - 9.0) / 1.0)
        expect = math.sqrt(sum(v * v for v in z))         # sqrt(4+4+0.7206) = 2.95306
        self.assertAlmostEqual(score, expect, delta=1e-12)
        self.assertAlmostEqual(score, 2.95305935728866, delta=1e-9)

    def test_full_inv_cov_path(self):
        inv = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        b = {"t_mean": 25.0, "t_std": 1.0, "h_mean": 50.0, "h_std": 1.0,
             "vib_mean": 0.0, "vib_std": 1.0}
        # z = (1, -2, 2) → d = 3
        score = sensor_anomaly_from_readings(26.0, 48.0, 0.0, 0.0, 2.0, baseline=b, inv_cov=inv)
        self.assertAlmostEqual(score, 3.0, places=12)

    def test_bad_inv_cov_shape_raises(self):
        with self.assertRaises(ValueError):
            sensor_anomaly_from_readings(25.0, 50.0, 0.0, 0.0, 9.81, inv_cov=[[1.0, 0.0], [0.0, 1.0]])

    def test_zero_std_does_not_crash(self):
        b = {"t_mean": 25.0, "t_std": 0.0, "h_mean": 50.0, "h_std": 0.0,
             "vib_mean": 9.81, "vib_std": 0.0}
        score = sensor_anomaly_from_readings(25.0, 50.0, 0.0, 0.0, 9.81, baseline=b)
        self.assertEqual(score, 0.0)

    def test_sensor_score_drives_u_ood_sensor(self):
        normal = sensor_anomaly_from_readings(25.0, 50.0, 0.0, 0.0, 9.81)
        bad = sensor_anomaly_from_readings(38.0, 80.0, 3.0, 2.0, 9.0)
        r_ok = compute_compliance(make_input(sensor_anomaly_score=normal,
                                             sensor_threshold=2.8, beta_sensor=0.01))
        r_bad = compute_compliance(make_input(sensor_anomaly_score=bad,
                                              sensor_threshold=2.8, beta_sensor=0.01))
        self.assertEqual(r_ok.u_ood_sensor, 0.0)          # 正常读数 → 低于阈值 → a=0
        self.assertGreater(r_bad.u_ood_sensor, 0.0)       # 异常读数 → OOD 分量 >0
        self.assertGreater(r_bad.u_ood, r_ok.u_ood)


class TestThermalExpansion(unittest.TestCase):
    """用例 8：热膨胀系数与材料表（P2-21）"""

    def test_default_is_steel_11_5e_6(self):
        inp = ComplianceInput(measured_value=100.0, reference_value=100.0, sensor_precision=0.01)
        self.assertAlmostEqual(inp.temp_coefficient, 11.5e-6, places=15)
        self.assertEqual(DEFAULT_MATERIAL, "steel")

    def test_material_table_values(self):
        self.assertAlmostEqual(MATERIAL_TEMP_COEFFICIENTS["steel"], 11.5e-6, places=15)
        self.assertAlmostEqual(MATERIAL_TEMP_COEFFICIENTS["aluminum"], 23e-6, places=15)
        self.assertAlmostEqual(MATERIAL_TEMP_COEFFICIENTS["invar"], 1.2e-6, places=15)

    def test_ub_dropped_from_0_0289_to_0_0033(self):
        """P2-21 验收：100 mm / ΔT=5 K 下 u_b 从 0.0289 降到约 0.0033 mm"""
        res = compute_compliance(make_input(include_u_ood=False))
        old = 100.0 * 0.0001 * 5.0 / math.sqrt(3)     # 改造前公式
        self.assertAlmostEqual(old, 0.0288675135, places=9)
        self.assertAlmostEqual(res.u_b, 5.75e-3 / math.sqrt(3), places=12)
        self.assertAlmostEqual(res.u_b, 0.0033197640, places=9)
        self.assertLess(res.u_b, 0.0289)
        self.assertGreater(res.u_b, 0.0030)
        # 比值约 8.7（=1e-4 / 11.5e-6）
        self.assertAlmostEqual(old / res.u_b, 0.0001 / 11.5e-6, places=6)

    def test_material_changes_u_b(self):
        steel = compute_compliance(make_input(material="steel", include_u_ood=False))
        alu = compute_compliance(make_input(material="aluminum", include_u_ood=False))
        invar = compute_compliance(make_input(material="invar", include_u_ood=False))
        self.assertAlmostEqual(alu.u_b / steel.u_b, 23e-6 / 11.5e-6, places=9)
        self.assertAlmostEqual(invar.u_b / steel.u_b, 1.2e-6 / 11.5e-6, places=9)
        self.assertLess(invar.u_b, steel.u_b)
        self.assertLess(steel.u_b, alu.u_b)

    def test_explicit_coefficient_wins_over_material(self):
        inp = make_input(material="invar", temp_coefficient=1e-4)
        self.assertAlmostEqual(inp.temp_coefficient, 1e-4, places=15)

    def test_backward_compatible_explicit_old_value(self):
        """旧调用方显式传 0.0001 时行为不变（便于对比/回滚）"""
        inp = make_input(temp_coefficient=0.0001, include_u_ood=False)
        self.assertAlmostEqual(inp.temp_coefficient, 0.0001, places=15)
        self.assertAlmostEqual(compute_compliance(inp).u_b, 0.0288675135, places=9)

    def test_unknown_material_raises(self):
        with self.assertRaises(ValueError):
            resolve_temp_coefficient("unobtainium", None)

    def test_chinese_alias(self):
        self.assertAlmostEqual(resolve_temp_coefficient("殷钢", None), 1.2e-6, places=15)
        self.assertAlmostEqual(resolve_temp_coefficient("铝", None), 23e-6, places=15)


class TestCalibrateBeta(unittest.TestCase):
    """用例 9：beta 物理锚点标定（P2-20）"""

    def test_median_of_ratios(self):
        beta = calibrate_beta_physical_anchor([1.0, 2.0, 3.0], [0.01, 0.04, 0.09], 0.0)
        self.assertAlmostEqual(beta, 0.02, places=12)

    def test_mean_mode(self):
        beta = calibrate_beta_physical_anchor([1.0, 2.0], [0.01, 0.05], 0.0, use_median=False)
        self.assertAlmostEqual(beta, 0.0175, places=12)

    def test_normal_samples_excluded(self):
        # score 全低于阈值 → a=0 → 无法标定 → 0.0
        beta = calibrate_beta_physical_anchor([-5.0, -4.5], [0.01, 0.02], -4.0)
        self.assertEqual(beta, 0.0)

    def test_threshold_shifts_ratio(self):
        beta = calibrate_beta_physical_anchor([1.0], [0.02], 0.0)
        beta_thr = calibrate_beta_physical_anchor([1.0], [0.02], 0.5)
        self.assertAlmostEqual(beta, 0.02, places=12)
        self.assertAlmostEqual(beta_thr, 0.04, places=12)

    def test_length_mismatch_raises(self):
        with self.assertRaises(ValueError):
            calibrate_beta_physical_anchor([1.0], [0.01, 0.02], 0.0)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            calibrate_beta_physical_anchor([], [], 0.0)

    def test_script_demo_end_to_end(self):
        """显式研究阈值下恢复合成设定量级；不构成物理标定。"""
        from agent.scripts.calibrate_beta import demo_samples, summarize_ratios
        research_threshold = 1.25
        scores, devs = demo_samples(threshold=research_threshold)
        st = summarize_ratios(scores, devs, research_threshold)
        self.assertGreater(st["n_used"], 50)
        self.assertGreater(st["beta"], 1e-3)
        self.assertLess(st["beta"], 1e-2)
        self.assertGreater(st["iqr"], 0.0)


class TestBackwardCompatibility(unittest.TestCase):
    """用例 10：向后兼容（字段追加、coordinator 参数组合、前端 dict）"""

    def test_result_field_order_preserved(self):
        """已有字段名字与顺序不能变（前端 compliance-card 依赖）"""
        names = [f for f in ComplianceResult.__dataclass_fields__]  # 保持定义顺序
        legacy = ["u_a", "u_b", "a_vis", "a_sensor", "u_ood_vis", "u_ood_sensor",
                  "u_ood", "u_c", "U", "deviation", "in_spec", "mode", "a_mapping"]
        self.assertEqual(names[:len(legacy)], legacy)
        self.assertTrue(all(n in names for n in (
            "guard_band_factor", "U_effective", "U_pass", "verdict",
            "requires_human_review", "reason", "confidence")))

    def test_input_field_order_preserved(self):
        names = [f for f in ComplianceInput.__dataclass_fields__]
        legacy = ["measured_value", "reference_value", "sensor_precision", "env_temp",
                  "baseline_temp", "temp_coefficient", "visual_anomaly_score",
                  "sensor_anomaly_score", "visual_threshold", "sensor_threshold",
                  "beta_vis", "beta_sensor", "a_mapping", "include_u_ood", "k"]
        self.assertEqual(names[:len(legacy)], legacy)
        self.assertIn("material", names)
        self.assertIn("guard_band_factor", names)

    def test_result_dict_legacy_keys_present(self):
        d = result_to_dict(compute_compliance(make_input()))
        for key in ("u_a", "u_b", "u_ood", "u_ood_vis", "u_ood_sensor", "u_c", "U",
                    "deviation", "in_spec", "mode", "a_mapping"):
            self.assertIn(key, d)
        self.assertIsInstance(d["in_spec"], bool)

    def test_coordinator_default_call_still_works(self):
        """直接复刻 coordinator.py 的构造方式（不改那个文件的前提下必须仍可用）"""
        inp = ComplianceInput(
            measured_value=100.0,
            reference_value=100.0,
            sensor_precision=0.01,
            env_temp=25.0,
            visual_anomaly_score=-3.0,
            visual_threshold=-4.0,
            beta_vis=0.001,
            a_mapping="hard",
            include_u_ood=True,
            k=2.0,
        )
        res = compute_compliance(inp)
        self.assertEqual(res.mode, "L3")
        self.assertTrue(hasattr(res, "verdict"))
        self.assertTrue(hasattr(res, "requires_human_review"))
        # coordinator 现有的 critical 判据仍可用
        critical = (abs(res.deviation) > 0.8 * res.U) and (abs(res.deviation) <= 1.2 * res.U)
        self.assertIsInstance(critical, bool)

    def test_positional_construction_still_works(self):
        """老代码可能按位置传前 3 个参数"""
        inp = ComplianceInput(100.0, 100.0, 0.01)
        res = compute_compliance(inp)
        self.assertAlmostEqual(res.u_a, 0.01 / math.sqrt(3), places=12)

    def test_include_u_ood_false_matches_legacy_g0(self):
        """G0 结果与"仅 u_a+u_b"的经典 GUM 合成一致"""
        res = compute_compliance(make_input(include_u_ood=False))
        u_a = 0.01 / math.sqrt(3)
        u_b = 100.0 * 11.5e-6 * 5.0 / math.sqrt(3)
        self.assertAlmostEqual(res.u_c, math.hypot(u_a, u_b), places=15)
        self.assertAlmostEqual(res.U, 2.0 * res.u_c, places=15)


class TestAblationScript(unittest.TestCase):
    """P2-25 消融脚本的可运行性与不变式自检"""

    def test_synthetic_samples_shape(self):
        from agent.scripts.ablation_ood_gum import synthetic_samples, GROUPS
        samples = synthetic_samples()
        self.assertEqual(len(samples), 6)
        for s in samples:
            for key in ("name", "measured_value", "reference_value", "visual_score",
                        "sensor_score", "sensor_precision", "env_temp"):
                self.assertIn(key, s)
        self.assertEqual(len(GROUPS), 4)
        self.assertEqual([g[0] for g in GROUPS], ["G0", "G1", "G2a", "G2b"])

    def test_invariant_checker_flags_violation(self):
        """自检函数本身有效：构造一条"OOD 放宽验收"的假数据必须被抓出

        注意：G0 fail 而 L3 pass 会同时命中"放宽验收"与"比 G0 更宽松"两条
        （本就是同一缺陷的两个角度），故断言 >=1 而非 ==1。
        """
        from agent.scripts.ablation_ood_gum import check_invariant
        rows = [
            {"name": "x", "group": "G0", "verdict": "fail"},
            {"name": "x", "group": "G1", "verdict": "pass"},
        ]
        n, msgs = check_invariant(rows)
        self.assertGreaterEqual(n, 1)
        self.assertTrue(any("放宽" in m for m in msgs))
        # 正常数据不应报违反
        ok_rows = [
            {"name": "y", "group": "G0", "verdict": "pass"},
            {"name": "y", "group": "G1", "verdict": "indeterminate"},
            {"name": "z", "group": "G0", "verdict": "fail"},
            {"name": "z", "group": "G1", "verdict": "fail"},
        ]
        n_ok, _ = check_invariant(ok_rows)
        self.assertEqual(n_ok, 0)

    def test_invariant_checker_passes_on_real_run(self):
        from agent.scripts.ablation_ood_gum import (
            synthetic_samples, run_group, check_invariant, GROUPS,
        )
        cfg = load_l3_config()
        rows = []
        for s in synthetic_samples():
            for group, include, mapping, _d in GROUPS:
                r = run_group(s, include, mapping, cfg, 0.00354)
                r.update({"name": s["name"], "kind": s["kind"], "group": group})
                rows.append(r)
        n, msgs = check_invariant(rows)
        self.assertEqual(n, 0, msg="消融自检发现不变式违反: %s" % msgs)
        # 表里必须同时出现三种 verdict
        self.assertEqual({r["verdict"] for r in rows}, {"pass", "fail", "indeterminate"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
