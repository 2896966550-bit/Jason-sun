# 本地模型文件

源码发布不包含训练权重或正常样本原型库。把自己保管的匹配文件放在：

```text
agent/omni_ad/weights/student_best.pth
agent/omni_ad/weights/memory_bank.pkl
```

在仓库根目录运行 `python -B scripts/verify_models.py`，检查 [model-manifest.json](model-manifest.json) 中的大小和SHA256。此检查只证明文件身份，不能证明检测性能或物理精度。

本地记录使用的学生权重约18.2MB，原型库约23.6MB。它们仍保留在原工作目录，没有随本次公开源码上传。没有模型时，界面、确定性计算和离线回归仍可使用，视觉评分应明确报告不可用。

首次使用还需按 `agent/omni_ad/configs/equis_inference.yaml` 准备对应骨干和配置。不同权重、类别原型、预处理或聚合方式需要重新评测；当前未经校准的正常/异常阈值保持关闭。只加载可信且来源明确的PyTorch/pickle文件。

模型、数据集与第三方预训练骨干的许可独立于本仓库的Apache-2.0源码许可证。此仓库不提供未确认来源的公开下载地址。
