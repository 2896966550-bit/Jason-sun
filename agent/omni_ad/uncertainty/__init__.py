"""工程一致性评定、预算估计与实验性 OOD 门控。

OOD 毫米映射尚未物理验证；执行失败与有效测量的三态判定分开表达。
"""

from .compliance import (
    ComplianceInput,
    ComplianceResult,
    MeasurementInputError,
    compute_compliance,
    map_anomaly,
    compute_u_ood,
    calibrate_beta_physical_anchor,
    result_to_dict,
)

__all__ = [
    "ComplianceInput",
    "ComplianceResult",
    "MeasurementInputError",
    "compute_compliance",
    "map_anomaly",
    "compute_u_ood",
    "calibrate_beta_physical_anchor",
    "result_to_dict",
]
