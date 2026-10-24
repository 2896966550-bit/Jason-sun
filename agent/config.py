"""Equis 配置

2026-09-12 升级：qwen3.7-max 免费额度耗尽（HTTP 403），
主控模型切换至 qwen3.8-max（原生多模态 / 1M 上下文 / Agentic SOTA）。
所有模型名均可由环境变量覆盖，便于回滚与灰度。
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path: str) -> None:
    """极简 .env 加载器：不覆盖已存在的环境变量。

    放在 config 层统一执行，使 server.py / 测试脚本 / graph 等任意入口
    都能拿到 API Key（此前仅 server.py 手动加载，导致独立入口 401）。
    """
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except Exception as e:  # .env 损坏不应阻断导入
        print(f"[config] .env 加载失败: {e}")


_load_dotenv(os.getenv("EQUIS_ENV_FILE", os.path.join(BASE_DIR, ".env")))
DATA_DIR = os.path.abspath(os.path.expanduser(os.getenv("EQUIS_DATA_DIR") or os.path.join(BASE_DIR, "data")))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")

# 阿里百炼 API（境内，满足"数据不出境 / 禁止境外 API"合规要求）
LLM_API_KEY = os.getenv("BAILIAN_API_KEY", "")
LLM_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# ── 模型配置 ──
# 主控模型：qwen3.8-max（自动跟随最新快照，2026-09-05 为 qwen3.8-max-0902）
LLM_MODEL = os.getenv("EQUIS_LLM_MODEL", "qwen3.8-max")
# 视觉理解主模型（256K 上下文，支持 function calling）
# 2026-10-17 修正：原值 "qwen3-vl-flash" 与降级值 "qwen-vl-plus" 在本账号下
# 均返回 404「Model not exist」，即**度元的视觉通道整体不可用**（实测
# 一次图片理解直接返回 [qwen3-vl-flash] HTTP 404），且降级模型也不存在，
# 等于主备双失。逐个探测后确认可用的是 qwen3-vl-plus（200）。
LLM_VL_MODEL = os.getenv("EQUIS_VL_MODEL", "qwen3-vl-plus")
# 视觉降级模型（主模型不可用时回退）
# 2026-10-17 修正：原值等于主模型（都是 qwen3-vl-plus）→ **没有降级**。
# 实测 qwen3.8-max 同样接受 image_url 输入（真实图片返回 200 且描述准确），
# 因此用主控模型作为真正不同的备份路线：qwen3-vl-plus 不可用时仍有第二次机会。
LLM_VL_FALLBACK = os.getenv("EQUIS_VL_FALLBACK", "qwen3.8-max")
# 语音识别 / 合成
LLM_ASR_MODEL = os.getenv("EQUIS_ASR_MODEL", "fun-asr")
LLM_TTS_MODEL = os.getenv("EQUIS_TTS_MODEL", "cosyvoice-v1")
# 图像生成
# 2026-10-17 修正：原值 "qwen-image-v4" 不存在；且原实现调用的兼容模式端点
# {BASE}/images/generations 在本账号下恒 404（换任何模型名都一样）。
# 零成本探测确认原生端点 /api/v1/services/aigc/text2image/image-synthesis
# 接受 wanx2.1-t2i-turbo（缺 prompt 时报 400 而非 404，说明模型与端点均有效）。
LLM_T2I_MODEL = os.getenv("EQUIS_T2I_MODEL", "wanx2.1-t2i-turbo")

# ── 引擎开关（一键回滚）──
# graph = LangGraph 状态图编排（新）；legacy = 手写 Coordinator（旧）
AGENT_ENGINE = os.getenv("EQUIS_ENGINE", "graph")

# ── 运行时参数 ──
LLM_TIMEOUT = int(os.getenv("EQUIS_LLM_TIMEOUT", "60"))
LLM_MAX_TOKENS = int(os.getenv("EQUIS_LLM_MAX_TOKENS", "2000"))
