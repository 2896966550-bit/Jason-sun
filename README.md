# Equis · 测量辅助与多传感器采集原型

Equis 将本地确定性测量计算、视觉异常评分、ESP32 传感器采集和 Flask/LangGraph 交互整合在一起。本仓库包含完整应用源码、离线前端资源、固件、采集/标定工具和自动化检查；物理精度和异常分数到毫米的映射仍需独立实验验证。

## 当前能力

- 测量输入与来源校验、测量事件绑定、三态结果和可展开预算。
- 本地异常原始分数；未经标定的二分类阈值与 OOD 毫米映射保持关闭。
- ESP32 温湿度慢速采样与 IMU 连续采样、单位/时间/序号/有效性记录。
- Windows DirectShow 稳定相机身份、静态双目软件配对、PNG/哈希/事件存储、回放与中断恢复。
- 棋盘格离线标定候选参数包、独立验证图对预留、配对残差数据检查。
- 无需API密钥、网络或硬件的本地回归门禁；CI另行安装依赖并执行。

## 快速启动（Windows）

需要 Python 3.13、Git；前端测试另需 Node.js 22。

```powershell
git clone https://github.com/2896966550-bit/Jason-sun.git
cd Jason-sun
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item agent/.env.example agent/.env
.\start.ps1
```

浏览器打开 http://127.0.0.1:7860。无密钥时可运行界面、本地计算和离线检查；模型服务与检索需配置自己的可用服务。GPU版PyTorch应按机器CUDA条件另行安装，不将本地GPU时延当作其他机器的承诺。

在 `agent/.env` 填入 `BAILIAN_API_KEY`，按账号权限覆盖模型名称；模板中没有密钥。默认只监听本机，可用 `EQUIS_HOST`/`EQUIS_PORT` 明确配置。当前开发服务没有完整公网身份认证，不直接作为公网服务部署。

## 验证

```powershell
.\.venv\Scripts\python.exe -B scripts/check_release.py
cd agent
..\.venv\Scripts\python.exe -B scripts/precommit.py
```

门禁对子进程设置临时数据、测试密钥并阻断网络，不写入生产会话、不打开相机/串口、不消耗模型额度。联网评测与硬件实验由独立入口执行。[当前验证状态](docs/VALIDATION.md)

## 相机和传感器

```powershell
cd agent
.\scripts\run_capture.ps1 --list-devices
```

相机采集按设备身份配置；两路UVC的主机接收时间接近不证明曝光同步。参考 [采集与标定指南](agent/CAPTURE_GUIDE.md)、[固件说明](esp32_sensors/README.md)。实际棋盘格内角点、格距和来源必须核验，不能用商品型号或默认值生成毫米验收。

## 目录

```text
agent/              Flask应用、前端、感知、计算、回归和实验工具
esp32_sensors/      当前生产固件（PlatformIO）
scripts/            发布检查、模型文件校验
docs/               当前验证、模型安装、项目边界
third_party/        离线前端依赖的许可证与校验清单
.github/workflows/  软件回归与固件编译CI
```

原仓库单页保留在 [docs/legacy/equis.html](docs/legacy/equis.html)，仅作历史参考；最新应用入口是 `agent/server.py` 与 `agent/equis.html`。

## 模型与数据

权重、原型库、API密钥、运行数据库和用户图像不随源码公开。视觉模块需要将自己保管的模型文件放入指定目录，并核对 [模型清单与安装步骤](docs/MODELS.md)。缺少模型时明确报告不可用，不填入正常分数。标准摘要不能替代标准原文或正式合规判断。

## 许可证

保留仓库既有 [Apache-2.0](LICENSE) 许可证；第三方前端库按 [各自许可证](THIRD_PARTY_NOTICES.md) 分发。模型、训练数据和标准资料的权利范围独立于应用源码许可证。
