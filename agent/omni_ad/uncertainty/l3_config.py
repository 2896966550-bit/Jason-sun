"""L3 配置加载器 — 让 equis_l3.yaml 真正生效（P2-22）

背景（改造前实测）：
    equis_l3.yaml 此前**没有被任何 .py 加载**（按文件名与 key 双向 grep 均无命中），
    其中承诺的 guard_band_factor 在所有 .py 里命中数为 0 ——
    属于"文档里承诺、代码里不存在"的功能。所有参数都硬编码在
    compliance.py 的 dataclass 默认值里。本模块补齐这条链路：

        equis_l3.yaml  →  load_l3_config()  →  build_compliance_input()  →  ComplianceInput
                                                                              ↓
                                                                     compute_compliance()

设计原则：
    1. **缺文件/缺字段都不崩**：任何异常都退回内置默认值，并记录 warning；
    2. **纯标准库 + pyyaml**：不引入新依赖（pyyaml 已在运行环境）；
    3. 本模块只做"读配置 → 造输入"，不含判定逻辑，便于单独测试。

用法::

    from agent.omni_ad.uncertainty.l3_config import build_compliance_input
    inp = build_compliance_input(measured_value=100.0, reference_value=100.0,
                                 sensor_precision=0.01, env_temp=25.0)
    # inp 已带上 YAML 中的 beta_vis / guard_band_factor / material / k ...
"""

import os
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "L3Defaults",
    "L3Config",
    "load_l3_config",
    "build_compliance_input",
    "l3_config_to_dict",
]

try:  # pyyaml 是运行环境既有依赖；缺失时优雅降级为"全默认值"
    import yaml  # type: ignore
except Exception:  # pragma: no cover - 仅在缺依赖环境触发
    yaml = None  # type: ignore


#: 默认配置文件路径：agent/omni_ad/configs/equis_l3.yaml
DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),  # .../omni_ad
    "configs",
    "equis_l3.yaml",
)


@dataclass
class L3Defaults:
    """内置兜底默认值（与 equis_l3.yaml 的出厂值保持一致）

    注意：这里的 beta_vis/beta_sensor 仍是**未标定占位值**。
    真实取值必须由 agent/scripts/calibrate_beta.py 用受控样本标定后回填
    （见该脚本 docstring 的物理锚点法步骤）。
    """
    # L3 模式开关
    include_u_ood: bool = True
    # Step 1 异常量纲映射
    a_mapping: str = "hard"
    visual_threshold: float = -4.0
    sensor_threshold: float = 2.8      # sqrt(chi2_0.95(3))=sqrt(7.81)≈2.8
    # Step 2 beta 折算系数 mm/score_unit
    beta_vis: float = 0.001
    beta_sensor: float = 0.001
    # Step 3 B 类：环境温度（P2-21：钢 11.5e-6，原 1e-4 大了约 8.7 倍）
    baseline_temp: float = 20.0
    temp_coefficient: Optional[float] = None   # None = 由 material 决定
    material: str = "steel"
    k: float = 2.0
    # Step 4 保护带
    guard_band_factor: float = 1.0
    # 三态判定阈值（P2-24）
    critical_band_low: float = 0.8
    critical_band_high: float = 1.2
    ood_dominance_ratio: float = 1.5
    # 传感器异常基线（P2-23：占位默认值，需标定脚本用健康样本重标）
    sensor_baseline: Dict[str, float] = field(default_factory=lambda: {
        "t_mean": 25.0, "t_std": 1.5,
        "h_mean": 50.0, "h_std": 8.0,
        "vib_mean": 9.81, "vib_std": 0.5,
    })
    # 3x3 逆协方差矩阵；None = 按特征独立假设（对角）
    sensor_inv_cov: Optional[List[List[float]]] = None


@dataclass
class L3Config:
    """解析后的 L3 配置（含来源与告警，便于报告里说明"哪些是真读到的"）"""
    values: Dict[str, Any] = field(default_factory=dict)
    path: str = ""
    loaded: bool = False                 # YAML 是否成功读到
    warnings: List[str] = field(default_factory=list)

    # ---- 便捷 dot 访问 ----
    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def __contains__(self, key: str) -> bool:
        return key in self.values

    # ---- 派生量 ----
    @property
    def sensor_baseline(self) -> Dict[str, float]:
        b = dict(L3Defaults().sensor_baseline)
        b.update(self.values.get("sensor_baseline") or {})
        return b

    @property
    def sensor_inv_cov(self) -> Optional[List[List[float]]]:
        return self.values.get("sensor_inv_cov")

    def to_compliance_kwargs(self) -> Dict[str, Any]:
        """返回可直接喂给 ComplianceInput(...) 的关键字参数（不含测量值/精度）"""
        from .compliance import ComplianceInput  # 延迟导入，避免循环依赖

        allowed = {f.name for f in ComplianceInput.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return {k: v for k, v in self.values.items() if k in allowed}


def load_l3_config(path: Optional[str] = None) -> L3Config:
    """加载 equis_l3.yaml，带完整默认值兜底（P2-22）

    容错行为（均有测试覆盖）：
        - 文件不存在        → 全默认值 + warning，不抛异常
        - pyyaml 不可用     → 全默认值 + warning，不抛异常
        - YAML 语法错误     → 全默认值 + warning，不抛异常
        - 顶层不是 dict     → 全默认值 + warning，不抛异常
        - 某个字段缺失      → 该字段用默认值（逐字段兜底）
        - 字段类型明显不对  → 尝试类型转换，失败则该字段用默认值 + warning

    Args:
        path: YAML 路径；None 用 DEFAULT_CONFIG_PATH

    Returns:
        L3Config（values 一定是"完整可用"的字段全集）
    """
    cfg_path = path or DEFAULT_CONFIG_PATH
    defaults = asdict(L3Defaults())
    values: Dict[str, Any] = dict(defaults)
    warnings: List[str] = []
    loaded = False

    if yaml is None:
        warnings.append("pyyaml 不可用，全部使用内置默认值")
    elif not os.path.exists(cfg_path):
        warnings.append("配置文件不存在，全部使用内置默认值: " + str(cfg_path))
    else:
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            if isinstance(raw, dict):
                loaded = True
                for key, val in raw.items():
                    if key not in defaults:
                        warnings.append("忽略未知配置项: " + str(key))
                        continue
                    if val is None:
                        warnings.append("配置项为空，使用默认值: " + str(key))
                        continue
                    coerced = _coerce(key, val, defaults[key], warnings)
                    if coerced is not _UNSET:
                        values[key] = coerced
                # 逐字段补齐（YAML 里没写的键保持默认值）
                missing = [k for k in defaults if k not in raw]
                if missing:
                    warnings.append("以下配置项缺失，已用默认值补齐: " + ", ".join(sorted(missing)))
            else:
                warnings.append("YAML 顶层不是映射（dict），全部使用内置默认值")
        except Exception as exc:  # 解析失败不应阻断判定
            warnings.append("配置文件解析失败（%s），全部使用内置默认值" % type(exc).__name__)

    # 派生字段：material → temp_coefficient
    if values.get("temp_coefficient") is None:
        values["temp_coefficient"] = _resolve_alpha(values.get("material"))

    return L3Config(values=values, path=cfg_path, loaded=loaded, warnings=warnings)


_UNSET = object()


def _resolve_alpha(material: Any) -> float:
    """material → α(1/°C)；未知材料退回钢并保留可读提示"""
    from .compliance import DEFAULT_MATERIAL, MATERIAL_TEMP_COEFFICIENTS

    key = str(material or DEFAULT_MATERIAL).strip().lower()
    if key in MATERIAL_TEMP_COEFFICIENTS:
        return MATERIAL_TEMP_COEFFICIENTS[key]
    return MATERIAL_TEMP_COEFFICIENTS[DEFAULT_MATERIAL]


def _coerce(key: str, val: Any, ref: Any, warnings: List[str]) -> Any:
    """按默认值的类型做最小转换；无法转换时返回 _UNSET（保持默认值）"""
    try:
        if isinstance(ref, bool):
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                return val.strip().lower() in ("1", "true", "yes", "on", "是")
            return bool(val)
        if isinstance(ref, float) and not isinstance(ref, bool):
            return float(val)
        if isinstance(ref, int) and not isinstance(ref, bool):
            return int(val)
        if isinstance(ref, str):
            return str(val)
        if isinstance(ref, dict):
            if not isinstance(val, dict):
                warnings.append("配置项类型异常，使用默认值: " + key)
                return _UNSET
            merged = dict(ref)
            merged.update(val)
            return merged
        if isinstance(ref, list):
            if not isinstance(val, list):
                warnings.append("配置项类型异常，使用默认值: " + key)
                return _UNSET
            return val
        return val
    except (TypeError, ValueError):
        warnings.append("配置项无法转换为期望类型，使用默认值: " + key)
        return _UNSET


def build_compliance_input(
    measured_value: float,
    reference_value: float,
    sensor_precision: float,
    env_temp: float = 25.0,
    config: Optional[L3Config] = None,
    config_path: Optional[str] = None,
    **overrides: Any,
):
    """按 YAML 配置构造 ComplianceInput（P2-22 的主用法）

    YAML 提供"全局策略参数"（beta、guard_band_factor、material、k、阈值…），
    调用方只传"本次测量"的量（实测值/参考值/传感器精度/环境温度），
    显式 overrides 优先级最高。

    Args:
        measured_value: 实测值 mm
        reference_value: 参考值 mm
        sensor_precision: 传感器精度（MPE）mm
        env_temp: 环境温度 °C
        config: 已加载的 L3Config（None 则按 config_path 加载）
        config_path: YAML 路径（None 用 DEFAULT_CONFIG_PATH）
        **overrides: 覆盖 YAML 的任意 ComplianceInput 字段（如 include_u_ood=False 跑 G0）

    Returns:
        ComplianceInput: 可直接调用 compute_compliance()
    """
    from .compliance import ComplianceInput

    cfg = config if config is not None else load_l3_config(config_path)
    kwargs = cfg.to_compliance_kwargs()
    kwargs.update(overrides)

    return ComplianceInput(
        measured_value=measured_value,
        reference_value=reference_value,
        sensor_precision=sensor_precision,
        env_temp=env_temp,
        **kwargs,
    )


def l3_config_to_dict(cfg: L3Config) -> Dict[str, Any]:
    """导出为可打印 dict（报告/调试用），过滤 inv_cov 这类大矩阵细节"""
    out = {k: v for k, v in cfg.values.items() if k != "sensor_inv_cov"}
    out["_source_path"] = cfg.path
    out["_loaded_from_yaml"] = cfg.loaded
    out["_warnings"] = list(cfg.warnings)
    out["_has_inv_cov"] = cfg.values.get("sensor_inv_cov") is not None
    return out
