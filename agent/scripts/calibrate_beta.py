"""Legacy threshold-ratio research utility, disabled for deployment.

The -4 cutoff failed deployment validation. A residual/score ratio is not a
standard uncertainty model. No default threshold, implicit demo or YAML writeback
is allowed. Use validate_measurement_dataset.py for the new paired-data contract.
"""
import argparse
import csv
import math
import os
import random
import shutil
import sys
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

# ---- 路径引导：允许 `python agent/scripts/calibrate_beta.py` 直接运行 ----
_THIS = os.path.abspath(__file__)
_AGENT_DIR = os.path.dirname(os.path.dirname(_THIS))          # .../agent
_REPO_ROOT = os.path.dirname(_AGENT_DIR)                       # .../<root>
for _p in (_REPO_ROOT, _AGENT_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent.omni_ad.uncertainty.compliance import (  # noqa: E402
    calibrate_beta_physical_anchor,
)
from agent.omni_ad.uncertainty.l3_config import DEFAULT_CONFIG_PATH  # noqa: E402

# 物理锚点法的参考量级（用于报告中给出"是否落在合理区间"的判断）



# ============================ 数据读取 ============================

_SCORE_KEYS = ("anomaly_score", "score", "异常分数", "ood_score")
_DEV_KEYS = ("physical_deviation_mm", "deviation_mm", "deviation", "delta_mm", "物理偏差", "偏差")


def read_samples_csv(path: str) -> Tuple[List[float], List[float]]:
    """读取 (anomaly_score, physical_deviation_mm) 样本对

    列名容错：见 _SCORE_KEYS / _DEV_KEYS（大小写与常见别名）。
    跳过空行、注释行（# 开头）与无法解析的行（计数并提示）。

    Raises:
        ValueError: 缺少必需列，或有效样本为 0
    """
    scores: List[float] = []
    devs: List[float] = []
    skipped = 0
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(
            line for line in f if line.strip() and not line.lstrip().startswith("#")
        )
        if not reader.fieldnames:
            raise ValueError("CSV 没有表头: " + path)
        lower = {str(name).strip().lower(): name for name in reader.fieldnames}

        def pick(keys: Sequence[str]) -> Optional[str]:
            for k in keys:
                if k.lower() in lower:
                    return lower[k.lower()]
            return None

        s_col, d_col = pick(_SCORE_KEYS), pick(_DEV_KEYS)
        if s_col is None or d_col is None:
            raise ValueError(
                "CSV 缺少必需列。需要 anomaly_score 与 physical_deviation_mm，实际表头: "
                + ", ".join(str(x) for x in reader.fieldnames)
            )
        for row in reader:
            try:
                s = float(str(row[s_col]).strip())
                d = float(str(row[d_col]).strip())
            except (TypeError, ValueError):
                skipped += 1
                continue
            scores.append(s)
            devs.append(d)

    if skipped:
        print("[warn] 跳过 %d 行无法解析的样本" % skipped)
    if not scores:
        raise ValueError("CSV 中没有有效样本: " + path)
    return scores, devs


def demo_samples(n: int = 60, seed: int = 20260609, *, threshold: float) -> Tuple[List[float], List[float]]:
    """生成**合成占位样本**（仅用于跑通脚本，不是真实标定数据）

    构造方式（刻意做得"像真实但明显是合成"）：
        a_i  ~ 均匀分布 [0.2, 6.0]（异常量，score 单位）
        beta_true = 3.5e-3 mm/score_unit（假想真值）
        Δ_i  = beta_true * a_i * lognormal(0, 0.25)   # 乘性噪声，模拟离散度
    另掺入 10% 的正常样本（score < threshold，a=0，会被标定函数自动排除）。
    """
    rng = random.Random(seed)
    beta_true = 3.5e-3
    scores: List[float] = []
    devs: List[float] = []
    for _ in range(n):
        if rng.random() < 0.10:
            # 合成低分样本；仅使用调用方显式给出的研究阈值
            scores.append(rng.uniform(threshold-1.5, threshold-0.05))
            devs.append(abs(rng.gauss(0.0, 0.0004)))
            continue
        a = rng.uniform(0.2, 6.0)
        delta = beta_true * a * math.exp(rng.gauss(0.0, 0.25))
        scores.append(threshold + a)
        devs.append(delta)
    return scores, devs


# ============================ 统计量 ============================

def _median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _quantile(xs: Sequence[float], q: float) -> float:
    """线性插值分位数（纯标准库实现，避免引入 numpy）"""
    s = sorted(xs)
    if not s:
        return 0.0
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1.0 - frac) + s[hi] * frac


def _stdev(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def summarize_ratios(
    scores: Sequence[float], devs: Sequence[float], threshold: float
) -> Dict[str, float]:
    """计算 beta 建议值与离散度统计

    Returns:
        dict: beta(中位数) / beta_mean / n_used / n_total / iqr / stdev /
              iqr_over_median / min / max
    """
    ratios = []
    for s, d in zip(scores, devs):
        a = max(0.0, s - threshold)
        if a > 1e-6:
            ratios.append(d / a)
    beta = calibrate_beta_physical_anchor(list(scores), list(devs), threshold, use_median=True)
    beta_mean = calibrate_beta_physical_anchor(list(scores), list(devs), threshold, use_median=False)
    q1, q3 = _quantile(ratios, 0.25), _quantile(ratios, 0.75)
    med = _median(ratios)
    return {
        "beta": beta,
        "beta_mean": beta_mean,
        "n_total": float(len(scores)),
        "n_used": float(len(ratios)),
        "iqr": q3 - q1,
        "stdev": _stdev(ratios),
        "iqr_over_median": (q3 - q1) / med if med else float("inf"),
        "min": min(ratios) if ratios else 0.0,
        "max": max(ratios) if ratios else 0.0,
    }


# ============================ YAML 写回 ============================

def write_beta_to_yaml(
    beta: float, path: str = DEFAULT_CONFIG_PATH, key: str = "beta_vis", dry_run: bool = False
) -> Optional[str]:
    """Deployment writeback remains disabled until independent physical validation."""
    raise RuntimeError("旧beta写回已关闭：需真实配对数据、基础预算、独立覆盖验证；不得直接写入部署配置")


def _fmt_beta(beta: float) -> str:
    """用可读形式输出 beta（避免 0.0034999999 这类噪音）"""
    if beta == 0:
        return "0.0"
    exp = math.floor(math.log10(abs(beta)))
    mant = beta / (10 ** exp)
    return "%.2fe%d" % (mant, exp)


# ============================ 主流程 ============================

def main(argv: Optional[Sequence[str]] = None) -> int:
    import json
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--research-only", action="store_true")
    src=ap.add_mutually_exclusive_group()
    src.add_argument("--csv");src.add_argument("--demo",action="store_true")
    ap.add_argument("--threshold",type=float)
    ap.add_argument("--write",action="store_true")
    ap.add_argument("--yaml");ap.add_argument("--key",default="beta_vis")
    args=ap.parse_args(argv)
    if args.write:ap.error("旧beta部署写回已关闭；不会读取或修改YAML")
    if not args.research_only:ap.error("旧比值法仅保留研究，必须显式指定 --research-only")
    if args.threshold is None or not math.isfinite(args.threshold):ap.error("必须显式提供经研究定义的有限阈值；没有默认-4")
    if not args.csv and not args.demo:ap.error("必须显式提供 --csv 或 --demo，不自动生成样本")
    scores,devs=read_samples_csv(args.csv) if args.csv else demo_samples(threshold=args.threshold)
    if not all(math.isfinite(v) for v in scores+devs):ap.error("样本含非有限数值")
    statistics=summarize_ratios(scores,devs,args.threshold)
    statistics={k:v if math.isfinite(v) else None for k,v in statistics.items()}
    print(json.dumps({"scope":"legacy_ratio_research_only","synthetic":bool(args.demo),
                      "standard_uncertainty_validated":False,"deployment_writeback":False,
                      "statistics":statistics},ensure_ascii=False,allow_nan=False,indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
