# 双目静态采集与标定工具

本入口独立于原聊天应用。新克隆默认使用项目根目录 .venv；可通过 EQUIS_CAPTURE_PYTHON 或本地 capture-environment.json 指定解释器，示例见 capture-environment.example.json。公开仓库不包含原机器的绝对环境路径，依赖固定OpenCV4.13。

## 1. 设备与设置

在 agent 目录执行：

```powershell
.\scripts\run_capture.ps1 --list-devices
```

复制 `capture-config.example.json` 为 `capture-config.json`，将确认为物理左、右的两个不同device_id填入。索引会在启动时重新解析；重插、枚举变化、单路失败后停止当前会话，重新确认身份。不能将一路相机的两次拍摄当作双目。

当前一次现场检查仅有一路设备。原请求1920×1080实际返回1280×720；1280×720持续读取15秒约10.03Hz，驱动FPS字段约30。因此必须分别保留请求、驱动报告与实测速率，不能承诺双路30FPS。第二路接入后再验收。

曝光、对焦、白平衡和增益模板初始为null，表示未请求设置，不表示已锁定。依照实际相机支持情况设置后检查回读；不支持的项目标为unverified。开始标定前还需现场确认清晰度、曝光稳定和安装状态，避免焦距或对焦在采集中变化。

## 2. 先诊断后标定采集

```powershell
.\scripts\run_capture.ps1 --config .\capture-config.json --out "C:\Users\Administrator\Desktop\挑战杯院赛提交材料\实验采集\diagnostic-001" --count 20 --purpose diagnostic --sensor-port COM10
```

会话目录默认必须不存在。--resume只恢复意外中断且未关闭的会话，并要求配置一致；未提交事件移到quarantine保留，不删除或冒充有效数据。正常关闭的会话用新目录继续。

左右PNG无损原图、SHA256、软件帧序号、主机接收时间和单调时钟保存在每个事件中。若指定COM10，另保存当时传感器快照及原始包和丢包计数。相机与传感器仅作主机时间关联，不能称曝光同步。当前配对准入参数max_skew_ms和max_age_ms是软件条件；对运动物体还须评定未知曝光错位，暂限定静态对象。

使用 `capture-environment.json` 中的Python运行：

```powershell
python -B scripts/validate_capture.py "会话目录"
```

必须报告valid=true并查看accepted/rejected、实际接收帧率、图像尺寸和失败原因。文件哈希及事件时间不符时拒绝验证；不对失败数据静默填零。

## 3. 标定板与采集

复制 `calibration-board.example.json` 为 `calibration-board.json`，填写实物确认的内角点columns/rows、square_size_mm和size_source。商品截图的12×9“方格”对应候选11×8内角点；必须核对实物。Q12-150-10中的10不自动当成已确认格距；卖家±5μm不能直接填成标准不确定度。PET软板需保持平整固定，记录尺寸依据与平面状态。

```powershell
.\scripts\run_capture.ps1 --config .\capture-config.json --out "C:\Users\Administrator\Desktop\挑战杯院赛提交材料\实验采集\calibration-001" --count 48 --purpose calibration --validation-every 4 --interactive
```

每次移动到新的静态位姿，摆稳后按回车。预先每第4个位姿留作验证，48对形成36个拟合和12个验证图对；需覆盖共同视场中心、边缘、近中远与倾斜，不能用高度重复的连续帧堆数量。原图不做缩放或矫正后再标定。

## 4. 离线标定

同样使用独立环境Python：

```powershell
python -B scripts/calibrate_stereo.py --session "会话目录" --board calibration-board.json --out calibration-candidate.json
```

工具检查完整性、用途和拟合/验证图像重复，检测棋盘角点，分别拟合内参、双目外参和校正矩阵，用预留图对统计竖直残差。至少10个拟合及3个验证图对才运行；正式建议如上36/12或更充分设计。无法检出角点时明确列出事件；可提供 `--exclusions exclusions.json`（已有事件ID→具体理由），原图保留，排除清单进入参数包，不因残差偏大自动删除数据。

输出保存K/D/R/T/E/F/R1/R2/P1/P2/Q、板参数、图像尺寸、设备配置、输入哈希、标定ID与验证残差。结果始终是 `candidate_requires_physical_validation`；重投影和极线残差小不证明毫米尺寸精度，仍需独立参考件、重复性、预算及适用域验收。相机相对位置、焦距、分辨率改变后重新验证或标定。[OpenCV标定依据](https://docs.opencv.org/4.x/d9/d0c/group__calib3d.html)

## 5. 后续实测与传感器静置工具

`measurement-pairs.example.csv` 仅含表头，不是实验结果。填写真实尺寸、参考值及u_ref、原始异常分数、几何指标、工件与会话身份，再用 `scripts/validate_measurement_dataset.py input.csv --out reviewed.json` 检查。工具拒绝重复事件、非有限值和同一工件跨development/calibration/test，不拟合或启用毫米映射。

旧 `scripts/calibrate_beta.py --write` 和 `write_beta_to_yaml()` 已阻断。旧比值法须显式 --research-only、真实CSV或明确--demo及显式研究阈值；没有默认-4、没有默认示例，也不会写部署配置。

`scripts/assess_sensor_static.py input.jsonl --out stability.json` 可分析传感器原始记录的均值、标准差和采样间隔；未声明全程静止时不生成零偏候选。只有操作者确认后才可加 --stationary-confirmed，但依然不自动修正固件，不代替六面标定或标准器对照。

所有工具均只访问本地数据和设备，无LLM请求。程序退出释放相机和串口；驱动挂起会明确报错，不在同一会话中偷偷切换设备。
