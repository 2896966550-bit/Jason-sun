"""Equis MCP Server —— 按 Model Context Protocol (MCP) 标准暴露全部工具

架构意义：
  MCP 是 2026 年 agent↔tool 的行业标准（Anthropic 发起，已捐 Linux Foundation，
  1 万+ 公共 server）。本 server 让 Claude Desktop / Cursor / 任意 MCP 客户端
  无需改造即可调用 Equis 的计量工具集与多模态能力——工具层从"项目内私有"
  升级为"标准化开放"，体现 L3 工具层的架构升级。

启动：
  python mcp_server.py                                # stdio（本机 MCP 客户端）
  python mcp_server.py --transport sse --port 8931    # HTTP/SSE（远程客户端）

接入示例（Claude Desktop / 任意 MCP 客户端的配置）：
  {"mcpServers": {"equis": {"command": "python", "args": ["agent/mcp_server.py"]}}}
"""
import os, sys, json, argparse

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

try:
    from mcp.server.mcpserver import MCPServer as _Server   # mcp 2.x（2026）
except ImportError:
    from mcp.server.fastmcp import FastMCP as _Server       # mcp 1.x（回退）

mcp = _Server(
    "equis-metrology-tools",
    instructions="Equis 计量检测智能体工具集：计量标准检索 / 多模态视觉 / 硬件感知 / 不确定度计算 / 长期记忆 / 合规自检",
)


def _pick_device() -> str:
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


# ── 知识 / 规划 / 搜索 ──────────────────────────────────

@mcp.tool()
def kb_search(query: str, top_k: int = 3) -> str:
    """检索计量标准知识库（JJF/JJG/GB；支持标准编号精确匹配 + 语义双路召回）"""
    from tools.kb_tools import search_knowledge_base
    return search_knowledge_base(query, top_k=top_k)


@mcp.tool()
def kb_topics() -> str:
    """列出知识库全部标准条目"""
    from tools.kb_tools import list_kb_topics
    return list_kb_topics()


@mcp.tool()
def plan_task(task: str) -> str:
    """精密测量任务规划（Plan-Execute）：查标准 → 取数 → 计算 → 判定"""
    from tools.task_planner import plan_measurement
    return plan_measurement(task)


@mcp.tool()
def web_search(query: str, max_results: int = 5) -> str:
    """联网搜索最新计量标准与行业动态（境内通道）"""
    from tools.web_search import web_search
    return web_search(query, max_results=max_results)


# ── 计算工具 ────────────────────────────────────────────

@mcp.tool()
def calc_uncertainty(mv: float, sp: float, temp: float = 25.0, hum: float = 50.0) -> str:
    """测量不确定度评定：mv=实测值(mm), sp=器具精度(mm), temp=℃, hum=%RH"""
    from tools.measurement_tools import estimate_uncertainty
    return estimate_uncertainty(mv, sp, temp, hum)


@mcp.tool()
def comp_3d(disp: float, bl: float = 150.0, fl: float = 500.0) -> str:
    """双目视差 → 三维坐标：disp=视差(px), bl=基线(mm), fl=焦距(px)"""
    from tools.measurement_tools import calculate_3d_coordinates
    return calculate_3d_coordinates(disp, bl, fl)


@mcp.tool()
def diag(t: float, h: float, ax: float, ay: float, az: float) -> str:
    """传感器数据诊断：t=℃ h=%RH ax/ay/az=m/s²"""
    from tools.measurement_tools import sensor_data_diagnose
    return sensor_data_diagnose(t, h, ax, ay, az)


# ── 硬件工具 ────────────────────────────────────────────

@mcp.tool()
def read_sensors() -> str:
    """读取 ESP32 传感器实时数据（DHT22 温湿度 + MPU6050 三轴加速度）"""
    from tools.sensor_reader import get_sensor_data
    return get_sensor_data()


@mcp.tool()
def anomaly_detect(class_name: str = "") -> str:
    """明确类别后采集一次图像并检测；不对未知类别回退。"""
    from perception.measurement_context import new_context, bind_percepts, detect_bound_anomaly
    from perception.bus import collect, to_state
    if not isinstance(class_name,str) or not class_name.strip() or class_name == "default":
        return json.dumps({"execution_status":"needs_input","error":"请指定已建库的 class_name"},ensure_ascii=False)
    context = new_context({"class_id":class_name})
    context = bind_percepts(context,to_state(collect(want_camera=True)))
    return json.dumps(detect_bound_anomaly(context),ensure_ascii=False)


# ── 多模态工具 ──────────────────────────────────────────

@mcp.tool()
def vision_understand(image_path: str, question: str = "") -> str:
    """视觉理解：分析本机图片内容（qwen3-vl-flash，失败自动降级）"""
    from tools.multimodal_tools import vision_understand as _vu
    return _vu(image_path, question)


@mcp.tool()
def inspect_image(image_path: str) -> str:
    """结构化工件视觉分析：工件类型/材质/表面状态/缺陷/检测建议（JSON）"""
    from tools.multimodal_tools import inspect_image as _ii
    return _ii(image_path)


@mcp.tool()
def speech_to_text(audio_path: str) -> str:
    """语音识别：本机音频文件 → 文字（fun-asr）"""
    from tools.multimodal_tools import speech_to_text as _stt
    return _stt(audio_path)


@mcp.tool()
def text_to_speech(text: str) -> str:
    """语音合成：文字 → mp3 文件路径（cosyvoice）"""
    from tools.multimodal_tools import text_to_speech as _tts
    return _tts(text)


@mcp.tool()
def generate_image(prompt: str) -> str:
    """文生图：提示词 → 本机 png 文件路径"""
    from tools.multimodal_tools import generate_image as _gi
    return _gi(prompt)


# ── 记忆 / 合规 ─────────────────────────────────────────

@mcp.tool()
def memory_recall(query: str, top_k: int = 3) -> str:
    """召回本系统历史判定案例（长期记忆，支持经验一致性比对）"""
    from memory import recall, format_memories
    return format_memories(recall(query, top_k=top_k)) or "无相似历史案例"


@mcp.tool()
def compliance_report() -> str:
    """数据合规自检报告（数据不出境 / 人脸脱敏 / 审计留痕）"""
    from compliance.gate import compliance_report as _cr
    return json.dumps(_cr(), ensure_ascii=False, indent=2)


# ── 启动 ────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Equis MCP Server")
    ap.add_argument("--transport", choices=["stdio", "sse"], default="stdio")
    ap.add_argument("--port", type=int, default=8931)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    if args.transport == "sse":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="sse")
    else:
        mcp.run()  # stdio


if __name__ == "__main__":
    main()
