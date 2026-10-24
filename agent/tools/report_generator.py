"""
PDF 测量报告生成器 —— 基于报告生成
输出专业格式的测量不确定度评定报告
"""
import os, sys, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fpdf import FPDF
from config import DATA_DIR


def _get_kb_count() -> int:
    """获取知识库文档数量（v4 维护：替代硬编码 43）

    Returns:
        知识库文档数量，失败返回 0

    对应文档: v4 变更记录 #11 - 硬编码修正
    """
    try:
        from agent.tools.kb_tools import _get_kb_client
        return _get_kb_client().get_collection("metrology_kb").count()
    except Exception:
        return 0


def generate_report(conversation: str, output_path: str = None) -> str:
    """
    根据对话内容生成 PDF 测量报告
    conversation: 完整的对话文本（用户问题 + AI回答）
    返回 PDF 文件路径
    """
    if output_path is None:
        output_path = os.path.join(DATA_DIR, f"report_{int(time.time())}.pdf")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # 解析对话内容
    user_q = extract_user_question(conversation)
    ai_answer = extract_ai_answer(conversation)
    data = parse_measurement_data(ai_answer)

    pdf = FPDF()
    pdf.add_page()

    # 注册中文字体
    FONT = "C:/Windows/Fonts/msyh.ttc"
    pdf.add_font("ZH", "", FONT, uni=True)
    pdf.add_font("ZH", "B", FONT, uni=True)
    FONT_N = "ZH"

    # ── 标题 ──
    pdf.set_font(FONT_N, "B", 20)
    pdf.set_text_color(30, 41, 59)
    pdf.cell(0, 12, "Equis - AI \u6d4b\u91cf\u62a5\u544a", ln=True, align="C")
    pdf.set_font(FONT_N, "", 11)
    pdf.set_text_color(100, 116, 139)
    pdf.cell(0, 8, "\u57fa\u4e8eAI\u667a\u80fd\u4f53\u7684\u591a\u4f20\u611f\u5668\u878d\u5408\u7cbe\u5bc6\u6d4b\u91cf\u7cfb\u7edf", ln=True, align="C")
    pdf.ln(4)

    # 分割线
    pdf.set_draw_color(59, 130, 246)
    pdf.set_line_width(0.8)
    pdf.line(15, pdf.get_y(), 195, pdf.get_y())
    pdf.ln(6)

    # ── 基本信息 ──
    pdf.set_font(FONT_N, "B", 13)
    pdf.set_text_color(30, 41, 59)
    pdf.cell(0, 8, "1. \u57fa\u672c\u4fe1\u606f", ln=True)
    pdf.ln(2)

    info_fields = [
        ("报告编号", f"EQUIS-{time.strftime('%Y%m%d')}-{os.getpid()%10000:04d}"),
        ("生成日期", time.strftime("%Y-%m-%d %H:%M")),
        ("依据标准", "JJF 1059.1-2012 测量不确定度评定与表示"),
        ("系统模型", "qwen3.7-max (阿里云百炼)"),
        ("检索知识库", f"{_get_kb_count()}项 JJF/JJG/GB/ISO 计量标准"),
    ]
    for label, value in info_fields:
        pdf.set_font(FONT_N, "B", 10)
        pdf.set_text_color(71, 85, 105)
        pdf.cell(35, 7, f"{label}:")
        pdf.set_font(FONT_N, "", 10)
        pdf.set_text_color(30, 41, 59)
        pdf.cell(0, 7, value, ln=True)
    pdf.ln(4)

    # ── 用户问题 ──
    pdf.set_font(FONT_N, "B", 13)
    pdf.set_text_color(30, 41, 59)
    pdf.cell(0, 8, "2. \u6d4b\u91cf\u95ee\u9898", ln=True)
    pdf.ln(2)
    pdf.set_font(FONT_N, "", 10)
    pdf.set_text_color(30, 41, 59)
    pdf.multi_cell(0, 6, user_q or "\u672a\u63d0\u4f9b")
    pdf.ln(4)

    # ── 不确定度评定 ──
    pdf.set_font(FONT_N, "B", 13)
    pdf.set_text_color(30, 41, 59)
    pdf.cell(0, 8, "3. \u4e0d\u786e\u5b9a\u5ea6\u8bc4\u5b9a", ln=True)
    pdf.ln(2)

    if data.get("sources"):
        # 表格头
        pdf.set_fill_color(239, 246, 255)
        pdf.set_font(FONT_N, "B", 9)
        pdf.set_text_color(30, 41, 59)
        col_w = [55, 20, 40, 30, 35]
        headers = ["\u4e0d\u786e\u5b9a\u5ea6\u6765\u6e90", "\u7c7b\u578b", "\u5206\u5e03", "ui (mm)", "ci"]
        for i, (h, w) in enumerate(zip(headers, col_w)):
            pdf.cell(w, 7, h, border=1, fill=True, align="C")
        pdf.ln()

        # 表格数据
        pdf.set_font(FONT_N, "", 9)
        for src in data["sources"]:
            for i, (val, w) in enumerate(zip(src, col_w)):
                pdf.cell(w, 7, str(val), border=1, align="C" if i > 0 else "L")
            pdf.ln()
        pdf.ln(3)

    # 计算结果
    for key, label in [("uc", "\u5408\u6210\u6807\u51c6\u4e0d\u786e\u5b9a\u5ea6 uc"),
                        ("U", "\u6269\u5c55\u4e0d\u786e\u5b9a\u5ea6 U (k=2)"),
                        ("result", "\u6d4b\u91cf\u7ed3\u679c")]:
        if key in data:
            pdf.set_font(FONT_N, "B", 10)
            pdf.set_text_color(30, 41, 59)
            pdf.cell(50, 7, label + ":")
            pdf.set_font(FONT_N, "", 10)
            pdf.set_text_color(37, 99, 235)
            pdf.cell(0, 7, str(data[key]), ln=True)

    pdf.ln(4)

    # ── 完整分析 ──
    pdf.set_font(FONT_N, "B", 13)
    pdf.set_text_color(30, 41, 59)
    pdf.cell(0, 8, "4. \u8be6\u7ec6\u5206\u6790", ln=True)
    pdf.ln(2)
    pdf.set_font(FONT_N, "", 9)
    pdf.set_text_color(30, 41, 59)

    # 输出 AI 回答内容, 每行截断
    lines = ai_answer.split("\n")
    for line in lines[:40]:  # 最多40行
        pdf.multi_cell(0, 5, line.strip() or " ")
    pdf.ln(4)

    # ── 页脚 ──
    pdf.set_draw_color(203, 213, 225)
    pdf.set_line_width(0.4)
    pdf.line(15, 270, 195, 270)
    pdf.set_font(FONT_N, "", 8)
    pdf.set_text_color(148, 163, 184)
    pdf.set_y(272)
    pdf.cell(0, 6, "\u672c\u62a5\u544a\u7531 Equis AI\u6d4b\u91cf\u667a\u80fd\u4f53\u81ea\u52a8\u751f\u6210 | \u4e2d\u56fd\u8ba1\u91cf\u5927\u5b66", align="C")

    pdf.output(output_path)
    return output_path


def extract_user_question(text: str) -> str:
    """提取用户问题"""
    for line in text.split("\n"):
        line = line.strip()
        if line and not line.startswith("【") and not line.startswith("==="):
            return line[:200]
    return "未知"


def extract_ai_answer(text: str) -> str:
    """提取 AI 回答"""
    return text[:3000] if text else ""


def parse_measurement_data(text: str) -> dict:
    """从 AI 回答中解析测量数据"""
    data = {"sources": []}

    # 提取不确定度来源（常见格式：u1, u2 或 来源名称 + 值）
    patterns = [
        (r'(\S+?(?:误差|影响|重复性|分辨率|示值|漂移|温度))[：:]\s*[uU]?\s*=\s*([\d.]+)\s*(mm|μm|um)', None),
        (r'[uU]\s*[\u2080-\u2089\u00B9\u00B2\u00B3]+\s*=\s*([\d.]+)\s*(mm|μm|um)', "A\u7c7b"),
        (r'B\u7c7b.*?=\s*([\d.]+)\s*(mm|μm|um)', "B\u7c7b"),
    ]

    for pattern, default_type in patterns:
        matches = re.findall(pattern, text)
        for i, m in enumerate(matches):
            if default_type:
                val, unit = m[0], m[1]
                label = f"B\u7c7b\u6765\u6e90{i+1}"
            else:
                label, val, unit = m

            val_mm = float(val)
            if unit in ("μm", "um"):
                val_mm /= 1000

            data["sources"].append([
                label[:20],
                "A\u7c7b" if "u1" in text.lower() or "\u91cd\u590d" in text else "B\u7c7b",
                "\u5747\u5300" if "\u5206\u8fa8" in text or "\u8bef\u5dee" in text else "\u6b63\u6001",
                f"{val_mm:.5f}",
                "1" if val_mm > 0.001 else "1"
            ])
            if len(data["sources"]) >= 5:
                break
        if data["sources"]:
            break

    # 提取 uc
    uc_match = re.search(r'[uU]c\s*=\s*([\d.]+)\s*(mm|μm|um)?', text)
    if uc_match:
        val = float(uc_match.group(1))
        if uc_match.group(2) in ("μm", "um"):
            val /= 1000
        data["uc"] = f"{val:.5f} mm"

    # 提取 U
    U_match = re.search(r'[Uu]\s*=\s*([\d.]+)\s*(mm|μm|um)?', text)
    if U_match:
        val = float(U_match.group(1))
        if U_match.group(2) in ("μm", "um"):
            val /= 1000
        data["U"] = f"{val:.5f} mm (k=2)"

    # 提取最终结果
    result_match = re.search(r'[dDLlYy]\s*=\s*\(?([\d.]+)\s*\u00B1\s*([\d.]+)\)?\s*(mm|μm|um)?', text)
    if result_match:
        data["result"] = f"{result_match.group(1)} \u00B1 {result_match.group(2)} mm (k=2)"

    return data
