# Equis 代码维护入口

这是现有 Flask + LangGraph 工程的维护说明。保留原有启动方式和无构建工具的前端；未迁移框架、修改权重或更改测量判定公式。

## 模块职责

|目录/文件|职责|
|---|---|
|`server.py`|HTTP 路由、上传、SSE 传输|
|`http_support.py`|HTTP 对象/字符串/分页输入契约、静态资源版本指纹|
|`agent_core.py`、`agent_router.py`、`coordinator.py`|引擎入口、路由决策、已有工具协调|
|`graph/`|状态图组装、节点调度、运行生命周期|
|`perception/`|感知采集与同一次测量事件绑定|
|`tools/measurement_executor.py`|基于用户证据的确定性测量执行|
|`omni_ad/`|视觉推断与不确定度预算；权重单独保管|
|`tools/json_output.py`|模型输出 JSON 对象解析；供路由、视觉和规划共同使用|
|`tools/cards.py`|结果卡片提取、去除和元数据绑定|
|`chat_history.py`、`memory/`|短期对话记录与长期案例记忆，二者分工不同|
|`observability/`、`compliance/`|运行轨迹、审计与出站限制|
|`scripts/`|本地门禁、测试、离线评测等维护工具|

前端资源按 `tokens → app → cards → premium → motion → interaction` 加载。后两层分别管理动效偏好/六卡片，以及导航、输入和通用交互。`static/js/composer.js` 只负责测量参数表单校验与草稿准备，不自动发送、不填入默认观测值；`cards.js` 保留结果契约并组织摘要、预算展开与详情。调整这些层时运行对应前端测试，不要只在最后追加高优先级覆盖规则。

## 启动

完整安装步骤见项目根目录 README。在已安装项目依赖的 Python 环境、此目录下运行：

```powershell
python -B server.py
```

默认地址为 `http://localhost:7860`。配置模板为 `.env.example`，不要提交 `.env`、生产数据库、用户图片或模型权重。依赖安装和硬件接入另行安排，维护门禁不安装或升级任何依赖。

## 一键维护门禁

```powershell
python -B scripts/precommit.py
```

门禁包含 19 项：原有算法/合规/轨迹检查，测量输入、事件绑定与执行回归，传感器协议与时效、双目采集与标定契约、HTTP、JSON 解析、历史配对、图生命周期，以及前端静态和交互检查。

- Python 用例各自在子进程中执行；数据目录指向临时目录，不读取 `.env`，设置测试密钥并阻断 socket 连接。
- 子进程完全退出后再删除临时数据，避免 Windows 原生 SQLite/Chroma 句柄阻碍清理。
- 图编排测试用替身代替外部检索与长期记忆写入；不使用 LLM 或 embedding 额度。
- 图像契约测试自行生成有效 PNG，不依赖用户上传的照片，也不把合成图当作测量精度实验。
- 需要本地 Node.js 运行 JavaScript 检查；找不到时门禁明确失败，不静默跳过。可设置 `EQUIS_NODE` 指定解释器。

单独执行一组本地测试：

```powershell
python -B -m scripts.run_local_test scripts.test_http_contracts
python -B -m scripts.run_local_test scripts.test_graph_runtime
python -B -m scripts.check_javascript
```

完整联网算法评测仍由 `scripts/run_evals.py` 等独立脚本执行，不纳入本地零额度门禁。

## 传感器本地诊断

生产固件入口及协议说明见仓库根目录 `esp32_sensors/README.md`。主机保留旧固件诊断兼容；未声明单位的推断数据不会获准物理融合。协议 v2 的温湿度和 IMU 独立检查有效性与新鲜度，串口断线清空旧快照，后台持续排空缓存。

使用同一项目 Python 环境，在此目录运行：

```powershell
python -B scripts/sensor_diagnostics.py --port COM10 --seconds 30 --out sensor-check.jsonl
```

输出原始串口包、主机接收时刻、解析状态及汇总 JSON，不调用模型、不读取 `.env`，不覆盖已存在输出。串口同一时刻只能由一个进程读取，运行前结束另一个串口监视器或正在读取设备的服务。多适配器时必须用 `EQUIS_SENSOR_PORT` 明确设备。诊断结束释放串口；不要把短时采样验证当作计量校准或相机曝光同步验证。

## 运行数据和会话生命周期

双目采集、独立标定环境、参数模板及离线验证步骤见 `CAPTURE_GUIDE.md`。新工具是独立实验入口，未将未标定双目尺寸注入原聊天测量路径。`scripts/calibrate_beta.py` 的旧部署写回已关闭；比值法只保留显式研究入口，正式配对数据使用新的字段与分组检查。

- 默认运行数据位置仍为 `agent/data`。可在启动进程前通过 `EQUIS_DATA_DIR` 指定另一位置；聊天、轨迹、审计和知识库使用同一配置入口。
- `EQUIS_ENV_FILE` 可指定环境文件；空字符串表示不加载环境文件，供隔离测试使用。
- 一次性图请求使用 UUID 会话标识，结束、失败或取消后释放该次检查点，避免长期运行时积累无用状态。
- 调用方明确传入 `thread_id` 时保留检查点，用于需要续接的会话；调用方负责其生命周期。
- 历史问答不能跨过下一条用户提问配对；上下文保留重复问题与未完成的最新轮次。
- HTTP 非对象 JSON、错误字段类型、空必填文本和无效分页统一返回结构化 400。合法请求的原有返回结构保留。

## 本轮验证的边界

本地测试验证软件契约、错误路径和工程规则，不等于物理精度或 β 标定验证。当前视觉阈值、测量公式及 OOD 到物理单位的证据状态保持不变。

发布依赖固定使用 OpenCV 4.13；原本地服务曾使用 OpenCV 5，该环境状态不能转用为新克隆的验证结论。运行时以实际能力报告为准。
