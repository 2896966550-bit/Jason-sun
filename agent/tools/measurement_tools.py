"""测量算法工具 —— 计算 & 指南（后续接入真实硬件数据）"""
import math
from typing import Optional


def estimate_uncertainty(
    measured_value: float,
    sensor_precision: float,
    env_temp: float = 25.0,
    env_humidity: float = 50.0,
    baseline_temp: float = 20.0,
    temp_coefficient: Optional[float] = None,
    material: str = "steel",
) -> str:
    """估算测量不确定度（简化 GUM 方法）

    v4 维护：本函数统一调用 agent.omni_ad.uncertainty.compute_compliance
    避免与 coordinator.py 重复实现 GUM 计算逻辑。

    措辞纪律：本计算是对 GUM 框架的扩展应用，不是"符合 GUM"。

    P2-21 修复（重要）：
        原实现把 ``temp_coefficient`` 默认写成 ``0.0001``（注释称"钢"），
        而钢的线膨胀系数约 **11.5e-6 /°C** —— 原值放大了约 **8.7 倍**。
        后果：100mm 量程、ΔT=5K 时 u_b 从应有的 0.0033mm 被抬到 0.0289mm，
        使扩展不确定度 U 达到"0.01mm 精度量具"的 5.9 倍，不合常理，
        且会被计量专业评审一眼抓住。
        现默认值改为 ``None``，交由 ``material`` 查表决定（缺省 steel）。

    Args:
        measured_value: 实测值 mm
        sensor_precision: 传感器精度 mm
        env_temp: 环境温度 摄氏度，默认 25.0
        env_humidity: 环境湿度（保留参数，当前未使用）
        baseline_temp: 基准温度 摄氏度，默认 20.0（JJF1059）
        temp_coefficient: 线膨胀系数 1/°C；None = 按 material 查表
        material: 材料名（steel/aluminum/invar/copper/stainless，支持中文别名）

    Returns:
        不确定度分析文本（含 A 类/B 类/合成/扩展）

    对应文档: 《Equis × Omni-AD 总规划 v4》第 5.2 节 L1 简化版
    """
    from agent.omni_ad.uncertainty import ComplianceInput, compute_compliance
    from agent.omni_ad.uncertainty.compliance import resolve_temp_coefficient

    alpha = resolve_temp_coefficient(material=material, temp_coefficient=temp_coefficient)
    inp = ComplianceInput(
        measured_value=measured_value,
        reference_value=None,  # 仅估计预算，不创建虚构参考比较
        sensor_precision=sensor_precision,
        env_temp=env_temp,
        baseline_temp=baseline_temp,
        temp_coefficient=alpha,
        include_u_ood=False,  # 简化版不启用 L3 第三源
        k=2.0,
        assessment_type="uncertainty_only",
    )
    res = compute_compliance(inp)
    u_sensor, u_temp = res.u_a, res.u_b
    u_combined, u_expanded = res.u_c, res.U
    delta_t = abs(env_temp - baseline_temp)

    # 合理性提示：U 远大于器具精度通常意味着某个分量被高估
    #（原 0.0289mm 的 u_b 正是这样被发现的）
    warn = ""
    if sensor_precision > 0 and u_expanded > 3 * sensor_precision:
        warn = (f"\n⚠ 提示：U={u_expanded:.4f}mm 已超过器具精度 {sensor_precision}mm 的 3 倍，"
                f"建议核对温度影响、重复性等分量是否被高估。")

    return (
        f"=== 测量不确定度评估 ===\n"
        f"实测值: {measured_value:.3f} mm\n"
        f"环境: {env_temp}°C / {env_humidity}%RH（基准 {baseline_temp}°C）\n"
        f"材料线膨胀系数 α: {alpha:.3e} /°C（{material}）\n\n"
        f"B类（器具误差限，矩形分布 u=a/√3）: ±{u_sensor:.4f} mm\n"
        f"B类（温度影响 ΔL=α·L·ΔT，u=ΔL/√3）: ±{u_temp:.4f} mm\n"
        f"──────────────────────────\n"
        f"合成标准不确定度 u_c: {u_combined:.4f} mm\n"
        f"扩展不确定度 U(k=2): {u_expanded:.4f} mm\n"
        f"相对不确定度: {u_expanded/max(abs(measured_value), 1e-9)*100:.4f}%\n\n"
        f"结论: 环境温度偏差 {delta_t:.1f}°C，已按 α={alpha:.3e}/°C 计入热膨胀影响。"
        f"{warn}"
    )


def calibrate_camera_guide(chessboard_cols: int = 9, chessboard_rows: int = 7) -> str:
    """
    给出双目标定操作指南。
    输入:
      chessboard_cols: 棋盘格内角列数
      chessboard_rows: 棋盘格内角行数
    """
    return (
        f"=== 双目标定操作指南 ===\n\n"
        f"📐 棋盘格规格: {chessboard_cols}×{chessboard_rows} 内角点\n\n"
        f"步骤:\n"
        f"1. 固定左右相机位置，确保两者视场有足够重叠区域（≥60%）\n"
        f"2. 手持棋盘格标定板，在不同姿态下同时拍摄左右图像（建议20-30组）:\n"
        f"   - 正面 × 5组\n"
        f"   - 左倾30° × 5组\n"
        f"   - 右倾30° × 5组\n"
        f"   - 上仰30° × 5组\n"
        f"   - 下俯30° × 5组\n"
        f"3. 确保每组图像中棋盘格完整可见、光照均匀\n"
        f"4. 保存图像至 calibration_images/left/ 和 calibration_images/right/\n"
        f"5. 运行 calibrate.py 自动计算内参和立体标定参数\n\n"
        f"⚠️ 标定完成后务必锁紧相机支架，避免位移。"
    )


def calculate_3d_coordinates(
    disparity: float,
    baseline: float = 150.0,
    focal_length: float = 500.0,
    pixel_size: float = 0.003,
) -> str:
    """
    根据视差值计算三维坐标。
    输入:
      disparity: 视差（像素）
      baseline: 双目基线距（mm）
      focal_length: 焦距（像素）
      pixel_size: 像元尺寸（mm/pixel）
    输出: 三维坐标
    """
    from agent.omni_ad.uncertainty.compliance import _number
    disparity=_number("disparity",disparity,positive=True)
    baseline=_number("baseline",baseline,positive=True)
    focal_length=_number("focal_length",focal_length,positive=True)
    pixel_size=_number("pixel_size",pixel_size,positive=True)
    if disparity <= 0:
        return "⚠️ 视差必须大于0，请检查双目匹配结果。"

    # 修正：focal_length 已是像素单位(px)，公式 Z = f_px * B / d_px
    # 旧版多除了 pixel_size 导致 ~333 倍误差（focal 500px/B 150mm/d 45px → 555m 而非 1.67m）
    depth = _number("depth",(focal_length * baseline) / disparity,positive=True)
    return (
        f"=== 三维坐标计算 ===\n"
        f"基线: {baseline} mm | 焦距: {focal_length} px | 像元: {pixel_size} mm\n"
        f"视差: {disparity} px\n\n"
        f"Z（深度）: {depth:.2f} mm\n"
        f"（X、Y 需结合像素坐标进一步计算）"
    )


def sensor_data_diagnose(temp: float, humidity: float, accel_x: float, accel_y: float, accel_z: float) -> str:
    """
    诊断传感器数据是否正常，给出建议。
    """
    from agent.omni_ad.uncertainty.compliance import _number
    temp=_number("temperature",temp,minimum=-273.15)
    humidity=_number("humidity",humidity,minimum=0)
    if humidity>100:
        raise ValueError("湿度必须在0至100%RH之间")
    accel_x=_number("accel_x",accel_x)
    accel_y=_number("accel_y",accel_y)
    accel_z=_number("accel_z",accel_z)
    issues = []

    if temp < -10 or temp > 60:
        issues.append(f"⚠️ 温度 {temp}°C 超出传感器工作范围（-10~60°C），数据可能不准")
    elif temp > 35:
        issues.append(f"⚠️ 温度 {temp}°C 偏高，建议开启空调降温，减小热膨胀误差")

    if humidity < 20:
        issues.append(f"⚠️ 湿度 {humidity}%RH 偏低，静电风险增加")
    elif humidity > 80:
        issues.append(f"⚠️ 湿度 {humidity}%RH 偏高，可能影响电子元件")

    accel_mag = _number("accel_magnitude",math.hypot(accel_x,accel_y,accel_z))
    if accel_mag > 12:
        issues.append(f"⚠️ 振动强度 {accel_mag:.2f} m/s² 较大，测量台面可能抖动，建议增加减震垫")
    elif accel_mag > 9.8 * 1.2:
        issues.append(f"⚠️ 加速度幅值异常({accel_mag:.2f})，检查传感器固定")

    if not issues:
        return (
            f"=== 传感器诊断 ===\n"
            f"温度: {temp}°C | 湿度: {humidity}%RH | 振动: {accel_mag:.2f} m/s²\n\n"
            f"上述读数未触发当前诊断阈值；尚未据此验证测量精度或环境适用性。"
        )

    return (
        f"=== 传感器诊断 ===\n"
        f"温度: {temp}°C | 湿度: {humidity}%RH | 振动: {accel_mag:.2f} m/s²\n\n"
        + "\n".join(issues)
    )
