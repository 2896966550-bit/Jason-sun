"""工程一致性评定与实验性 OOD 风险门控。

这里没有产品公差输入，不能据此判定工件合格。OOD 毫米映射尚未物理
验证；保留已有计算以供研究复现，不将其称为已验证的 B 类分量。

核心创新（"创新①"）：
    Step 1: 异常量纲映射 a = map(score, threshold)
    Step 2: 多源方和根合成 u_ood = sqrt((beta_vis*a_vis)^2 + (beta_sensor*a_sensor)^2)
    Step 3: GUM 扩展合成 u_c = sqrt(u_a^2 + u_b^2 + u_ood^2), U = k*u_c
    Step 4: 三态合格判定（pass / fail / indeterminate，GB/T 18779.1 思路）

措辞纪律：本模块是对 GUM 框架的扩展应用，不是"符合 GUM"。
OOD 分数是测量上下文的信任信号，不是经典误差源。

设计要点（P2-24）：
    OOD 分量增大 **只允许** 让结论变得更保守或标注不确定，
    绝不允许把"超差(fail)"翻成"合格(pass)"。
    实现方式：fail 由"可信基线不确定度" U_base（仅 u_a、u_b）的 80% 硬边界锁定，
    扩大的 u_ood 只可能把 pass 降级为 indeterminate。
    不变式由 compute_verdict() 的结构与 test_compliance.py 共同锁定。

本模块保持纯标准库（math / dataclasses / typing），可独立导入运行。
"""

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


class MeasurementInputError(ValueError):
    """An execution failure, separate from a valid measurement verdict."""

    def __init__(self, field: str, message: str, *, status: str = "invalid_input",
                 code: str = "invalid_measurement_input") -> None:
        super().__init__(message)
        self.field, self.status, self.code = field, status, code

    def to_dict(self) -> dict:
        return {"execution_status": self.status, "verdict": None, "in_spec": None,
                "confidence": None, "requires_human_review": True,
                "error": {"code": self.code, "field": self.field, "message": str(self)}}


def _number(field: str, value, *, minimum: Optional[float] = None,
            positive: bool = False) -> float:
    if value is None:
        raise MeasurementInputError(field, "缺少参数: " + field, status="needs_input")
    if isinstance(value, (bool, str, bytes)):
        raise MeasurementInputError(field, "参数必须是带明确单位的数值: " + field)
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise MeasurementInputError(field, "参数不是有效数值: " + field) from None
    if not math.isfinite(result):
        raise MeasurementInputError(field, "参数或派生结果必须为有限数值: " + field)
    if (minimum is not None and result < minimum) or (positive and result <= 0):
        raise MeasurementInputError(field, "参数不在允许范围内: " + field)
    return result


def _policy(guard_band_factor, critical_band_low, critical_band_high,
            ood_dominance_ratio, k, max_ood_inflation=10.0):
    gbf = _number("guard_band_factor", guard_band_factor, positive=True)
    low = _number("critical_band_low", critical_band_low, positive=True)
    high = _number("critical_band_high", critical_band_high, positive=True)
    ratio = _number("ood_dominance_ratio", ood_dominance_ratio, minimum=1.0)
    factor = _number("k", k, positive=True)
    cap = _number("max_ood_inflation", max_ood_inflation, positive=True)
    if high <= low:
        raise MeasurementInputError("critical_band_high", "临界带上界必须大于下界")
    if ratio >= cap:
        raise MeasurementInputError("ood_dominance_ratio", "OOD 主导阈值必须小于膨胀上限")
    return gbf, low, high, ratio, factor, cap


# ========== 材料线膨胀系数表（P2-21）==========
#
# 量纲：1/°C（线膨胀系数 α，20 °C 附近的名义值）。
# 用途：B 类不确定度分量 u_b = L * α * ΔT / sqrt(3)（矩形分布假设）。
#
# 来源（均为工程手册常用名义值，非检定证书值）：
#   - 钢（碳钢/合金钢）：约 11.5e-6 /°C（GB/T 4339 与常见材料手册给出的
#     20~100 °C 区间的量级；不锈钢略高，约 16~17e-6）
#   - 铝（铝合金）：约 23e-6 /°C
#   - 殷钢 / 因瓦合金（Fe-36Ni）：约 1.2e-6 /°C，量具与标准器的低膨胀材料
#   - 铜：约 16.5e-6 /°C；不锈钢 304：约 17.3e-6 /°C
#
# 计量提示：真实计量场景应以校准证书 / 材料证明书给出的 α 为准；
# 本表只用于缺省量级与量纲正确性，不作为溯源依据。
MATERIAL_TEMP_COEFFICIENTS: Dict[str, float] = {
    "steel": 11.5e-6,          # 钢（默认）
    "aluminum": 23e-6,         # 铝 / 铝合金
    "invar": 1.2e-6,           # 殷钢（因瓦合金，低膨胀）
    "copper": 16.5e-6,         # 铜
    "stainless_steel": 17.3e-6,  # 不锈钢（304）
}
DEFAULT_MATERIAL = "steel"


def resolve_temp_coefficient(
    material: Optional[str] = None,
    temp_coefficient: Optional[float] = None,
) -> float:
    """解析热膨胀系数 α（1/°C）。

    优先级：显式 temp_coefficient > material 查表 > DEFAULT_MATERIAL（钢）。

    为什么需要这个函数：
        钢的线膨胀系数约 11.5e-6 /°C，而改造前的默认值是 1e-4 /°C（大了约 8.7 倍），
        导致 25 °C 环境、100 mm 量程下 u_b=0.0289 mm —— 比"精度 0.01 mm"的量具
        本身还大 2.9 倍，U 达到量具精度的 5.9 倍，属明显量纲/量级错误。

    Args:
        material: 材料名（键见 MATERIAL_TEMP_COEFFICIENTS），大小写不敏感，
            支持 "steel"/"钢" 这类别名（见 _MATERIAL_ALIASES）。
        temp_coefficient: 显式 α 值（1/°C）。给定时直接采用，不做材料覆盖。

    Returns:
        float: α，单位 1/°C。

    Raises:
        ValueError: material 非空且不在表中（不静默兜底，避免"以为用了 invar"）。
    """
    if temp_coefficient is not None:
        return _number("temp_coefficient", temp_coefficient, minimum=0.0)
    if material is not None and not isinstance(material, str):
        raise MeasurementInputError("material", "材料名必须是字符串")
    key = (material or DEFAULT_MATERIAL).strip().lower()
    key = _MATERIAL_ALIASES.get(key, key)
    if key not in MATERIAL_TEMP_COEFFICIENTS:
        raise MeasurementInputError(
            "material", "Unknown material: " + str(material)
            + ". Known: " + ", ".join(sorted(MATERIAL_TEMP_COEFFICIENTS))
        )
    return MATERIAL_TEMP_COEFFICIENTS[key]


_MATERIAL_ALIASES: Dict[str, str] = {
    "钢": "steel",
    "碳钢": "steel",
    "碳素钢": "steel",
    "铝": "aluminum",
    "铝合金": "aluminum",
    "铝材": "aluminum",
    "殷钢": "invar",
    "因瓦": "invar",
    "因瓦合金": "invar",
    "铜": "copper",
    "不锈钢": "stainless_steel",
    "304": "stainless_steel",
}


# ========== 判定阈值常量（P2-24）==========

#: 临界带下界比例：|d| > 该比例 * U_effective 时进入"不能确定"候选区
CRITICAL_BAND_LOW = 0.8
#: 临界带上界比例：|d| > 该比例 * U_base 时直接判 fail（可信基线的硬边界）
CRITICAL_BAND_HIGH = 1.2
#: OOD 主导阈值：U_effective / U_base 超过该比例即认为 OOD 显著放大不确定度
OOD_DOMINANCE_RATIO = 1.5
#: OOD 膨胀上限：U 相对 U_base 的最大允许放大倍数（防止未标定的 beta 支配结论）
MAX_OOD_INFLATION = 10.0
#: 结论取值集合
VERDICTS: Tuple[str, str, str] = ("pass", "fail", "indeterminate")


# ========== 数据结构 ==========

@dataclass
class ComplianceInput:
    """合规判定输入参数

    向后兼容说明（P2-21）：
        所有字段原有名字、顺序、语义均未改变，新增字段一律追加在末尾。
        现有调用方（coordinator.py / tools/measurement_tools.py）按关键字传参，
        不受影响。`material` 为新增可选字段。

    热膨胀系数语义（P2-21）：
        temp_coefficient 的默认值是钢的 11.5e-6 /°C（旧值为 1e-4，大了约 8.7 倍）。
        - 只写 material="invar" → α 取材料表值；
        - 只写 temp_coefficient=1.6e-5 → 用显式值（优先于 material）；
        - 两者都不写 → 钢 11.5e-6。
        注：dataclass 字段默认值为 None 是为了区分"未赋值"与"显式给值"，
        __post_init__ 会把它解析成 float，构造完成后该字段恒为 1/°C 的浮点数。
    """
    # 测量值
    measured_value: float           # 实测值 mm
    reference_value: Optional[float] # 参考值 mm；仅估计不确定度时可为 None
    # 器具误差限分量（B 类；保留历史字段名 u_a）
    sensor_precision: float        # 传感器精度（最大允许误差）mm
    # B 类：环境温度影响
    env_temp: float = 25.0          # 环境温度 摄氏度
    baseline_temp: float = 20.0    # 基准温度 摄氏度（JJF1059 默认 20C）
    temp_coefficient: Optional[float] = None  # 热膨胀系数 1/°C；None=按 material 解析（默认钢 11.5e-6）
    # L3 新增：OOD 分量
    visual_anomaly_score: Optional[float] = None   # 视觉异常分数（Omni-AD 输出）
    sensor_anomaly_score: Optional[float] = None   # 传感器异常分数（马氏距离等）
    # 异常阈值
    visual_threshold: float = -4.0    # 视觉异常阈值（Omni-AD 默认 -4.0）
    sensor_threshold: float = 0.0     # 传感器异常阈值（马氏距离默认 0）
    # beta 折算系数 mm/score_unit （Phase 1 P1-5 标定）
    beta_vis: float = 0.001           # 视觉 beta：物理锚点标定后的默认值
    beta_sensor: float = 0.001        # 传感器 beta：物理锚点标定后的默认值
    # 模式选择
    a_mapping: str = "hard"           # "hard" (ReLU) 或 "softplus"
    include_u_ood: bool = True        # True=L3, False=G0 baseline
    # 扩展不确定度
    k: float = 2.0                    # 包含因子（k=2, ~95% 置信）
    # ---- 以下为 P2-21/P2-22/P2-24 新增（追加在末尾，保持向后兼容）----
    material: Optional[str] = None                # 材料名（steel/aluminum/invar/...），见 MATERIAL_TEMP_COEFFICIENTS
    guard_band_factor: float = 1.0                # U_effective = guard_band_factor * U（<1 更严格）
    critical_band_low: float = CRITICAL_BAND_LOW  # 临界带下界比例（相对 U_effective）
    critical_band_high: float = CRITICAL_BAND_HIGH  # 硬 fail 比例（相对 U_base）
    ood_dominance_ratio: float = OOD_DOMINANCE_RATIO  # OOD 主导阈值（相对 U_base）
    assessment_type: str = "reference_consistency"  # 或 uncertainty_only；未实现产品公差评定

    def __post_init__(self) -> None:
        # 解析材料/显式值 → 恒定 float，避免下游读到 None
        self.temp_coefficient = resolve_temp_coefficient(
            self.material, self.temp_coefficient
        )


@dataclass
class ComplianceResult:
    """合规判定结果

    向后兼容说明（P2-22/P2-24）：
        原有字段名字、顺序、语义完全不变（前端 compliance-card 依赖这些键），
        新字段一律追加在末尾且有默认值，因此 `ComplianceResult(...)` 的
        关键字构造与字段访问均不受影响。
    """
    # 器具分量（B 类，历史名 u_a）
    u_a: float                        # 传感器精度分量 mm
    # B 类
    u_b: float                        # 温度影响分量 mm
    # OOD 分量（L3 新增）
    a_vis: float                      # 视觉异常量
    a_sensor: float                   # 传感器异常量
    u_ood_vis: float                  # 视觉 OOD 分量 mm
    u_ood_sensor: float               # 传感器 OOD 分量 mm
    u_ood: float                      # 合成 OOD 分量 mm
    # 合成不确定度
    u_c: float                        # 合成标准不确定度 mm
    U: float                          # 扩展不确定度 mm
    # 判定结果
    deviation: Optional[float]        # 仅估计不确定度时无参考差值
    in_spec: Optional[bool]           # 遗留边界标志，不可覆盖有效三态 verdict
    # 元数据
    mode: str                         # "L1" 或 "L3"
    a_mapping: str                    # "hard" 或 "softplus"
    # ---- 以下为 P2-22/P2-24 新增（追加在末尾，向后兼容）----
    guard_band_factor: float = 1.0    # 保护带系数
    U_effective: float = 0.0          # 名义保护带扩展不确定度 = guard_band_factor * U（U 已按上限截断）
    u_c_base: float = 0.0             # 仅 u_a+u_b 的基线合成标准不确定度 mm
    U_base: float = 0.0               # k * u_c_base（OOD 膨胀前的可信基线）mm
    U_pass: float = 0.0               # 合格门限 = guard_band_factor * U_base（不被 OOD 抬高）
    verdict: Optional[str] = "pass"   # 仅估计不确定度时为 None
    requires_human_review: bool = False  # 是否需要人工复核（HITL）
    confidence: Optional[float] = 1.0 # 启发式提示，不是统计覆盖概率
    u_ood_dominant: bool = False      # u_ood 是否显著放大不确定度（OOD 主导）
    ood_capped: bool = False          # u_ood 导致的膨胀是否被 MAX_OOD_INFLATION 截断
    u_c_raw: float = 0.0              # 未截断的三源合成值，便于对照（u_c 为判定实际使用值）
    reason: str = ""                  # 中文可读结论依据
    execution_status: str = "success"
    assessment_type: str = "reference_consistency"
    rule_version: str = "legacy_consistency_v1"


# ========== Step 1: 异常量纲映射 ==========

def map_anomaly(anomaly_score: float, threshold: float, mode: str = "hard") -> float:
    """Step 1: 异常量纲映射

    将异常分数映射为非负的"异常量" a。
    正常时 a=0，异常时 a>0。

    两种模式（对应消融 G2a/G2b）：
    - "hard" (ReLU): a = max(0, score - threshold)
      优点：简单、正常时严格为 0
      缺点：threshold 处不可导，F1 微调可能不稳
    - "softplus": a = log(1 + exp(score - threshold))
      优点：处处连续可导，适合 F1 微调
      缺点：正常时 a 略大于 0（数值很小，可忽略）

    符号约定：
      - anomaly_score: 分数越高越异常（Omni-AD 约定）
      - threshold: 分数高于此值判定为异常

    Note:
        规划文档原公式为 a = max(0, thr - ood_score)，假设"分数高=正常"。
        本实现采用等价形式 a = max(0, score - threshold)，
        与代码中 anomaly_score = 1 - cos_sim/tau 的"分数高=异常"约定一致。

    数值细节：softplus 在 x>30 时直接返回 x（log(1+e^x)≈x，避免 exp 溢出），
    在 x<-30 时返回 0.0（e^x 下溢，相对误差 <1e-13）。
    """
    anomaly_score = _number("anomaly_score", anomaly_score)
    threshold = _number("threshold", threshold)
    x = _number("score_minus_threshold", anomaly_score - threshold)
    if mode == "hard":
        return max(0.0, x)
    elif mode == "softplus":
        # 数值稳定的 softplus: log(1 + exp(x))
        if x > 30:
            # exp(30) 很大，log(1+exp(30)) 约等于 x，避免溢出
            return x
        elif x < -30:
            # exp(-30) 很小，log(1+exp(-30)) 约等于 0
            return 0.0
        else:
            return math.log1p(math.exp(x))
    else:
        raise ValueError("Unknown a_mapping mode: " + mode + ". Expected 'hard' or 'softplus'.")


# ========== Step 2: 多源方和根合成 ==========

def compute_u_ood(
    a_vis: float,
    a_sensor: float,
    beta_vis: float,
    beta_sensor: float,
) -> Tuple[float, float, float]:
    """Step 2: 多源方和根合成 u_ood

    将视觉异常量和传感器异常量折算为不确定度分量，并按方和根合成。
    假设视觉异常与传感器异常独立不相关（协方差为 0）。

    公式：u_ood = sqrt((beta_vis * a_vis)^2 + (beta_sensor * a_sensor)^2)

    量纲：beta_vis [mm/score_unit] × a_vis [score_unit] = u_ood_vis [mm]。
    """
    a_vis = _number("a_vis", a_vis, minimum=0.0)
    a_sensor = _number("a_sensor", a_sensor, minimum=0.0)
    beta_vis = _number("beta_vis", beta_vis, minimum=0.0)
    beta_sensor = _number("beta_sensor", beta_sensor, minimum=0.0)
    u_ood_vis = _number("u_ood_vis", beta_vis * a_vis)
    u_ood_sensor = _number("u_ood_sensor", beta_sensor * a_sensor)
    u_ood = _number("u_ood", math.hypot(u_ood_vis, u_ood_sensor))
    return u_ood_vis, u_ood_sensor, u_ood


# ========== Step 1b: 传感器异常量（P2-23）==========

#: 传感器基线占位默认值（t °C, h %RH, 振动模 |a| m/s²）
#: 仅用于让"传感器通道"能跑通；真实部署必须由标定脚本用健康样本重标。
DEFAULT_SENSOR_BASELINE: Dict[str, float] = {
    "t_mean": 25.0, "t_std": 1.5,
    "h_mean": 50.0, "h_std": 8.0,
    "vib_mean": 9.81, "vib_std": 0.5,
}


def sensor_anomaly_from_readings(
    t: float,
    h: float,
    ax: float,
    ay: float,
    az: float,
    baseline: Optional[Dict[str, float]] = None,
    inv_cov: Optional[List[List[float]]] = None,
) -> float:
    """从传感器原始读数计算马氏距离式异常量（P2-23）

    为什么需要它：
        ComplianceInput.sensor_anomaly_score 在改造前从未被赋值，
        导致 a_sensor 恒为 0、beta_sensor 恒不生效 ——
        "多源方和根合成"实际只有视觉一源。本函数补齐传感器侧的入口。

    数学形式（标准化空间中的平方和开方 = 对角协方差下的马氏距离）：
        z_k    = (x_k - mean_k) / std_k          # 无量纲标准化残差
        d2     = Σ_k z_k²                        # inv_cov=None 时的对角形式
        d2     = z^T · inv_cov · z               # 给定 inv_cov 时的完整马氏距离²
        score  = sqrt(d2)                        # 无量纲

    特征顺序固定为 (t, h, |a|)，其中 |a| = sqrt(ax²+ay²+az²) 为振动模。
    特征量纲：t [°C]、h [%RH]、ax/ay/az [m/s²]；标准化后 z 无量纲，
    因此 score 也无量纲（单位：标准化单位 / score_unit）。

    Args:
        t: 温度读数 °C
        h: 湿度读数 %RH
        ax, ay, az: 三轴加速度读数 m/s²（模长用于振动特征）
        baseline: 基线统计量，键 t_mean/t_std/h_mean/h_std/vib_mean/vib_std；
            缺键或 None 时用 DEFAULT_SENSOR_BASELINE 补齐。
            **基线需由标定脚本给出，当前为占位默认值。**
        inv_cov: 3×3 逆协方差矩阵（行优先）。None 时按"特征独立"假设
            退化为标准化残差平方和。

    Returns:
        float: 非负异常量 d（无量纲，标准化单位）。健康样本接近 0，
        异常样本显著增大。

    Note:
        - **独立假设**：inv_cov=None 等价于假设 t/h/振动互相独立且方差为
          std_k²。若三轴或温湿度存在相关，请传入由标定得到的 inv_cov。
        - 阈值建议：3 特征下 d² ~ χ²(3)（健康样本近似），
          d² 的 95% 分位为 7.81 → 对应 d ≈ 2.8，故 sensor_threshold 建议取 2.8。
        - 占位基线的 std 取值偏大（保守），意在避免正常波动被判异常；
          真实标定后应替换。
    """
    b = dict(DEFAULT_SENSOR_BASELINE)
    if baseline:
        for key, val in baseline.items():
            if val is not None:
                b[key] = val

    vib = math.sqrt(ax * ax + ay * ay + az * az)
    feats = (t, h, vib)
    means = (b["t_mean"], b["h_mean"], b["vib_mean"])
    stds = (b["t_std"], b["h_std"], b["vib_std"])

    z = []
    for x, mu, sd in zip(feats, means, stds):
        sd = abs(float(sd))
        if sd <= 1e-12:
            # std 为 0 无法标准化：退化为绝对偏差，避免除零崩溃
            z.append(float(x) - float(mu))
        else:
            z.append((float(x) - float(mu)) / sd)

    if inv_cov is None:
        d2 = sum(zi * zi for zi in z)
    else:
        if len(inv_cov) != 3 or any(len(row) != 3 for row in inv_cov):
            raise ValueError("inv_cov must be a 3x3 matrix (got non-3x3)")
        d2 = 0.0
        for i in range(3):
            for j in range(3):
                d2 += z[i] * float(inv_cov[i][j]) * z[j]

    if d2 < 0.0:  # 数值噪声导致的极小负值
        d2 = 0.0
    return math.sqrt(d2)


# ========== Step 4: 三态判定（P2-24）==========

def _budget_fields(u_c: float, u_c_base: float, guard_band_factor: float,
                   k: float, max_ood_inflation: float = MAX_OOD_INFLATION) -> dict:
    """Shared legacy budget arithmetic; no reference value or decision is fabricated."""
    base = _number("u_c_base", u_c_base, minimum=0.0)
    if base == 0:
        raise MeasurementInputError("u_c_base", "缺少可信的不确定度基线，不能出具有效评定",
                                    status="unavailable", code="missing_uncertainty_baseline")
    raw = _number("u_c", u_c, minimum=base)
    cap = _number("uncertainty_cap", max_ood_inflation * base)
    used = min(raw, cap)
    U_base = _number("U_base", k * base)
    U = _number("U", k * used)
    return {"u_c_used": used, "u_c_raw": raw, "U_base": U_base, "U": U,
            "U_effective": _number("U_effective", guard_band_factor * U),
            "U_pass": _number("U_pass", guard_band_factor * U_base),
            "ood_capped": bool(used < raw - 1e-15)}


def compute_verdict(
    deviation: float,
    u_c: float,
    u_c_base: float,
    guard_band_factor: float = 1.0,
    critical_band_low: float = CRITICAL_BAND_LOW,
    critical_band_high: float = CRITICAL_BAND_HIGH,
    ood_dominance_ratio: float = OOD_DOMINANCE_RATIO,
    u_ood: float = 0.0,
    k: float = 2.0,
    max_ood_inflation: float = MAX_OOD_INFLATION,
) -> dict:
    """三态合格判定（P2-24），GB/T 18779.1 思路的简化实现

    判定规则（d = 偏差，U = k·u_c 含 OOD，U_base = k·u_c_base 不含 OOD）：
        1. |d| > critical_band_high(1.2) * U_base  → fail
           硬边界只用可信基线 U_base。OOD 放大 U 的目的不是放宽验收，
           而是暴露"测量本身不够可信"这一事实。
        2. 否则若 |d| > critical_band_low(0.8) * U_pass  → indeterminate
        3. 否则 → pass（除非被第 4 条降级）
        4. OOD 主导（u_c > ood_dominance_ratio(1.5) * u_c_base）→ 至多 indeterminate，
           **不允许在 fail 之上生效**（否则等于"异常反而变合格"）。

    核心设计：判定限值只锚定"可信基线不确定度" U_base，不被 OOD 抬高。
        U_pass = min( guard_band_factor * U_effective ,  guard_band_factor * U_base )
               = guard_band_factor * U_base        （因为 U_effective >= U_base）
        fail 限值 = critical_band_high * U_base

    为什么必须这样锚定（本轮实测踩到的坑，两个反例）：
        反例 A：判定限值取 0.8·U_effective（随 OOD 一起放大）时，
                Δ=0.012 mm、β 从 0.005 增到 0.01 会从 indeterminate 翻回 pass
                ——"异常变大反而判合格"。
        反例 B：临界带上界取 1.2·U_effective 时，同一 Δ 在 β 从 0.001 增到 0.005
                的过程中同样出现 indeterminate → pass 回退。
        根因：只要 u_ood 出现在"合格门限"里，门限就随 OOD 单调上升，
        等于用未标定的 OOD 分量替被测件放宽验收 —— 这正是 P2-24 要堵的反模式。
        因此 0.8/1.2 两个限值都只由 U_base 构成：OOD 只能收紧结论（pass→
        indeterminate/fail），永远不能放宽。

    单调性不变式（回归测试锁定）：
        固定 deviation/u_c_base 等参数，令 u_ood 单调增大（u_c 随之增大）：
            fail          → 仍是 fail（限值 1.2·U_base 与 u_ood 无关）
            pass          → 仍是 pass（限值 0.8·gbf·U_base 与 u_ood 无关）
            indeterminate → 仍是 indeterminate
            唯一变化：u_c > ood_dominance_ratio·u_c_base 时 pass 降级为 indeterminate
        即 u_ood 增大**永不**使结论变得更宽松，更**永不**把 fail 翻成 pass。

    Args:
        deviation: 偏差 d = 实测值 - 参考值（mm）
        u_c: 含 OOD 的合成标准不确定度（mm）
        u_c_base: 仅 u_a+u_b 的基线合成标准不确定度（mm）
        guard_band_factor: 保护带系数，U_effective = guard_band_factor * U（名义值，
            结果字段中如实给出）；实际判定门限用 guard_band_factor * U_base
        critical_band_low: 临界带下界比例（相对"可信门限" 0.8·gbf·U_base）
        critical_band_high: 硬 fail 比例（相对 U_base）
        ood_dominance_ratio: OOD 主导阈值（相对 U_base）
        u_ood: 合成 OOD 分量（mm），用于生成可读 reason
        k: 包含因子
        max_ood_inflation: OOD 膨胀上限倍数（相对 u_c_base），防止未标定的 beta
            单独支配结论；触顶时 reason 中会显式标注。
        u_c_base=0 时没有可信基线，抛出结构化执行错误，不生成有效 verdict。

    Returns:
        dict: {"verdict", "requires_human_review", "confidence",
               "u_ood_dominant", "reason", "U", "U_base", "U_effective",
               "U_pass", "ood_capped"}
    """
    ad = abs(_number("deviation", deviation))
    u_c_base = _number("u_c_base", u_c_base, minimum=0.0)
    u_ood = _number("u_ood", u_ood, minimum=0.0)
    (guard_band_factor, critical_band_low, critical_band_high,
     ood_dominance_ratio, k, max_ood_inflation) = _policy(
         guard_band_factor, critical_band_low, critical_band_high,
         ood_dominance_ratio, k, max_ood_inflation)
    budget = _budget_fields(u_c, u_c_base, guard_band_factor, k, max_ood_inflation)
    u_c_raw, u_c_capped = budget["u_c_raw"], budget["u_c_used"]
    ood_capped = budget["ood_capped"]
    U_base, U = budget["U_base"], budget["U"]
    U_effective, U_pass = budget["U_effective"], budget["U_pass"]
    _number("critical_limit_low", critical_band_low * U_pass)
    _number("critical_limit_high", critical_band_high * U_base)

    ood_dominant = (u_c_base > 0) and (u_c_capped > ood_dominance_ratio * u_c_base)
    # OOD 主导时置信度随"相对膨胀量"线性衰减，下限 0.5
    if ood_dominant and u_c_base > 0:
        inflation = u_c_capped / u_c_base
        confidence = max(0.5, min(1.0, 1.0 - 0.5 * (inflation - 1.0)))
    else:
        confidence = 1.0

    ood_note = ""
    if ood_dominant:
        pct = (U / U_base - 1.0) * 100.0 if U_base > 0 else 0.0
        ood_note = (
            "；OOD 分量 u_ood={:.6f} mm 使 U 相对可信基线膨胀 {:.0f}%（U={:.6f} vs U_base={:.6f} mm），"
            "该部分不计入『放宽验收』，仅降低结论置信度".format(u_ood, pct, U, U_base)
        )
        if ood_capped:
            ood_note += "；且已按 {:.0f}×u_c_base 上限截断（beta 未标定，防止其单独支配结论）".format(
                max_ood_inflation
            )

    if ad > critical_band_high * U_base:
        verdict = "fail"
        reason = (
            "|d|={:.6f} mm > {:.1f}×U_base={:.6f} mm（工程一致性规则边界），判与参考不一致。"
            "U_base 只由 u_a、u_b 构成，与 OOD 无关，故结论不会因 u_ood 增大而翻转".format(
                ad, critical_band_high, critical_band_high * U_base
            ) + ood_note
        )
    elif ad > critical_band_low * U_pass:
        verdict = "indeterminate"
        reason = (
            "|d|={:.6f} mm 落在临界带 ({:.1f}×U_pass, {:.1f}×U_base] = ({:.6f}, {:.6f}] mm 内，"
            "按当前工程一致性规则判『不能确定』，此结论不是工件公差合格判定，"
            "建议人工复测或改用更高精度仪器".format(
                ad, critical_band_low, critical_band_high,
                critical_band_low * U_pass, critical_band_high * U_base,
            ) + ood_note
        )
    else:
        if ood_dominant:
            verdict = "indeterminate"
            reason = (
                "|d|={:.6f} mm 未落在临界带内，但 OOD 分量显著（u_c={:.6f} mm）"
                "使测量可信度不足，结论降级为『不能确定』".format(ad, u_c_capped) + ood_note
            )
        else:
            verdict = "pass"
            reason = (
                "|d|={:.6f} mm <= {:.1f}×U_pass={:.6f} mm（U_pass={:.4f}×U_base），"
                "测量结果与参考值满足当前工程一致性规则".format(
                    ad, critical_band_low, critical_band_low * U_pass, guard_band_factor
                ) + ood_note
            )

    return {
        "verdict": verdict,
        "requires_human_review": verdict != "pass",
        "confidence": round(confidence, 4),
        "u_ood_dominant": bool(ood_dominant),
        "reason": reason,
        "U": U,
        "U_base": U_base,
        "U_effective": U_effective,
        "U_pass": U_pass,
        "ood_capped": bool(ood_capped),
        "u_c_used": u_c_capped,
        "u_c_raw": u_c_raw,
    }


# ========== Step 3-4: 主入口 ==========

def compute_compliance(
    inp: ComplianceInput,
    *,
    guard_band_factor: Optional[float] = None,
    critical_band_low: Optional[float] = None,
    critical_band_high: Optional[float] = None,
    ood_dominance_ratio: Optional[float] = None,
) -> ComplianceResult:
    """L3 合规判定主入口 — 对 GUM 框架的扩展应用

    实现四步流程：
        Step 1: a = map_anomaly(score, threshold)
        Step 2: u_ood = sqrt((beta_vis*a_vis)^2 + (beta_sensor*a_sensor)^2)
        Step 3: u_c = sqrt(u_a^2 + u_b^2 + u_ood^2), U = k * u_c
        Step 3b: U_effective = guard_band_factor * U（P2-22 保护带）
        Step 4: 三态判定 pass / fail / indeterminate（P2-24）

    当 include_u_ood=False 时退化为 G0 baseline（L1 简化版），
    用于消融实验对比。

    Args:
        inp: 判定输入（ComplianceInput）
        guard_band_factor: 保护带系数，覆盖 inp.guard_band_factor
            （便于从 equis_l3.yaml 注入，见 l3_config.py）
        critical_band_low / critical_band_high / ood_dominance_ratio: 覆盖 inp 的同名阈值

    措辞纪律：本方法是对 GUM 框架的扩展应用，不是"符合 GUM"。
    """
    # 覆盖参数（None = 用 inp 的值）
    if inp.assessment_type not in ("reference_consistency", "uncertainty_only"):
        raise MeasurementInputError("assessment_type", "未实现该评定类型；产品符合性需要独立的公差输入与规则")
    if not isinstance(inp.include_u_ood, bool):
        raise MeasurementInputError("include_u_ood", "include_u_ood 必须是布尔值")
    if inp.a_mapping not in ("hard", "softplus"):
        raise MeasurementInputError("a_mapping", "不支持的异常映射方式")
    mv = _number("measured_value", inp.measured_value)
    rv = (_number("reference_value", inp.reference_value)
          if inp.assessment_type == "reference_consistency" else None)
    precision = _number("sensor_precision", inp.sensor_precision, minimum=0.0)
    env_temp = _number("env_temp", inp.env_temp, minimum=-273.15)
    baseline_temp = _number("baseline_temp", inp.baseline_temp, minimum=-273.15)
    alpha = _number("temp_coefficient", inp.temp_coefficient, minimum=0.0)
    for name in ("visual_threshold", "sensor_threshold"):
        _number(name, getattr(inp, name))
    for name in ("beta_vis", "beta_sensor"):
        _number(name, getattr(inp, name), minimum=0.0)
    for name in ("visual_anomaly_score", "sensor_anomaly_score"):
        if getattr(inp, name) is not None:
            _number(name, getattr(inp, name))
    gbf, cb_low, cb_high, ood_ratio, factor, _cap = _policy(
        inp.guard_band_factor if guard_band_factor is None else guard_band_factor,
        inp.critical_band_low if critical_band_low is None else critical_band_low,
        inp.critical_band_high if critical_band_high is None else critical_band_high,
        inp.ood_dominance_ratio if ood_dominance_ratio is None else ood_dominance_ratio, inp.k)

    # 器具误差限按矩形分布估计（B 类；保留历史字段 u_a）。
    u_a = precision / math.sqrt(3)

    # ===== B 类：环境温度影响 =====
    # u_b = L * α * ΔT / sqrt(3)；α 由 material/temp_coefficient 解析（默认钢 11.5e-6 /°C）
    delta_t = _number("delta_t", abs(env_temp - baseline_temp))
    u_b = _number("u_b", abs(mv) * alpha * delta_t / math.sqrt(3))

    # ===== L3: OOD 不确定度分量 =====
    if inp.include_u_ood:
        # Step 1: 异常量纲映射
        a_vis = 0.0
        a_sensor = 0.0
        if inp.visual_anomaly_score is not None:
            a_vis = map_anomaly(
                inp.visual_anomaly_score,
                inp.visual_threshold,
                inp.a_mapping,
            )
        if inp.sensor_anomaly_score is not None:
            a_sensor = map_anomaly(
                inp.sensor_anomaly_score,
                inp.sensor_threshold,
                inp.a_mapping,
            )

        # Step 2: 多源方和根合成
        u_ood_vis, u_ood_sensor, u_ood = compute_u_ood(
            a_vis, a_sensor, inp.beta_vis, inp.beta_sensor,
        )

        mode = "L3"
    else:
        # G0 baseline: 无 u_ood
        a_vis = 0.0
        a_sensor = 0.0
        u_ood_vis = 0.0
        u_ood_sensor = 0.0
        u_ood = 0.0
        mode = "L1"

    # ===== Step 3: GUM 扩展合成（三源方和根）=====
    # u_c = sqrt(u_a^2 + u_b^2 + u_ood^2)
    # 注意：当 include_u_ood=False 时 u_ood=0，自动退化为 L1 简化版
    u_c_base = _number("u_c_base", math.hypot(u_a, u_b))
    if u_c_base == 0:
        raise MeasurementInputError("u_c_base", "缺少可信的不确定度基线，不能出具有效评定",
                                    status="unavailable", code="missing_uncertainty_baseline")
    u_c_raw = _number("u_c_raw", math.hypot(u_a, u_b, u_ood))

    # ===== Step 3b + 4: 保护带 + 三态判定 =====
    # 仅估计不确定度不制造 reference=measured 的比较任务。
    d = _number("deviation", mv - rv) if rv is not None else None
    if d is None:
        verdict_info = _budget_fields(u_c_raw, u_c_base, gbf, factor)
        verdict_info.update(verdict=None, confidence=None, requires_human_review=False,
                            u_ood_dominant=verdict_info["u_c_used"] > ood_ratio * u_c_base,
                            reason="仅估计不确定度；不执行参考一致性或工件符合性判定")
    else:
        verdict_info = compute_verdict(
            deviation=d, u_c=u_c_raw, u_c_base=u_c_base, guard_band_factor=gbf,
            critical_band_low=cb_low, critical_band_high=cb_high,
            ood_dominance_ratio=ood_ratio, u_ood=u_ood, k=factor)
    # 判定实际使用的合成不确定度（受 MAX_OOD_INFLATION 上限约束），
    # 结果里的 u_c/U 与它保持一致，避免"报告的 U"与"判据用的 U"不同导致评审歧义。
    u_c = verdict_info["u_c_used"]
    U = factor * u_c
    U_effective = verdict_info["U_effective"]
    U_pass = verdict_info["U_pass"]

    # 二态判定保留（向后兼容），采用与三态判定一致的"可信门限"：
    # in_spec = (|d| <= guard_band_factor * U_base) = (|d| <= U_pass)
    # 有意不用被 OOD 抬高的 nominal U_effective，否则 u_ood 会直接放宽 in_spec。
    in_spec = abs(d) <= U_pass if d is not None else None

    return ComplianceResult(
        u_a=u_a,
        u_b=u_b,
        a_vis=a_vis,
        a_sensor=a_sensor,
        u_ood_vis=u_ood_vis,
        u_ood_sensor=u_ood_sensor,
        u_ood=u_ood,
        u_c=u_c,
        U=U,
        deviation=d,
        in_spec=in_spec,
        mode=mode,
        a_mapping=inp.a_mapping,
        # ---- 新增字段（P2-22/P2-24）----
        guard_band_factor=gbf,
        U_effective=U_effective,
        u_c_base=u_c_base,
        U_base=verdict_info["U_base"],
        U_pass=U_pass,
        verdict=verdict_info["verdict"],
        requires_human_review=verdict_info["requires_human_review"],
        confidence=verdict_info["confidence"],
        u_ood_dominant=verdict_info["u_ood_dominant"],
        ood_capped=verdict_info["ood_capped"],
        u_c_raw=verdict_info["u_c_raw"],
        reason=verdict_info["reason"],
        assessment_type=inp.assessment_type,
        rule_version=("legacy_consistency_v1" if d is not None else "legacy_budget_v1"),
    )


# ========== 前端渲染辅助 ==========

def result_to_dict(res: ComplianceResult) -> dict:
    """将 ComplianceResult 转换为前端 compliance-card 可渲染的 dict

    前端通过解析 compliance-card 代码块中的 JSON 渲染决策边界图。
    原有键名与顺序保持不变（追加式新增），新增键用于三态判定展示。
    """
    return {
        "u_a": res.u_a,
        "u_b": res.u_b,
        "u_ood": res.u_ood,
        "u_ood_vis": res.u_ood_vis,
        "u_ood_sensor": res.u_ood_sensor,
        "u_c": res.u_c,
        "U": res.U,
        "deviation": res.deviation if res.deviation is not None else None,
        "in_spec": res.in_spec,
        "mode": res.mode,
        "a_mapping": res.a_mapping,
        # ---- 追加字段（P2-22/P2-24）----
        "guard_band_factor": res.guard_band_factor,
        "U_effective": res.U_effective,
        "u_c_base": res.u_c_base,
        "U_base": res.U_base,
        "U_pass": res.U_pass,
        "verdict": res.verdict,
        "requires_human_review": bool(res.requires_human_review),
        "confidence": res.confidence if res.confidence is not None else None,
        "u_ood_dominant": bool(res.u_ood_dominant),
        "ood_capped": bool(res.ood_capped),
        "u_c_raw": res.u_c_raw,
        "reason": res.reason,
        "execution_status": res.execution_status,
        "assessment_type": res.assessment_type,
        "rule_version": res.rule_version,
        "confidence_kind": "heuristic" if res.confidence is not None else None,
        "ood_mapping_validated": False,
    }


# ========== beta 标定工具（P2-20：已接入 calibrate_beta.py）==========

def calibrate_beta_physical_anchor(
    anomaly_scores: List[float],
    physical_deviations: List[float],
    threshold: float,
    use_median: bool = True,
) -> float:
    """beta 物理锚点标定（第一阶段）

    在受控异常样本上，记录异常分数 a 和实际测量偏差 Delta，
    估算 beta = Delta / a。

    受控异常样本制备方法（Phase 1 P1-5）：
        1. 正常工件作为基准，记录 Omni-AD 异常分数（应低于阈值）
        2. 人工注入视觉异常（遮挡部分工件/涂改表面/角度偏移）
        3. 用更高精度仪器（如三坐标测量机）测量实际偏差 Delta
        4. 记录 Omni-AD 异常分数 a
        5. 重复 50+ 组，计算 beta = median(Delta_i / a_i)

    Note:
        本函数仅计算物理锚点 beta，不包含 F1 微调（第二阶段）。
        F1 微调需要在验证集上跑模型推理，由独立的 calibration 脚本完成。
        稳定性退路：若物理锚点 beta 方差过大，应优先使用中位数。

    量纲: beta 单位 mm/score_unit（Delta [mm] ÷ a [score_unit]）。
    调用方: agent/scripts/calibrate_beta.py（改造前本函数全项目零调用）。
    """
    if len(anomaly_scores) != len(physical_deviations):
        raise ValueError(
            "Length mismatch: " + str(len(anomaly_scores)) + " scores vs "
            + str(len(physical_deviations)) + " deviations"
        )

    if len(anomaly_scores) == 0:
        raise ValueError("Empty input: no samples to calibrate")

    ratios = []
    for score, delta in zip(anomaly_scores, physical_deviations):
        a = max(0.0, score - threshold)  # 使用 hard 模式
        if a > 1e-6:  # 避免除零（正常样本）
            ratios.append(delta / a)

    if not ratios:
        # 所有样本都是正常的（a=0），无法标定
        return 0.0

    if use_median:
        ratios.sort()
        n = len(ratios)
        if n % 2 == 1:
            return ratios[n // 2]
        else:
            return (ratios[n // 2 - 1] + ratios[n // 2]) / 2
    else:
        return sum(ratios) / len(ratios)
