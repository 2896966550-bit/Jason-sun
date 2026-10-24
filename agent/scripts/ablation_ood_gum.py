#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OOD×GUM 消融实验脚本（P2-25）

对同一组输入分别跑四个实验组，输出可直接粘进汇报的 Markdown/CSV 对比表：

    G0   include_u_ood=False                     （L1 基线：只有 u_a + u_b）
    G1   include_u_ood=True, beta 已标定         （L3 主配置，hard 映射）
    G2a  include_u_ood=True, a_mapping="hard"    （ReLU 硬阈值）
    G2b  include_u_ood=True, a_mapping="softplus"（处处可导，用于 F1 微调）

输出列：样本 / 组别 / u_a / u_b / u_ood / u_c / U / U_base / U_pass / verdict / 人工复核

关键观察点（P2-24 的核心主张）：
    G0 与 G1/G2 的 verdict 差异**只应出现在"临界带"附近**：
      - |d| 明显小于判据 → 所有组都 pass；
      - |d| 明显超差     → 所有组都 fail（OOD 不会把超差洗成合格）；
      - |d| 落在临界带   → G0 可能 pass，而 L3 组判 indeterminate 并要人工复核。
    脚本末尾会打印"OOD 是否放宽过验收"的自检结果（期望 0 次）。

数据说明：
    默认使用**合成数据**（示例样本，固定随机种子，可复现），
    合成数据仅用于让对比表有内容可看与回归自检，
    **真实结论必须用实测样本（真实工件 + 真实 Omni-AD 分数 + CMM 实测偏差）重跑**。
    可用 --csv 传入真实样本集替代。

用法::

    python agent/scripts/ablation_ood_gum.py                 # 打印 Markdown 表
    python agent/scripts/ablation_ood_gum.py --out agent/reports/ablation_ood_gum.md
    python agent/scripts/ablation_ood_gum.py --csv samples.csv --calibrated-beta 0.00354
"""

import argparse
import csv
import os
import random
import sys
from typing import Dict, List, Optional, Sequence, Tuple

# ---- 路径引导：允许 `python agent/scripts/ablation_ood_gum.py` 直接运行 ----
_THIS = os.path.abspath(__file__)
_AGENT_DIR = os.path.dirname(os.path.dirname(_THIS))
_REPO_ROOT = os.path.dirname(_AGENT_DIR)
for _p in (_REPO_ROOT, _AGENT_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent.omni_ad.uncertainty.compliance import (  # noqa: E402
    ComplianceInput,
    compute_compliance,
    sensor_anomaly_from_readings,
)
from agent.omni_ad.uncertainty.l3_config import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    load_l3_config,
    build_compliance_input,
)

# 消融组定义：(组名, include_u_ood, a_mapping, 说明)
GROUPS: Tuple[Tuple[str, bool, str, str], ...] = (
    ("G0", False, "hard", "L1 基线（无 u_ood）"),
    ("G1", True, "hard", "L3 + 标定 beta"),
    ("G2a", True, "hard", "L3 + hard ReLU"),
    ("G2b", True, "softplus", "L3 + softplus"),
)


# ============================ 样本集 ============================

def synthetic_samples(seed: int = 20260609) -> List[Dict[str, float]]:
    """**合成样本集**（占位，不是实测数据）

    构造 6 个样本覆盖三种判定情形：
      - ok-1/ok-2      : |d| 远小于判据                → 期望全组 pass
      - edge-1/edge-2  : |d| 落在临界带（≈0.8~1.2·U）  → 期望 G0 pass、L3 indeterminate
      - bad-1/bad-2    : |d| 明显超差（≈3~5 倍）       → 期望全组 fail（不变式）
    传感器通道用 sensor_anomaly_from_readings() 造出"正常/异常"两类读数，
    演示 P2-23 补齐的第二路 OOD 分量确实参与合成。
    """
    rng = random.Random(seed)
    mv_base = 100.0
    samples: List[Dict[str, float]] = []

    layout = [
        ("ok-1", 0.0000, -5.20, "正常"),
        ("ok-2", 0.0035, -4.60, "正常"),
        ("edge-1", 0.0105, -3.80, "临界"),
        ("edge-2", 0.0128, -3.20, "临界"),
        ("bad-1", 0.0400, -2.40, "超差"),
        ("bad-2", 0.0750, -1.20, "超差"),
    ]
    for name, dev, vscore, kind in layout:
        # 传感器读数：正常样本贴近基线，临界/超差样本引入温湿度与振动偏移
        if kind == "正常":
            t, h, vib_extra = 25.0 + rng.uniform(-0.4, 0.4), 50.0 + rng.uniform(-3, 3), 0.0
        elif kind == "临界":
            t, h, vib_extra = 27.5, 62.0, 0.6
        else:
            t, h, vib_extra = 33.0, 74.0, 2.4
        az = 9.81 + vib_extra
        s_score = sensor_anomaly_from_readings(t, h, 0.05, 0.05, az)
        samples.append({
            "name": name,
            "kind": kind,
            "measured_value": mv_base + dev,
            "reference_value": mv_base,
            "deviation": dev,
            "visual_score": vscore,
            "sensor_score": s_score,
            "sensor_precision": 0.01,
            "env_temp": 25.0,
        })
    return samples


def read_samples_csv(path: str) -> List[Dict[str, float]]:
    """读取真实样本集（列名大小写不敏感）

    必需列：name（可选）, deviation_mm / measured_value + reference_value,
            visual_score, sensor_score（可缺省 → 0）, sensor_precision（默认 0.01）,
            env_temp（默认 25.0）
    """
    out: List[Dict[str, float]] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(
            line for line in f if line.strip() and not line.lstrip().startswith("#")
        )
        if not reader.fieldnames:
            raise ValueError("CSV 没有表头: " + path)
        lower = {str(n).strip().lower(): n for n in reader.fieldnames}

        def pick(*keys: str) -> Optional[str]:
            for k in keys:
                if k in lower:
                    return lower[k]
            return None

        c_name = pick("name", "sample", "样本")
        c_mv = pick("measured_value", "mv", "实测值")
        c_rv = pick("reference_value", "rv", "参考值")
        c_dev = pick("deviation_mm", "deviation", "偏差")
        c_vis = pick("visual_score", "anomaly_score", "视觉分数")
        c_sen = pick("sensor_score", "sensor_anomaly_score", "传感器分数")
        c_sp = pick("sensor_precision", "sp", "传感器精度")
        c_temp = pick("env_temp", "temp", "环境温度")
        if c_vis is None:
            raise ValueError("CSV 缺少必需列 visual_score/anomaly_score:" + path)
        idx = 0
        for row in reader:
            idx += 1
            try:
                if c_dev is not None:
                    dev = float(row[c_dev])
                    mv, rv = 100.0 + dev, 100.0
                elif c_mv is not None and c_rv is not None:
                    mv, rv = float(row[c_mv]), float(row[c_rv])
                    dev = mv - rv
                else:
                    raise ValueError("CSV 需要 deviation_mm 或 (measured_value + reference_value)")
                out.append({
                    "name": (str(row[c_name]) if c_name else "s%02d" % idx),
                    "kind": str(row.get("kind", "真实")),
                    "measured_value": mv,
                    "reference_value": rv,
                    "deviation": dev,
                    "visual_score": float(row[c_vis]),
                    "sensor_score": float(row[c_sen]) if c_sen else 0.0,
                    "sensor_precision": float(row[c_sp]) if c_sp else 0.01,
                    "env_temp": float(row[c_temp]) if c_temp else 25.0,
                })
            except (TypeError, ValueError) as exc:
                print("[warn] 跳过第 %d 行（%s）" % (idx, exc))
    if not out:
        raise ValueError("CSV 中没有有效样本: " + path)
    return out


# ============================ 消融执行 ============================

def run_group(
    sample: Dict[str, float],
    include_u_ood: bool,
    mapping: str,
    cfg,
    beta_vis: float,
) -> Dict[str, object]:
    """按 YAML 配置 + 组别开关跑一次判定"""
    inp = build_compliance_input(
        measured_value=sample["measured_value"],
        reference_value=sample["reference_value"],
        sensor_precision=sample["sensor_precision"],
        env_temp=sample["env_temp"],
        config=cfg,
        include_u_ood=include_u_ood,
        a_mapping=mapping,
        beta_vis=beta_vis,
        visual_anomaly_score=sample["visual_score"],
        sensor_anomaly_score=(sample["sensor_score"] if include_u_ood else None),
    )
    res = compute_compliance(inp)
    return {
        "u_a": res.u_a, "u_b": res.u_b,
        "u_ood_vis": res.u_ood_vis, "u_ood_sensor": res.u_ood_sensor,
        "u_ood": res.u_ood, "u_c": res.u_c, "U": res.U,
        "U_base": res.U_base, "U_pass": res.U_pass,
        "U_effective": res.U_effective,
        "verdict": res.verdict, "in_spec": res.in_spec,
        "review": res.requires_human_review, "confidence": res.confidence,
        "a_vis": res.a_vis, "a_sensor": res.a_sensor,
        "deviation": res.deviation, "mode": res.mode,
    }


def build_table(rows: List[Dict[str, object]], beta_vis: float, source_note: str) -> str:
    """生成 Markdown 表"""
    lines: List[str] = []
    lines.append("### OOD×GUM 消融对比表（G0 / G1 / G2a / G2b）")
    lines.append("")
    lines.append("- 数据来源：%s" % source_note)
    lines.append("- beta_vis = %.6g mm/score_unit（G1/G2 组使用；G0 组不启用 OOD）" % beta_vis)
    lines.append("- 配置文件：agent/omni_ad/configs/equis_l3.yaml（经 l3_config 加载）")
    lines.append("- 单位：mm；u_a=A 类（传感器精度），u_b=B 类（温度），u_ood=OOD 分量，u_c/U=合成/扩展不确定度")
    lines.append("")
    header = ("| 样本 | 类型 | 组别 | u_a | u_b | u_ood | u_ood_vis | u_ood_sensor | u_c | U | "
              "U_base | U_pass | verdict | 人工复核 |")
    sep = "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
    lines.append(header)
    lines.append(sep)
    for r in rows:
        lines.append(
            "| {name} | {kind} | {group} | {u_a:.6f} | {u_b:.6f} | {u_ood:.6f} | {u_ood_vis:.6f} | "
            "{u_ood_sensor:.6f} | {u_c:.6f} | {U:.6f} | {U_base:.6f} | {U_pass:.6f} | {verdict} | {review} |".format(
                name=r["name"], kind=r["kind"], group=r["group"],
                u_a=r["u_a"], u_b=r["u_b"], u_ood=r["u_ood"],
                u_ood_vis=r["u_ood_vis"], u_ood_sensor=r["u_ood_sensor"],
                u_c=r["u_c"], U=r["U"], U_base=r["U_base"], U_pass=r["U_pass"],
                verdict=r["verdict"], review=("是" if r["review"] else "否"),
            )
        )
    return "\n".join(lines)


def check_invariant(rows: List[Dict[str, object]]) -> Tuple[int, List[str]]:
    """自检不变式：OOD 不得让"明显超差"变成 pass；u_ood 增大不得放宽结论

    Returns:
        (违反次数, 说明列表)
    """
    rank = {"pass": 0, "indeterminate": 1, "fail": 2}
    by_sample: Dict[str, List[Dict[str, object]]] = {}
    for r in rows:
        by_sample.setdefault(str(r["name"]), []).append(r)

    violations: List[str] = []
    notes: List[str] = []
    for name, rs in by_sample.items():
        g0 = next((r for r in rs if r["group"] == "G0"), None)
        if g0 is None:
            continue
        l3 = [r for r in rs if r["group"] != "G0"]
        # 不变式 1：G0 判 fail 的样本，任何 L3 组都必须是 fail
        if g0["verdict"] == "fail":
            for r in l3:
                if rank[str(r["verdict"])] < rank["fail"]:
                    violations.append(
                        "%s: G0 判 fail，但 %s 判 %s（OOD 放宽了验收！）"
                        % (name, r["group"], r["verdict"])
                    )
        # 不变式 2：L3 组结论不得比 G0 更宽松
        for r in l3:
            if rank[str(r["verdict"])] < rank[str(g0["verdict"])]:
                violations.append(
                    "%s: %s(%s) 比 G0(%s) 更宽松" % (name, r["group"], r["verdict"], g0["verdict"])
                )
        # 记录 G0 → L3 的结论变化（不作为违反）
        if g0["verdict"] != "fail":
            changed = [r for r in l3 if r["verdict"] != g0["verdict"]]
            if changed:
                notes.append(
                    "%s: G0=%s → %s" % (
                        name, g0["verdict"],
                        "/".join("%s=%s" % (r["group"], r["verdict"]) for r in changed),
                    )
                )
    return len(violations), violations + notes


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="OOD×GUM 消融实验（P2-25）")
    ap.add_argument("--csv", help="真实样本集 CSV（缺省用内置合成样本）")
    ap.add_argument("--calibrated-beta", type=float, default=None,
                    help="已标定的 beta_vis（缺省用 YAML 中的值）")
    ap.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="equis_l3.yaml 路径")
    ap.add_argument("--out", default=None, help="把 Markdown 表写入该文件（如 agent/reports/ablation_ood_gum.md）")
    ap.add_argument("--csv-out", default=None, help="额外输出明细 CSV（如 agent/reports/ablation_ood_gum.csv）")
    args = ap.parse_args(argv)

    cfg = load_l3_config(args.config)
    beta_vis = args.calibrated_beta if args.calibrated_beta is not None else float(cfg.get("beta_vis", 0.001))

    if args.csv:
        samples = read_samples_csv(args.csv)
        source_note = "**实测样本 CSV**：%s（%d 个样本）" % (args.csv, len(samples))
    else:
        samples = synthetic_samples()
        source_note = ("**合成数据（占位）**：6 个程序生成样本（固定种子 20260609），"
                       "仅用于让表有内容可看与回归自检；**真实结论必须用实测样本重跑**")

    rows: List[Dict[str, object]] = []
    for s in samples:
        for group, include, mapping, _desc in GROUPS:
            r = run_group(s, include, mapping, cfg, beta_vis)
            r.update({"name": s["name"], "kind": s["kind"], "group": group})
            rows.append(r)

    table = build_table(rows, beta_vis, source_note)
    print(table)
    print()

    # ---- 关键数字对照（u_b 修正前后）----
    print("### 关键数字对照")
    print()
    print("- 热膨胀系数 α：改造前 0.0001 /°C（注释写『钢』）→ 现在 %.6g /°C（material=%s）"
          % (float(cfg.get("temp_coefficient", 0.0)), cfg.get("material")))
    print("- 100 mm / ΔT=5 K 下 u_b：改造前 0.02886751 mm → 现在 %.8f mm" % rows[0]["u_b"])
    print("- 说明：u_b 下降约 %.1f 倍，A 类分量重新成为主导，U 不再是量具精度的 5.9 倍"
          % (0.02886751 / rows[0]["u_b"] if rows[0]["u_b"] else float("nan")))
    print()

    print("### OOD 是否放宽过验收（自检）")
    print()
    n_viol, msgs = check_invariant(rows)
    if n_viol == 0:
        print("- ✅ 未发现『OOD 放宽验收』：所有 G0 判 fail 的样本，L3 各组仍判 fail")
    else:
        print("- ❌ 发现 %d 处不变式违反：" % n_viol)
        for m in msgs:
            print("  - " + m)
    notes = [m for m in msgs if "更宽松" not in m and "放宽了验收" not in m]
    if notes:
        print("- 结论变化明细（G0 → L3）：")
        for m in notes:
            print("  - " + m)
    print()

    # ---- 落盘 ----
    if args.out:
        out_dir = os.path.dirname(os.path.abspath(args.out))
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)
        body = [table, "", "### 关键数字对照", "",
                "- α: %.6g /°C (material=%s)" % (float(cfg.get("temp_coefficient", 0.0)), cfg.get("material")),
                "- u_b @100mm,ΔT=5K: 0.02886751 → %.8f mm" % rows[0]["u_b"],
                "- beta_vis = %.6g mm/score_unit" % beta_vis,
                "- 不变式自检违反次数: %d" % n_viol,
                ""
                ]
        with open(args.out, "w", encoding="utf-8") as f:
            f.write("\n".join(body))
        print("[ok] Markdown 表已写入: " + os.path.abspath(args.out))

    if args.csv_out:
        out_dir = os.path.dirname(os.path.abspath(args.csv_out))
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)
        cols = ["name", "kind", "group", "deviation", "u_a", "u_b", "u_ood",
                "u_ood_vis", "u_ood_sensor", "u_c", "U", "U_base", "U_pass",
                "verdict", "review", "confidence"]
        with open(args.csv_out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in rows:
                w.writerow([r.get(c) for c in cols])
        print("[ok] 明细 CSV 已写入: " + os.path.abspath(args.csv_out))

    return 1 if n_viol else 0


if __name__ == "__main__":
    raise SystemExit(main())
