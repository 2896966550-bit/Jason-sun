"""Equis 后端 —— Flask + 百炼 Agent + 本地工具"""
import sys, os, json, time, queue, threading, re, math
from pathlib import Path
import uuid

# 路径修复：同时注册 agent/ 与项目根目录，保证 agent.omni_ad / agent.tools 等前缀导入稳定
_AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_AGENT_DIR)
for _p in (_AGENT_DIR, _PROJECT_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# v4 维护：SSE 序列化性能优化（orjson 带降级）
try:
    import orjson
    def _dumps_json(obj) -> str:
        """orjson 序列化（性能优化），失败时降级到标准 json

        对应文档: v4 变更记录 #6 - SSE 序列化性能优化
        """
        return orjson.dumps(obj).decode('utf-8')
except ImportError:
    def _dumps_json(obj) -> str:
        """标准 json 序列化（orjson 不可用时的降级路径）"""
        return json.dumps(obj, ensure_ascii=False)

from flask import Flask, request, jsonify, send_file, Response, send_from_directory, stream_with_context
from flask_cors import CORS
from werkzeug.utils import secure_filename
# 维护（2026-10-02）：.env 加载已统一由 config._load_dotenv() 在导入时完成
# （encoding="utf-8"，且 os.environ.setdefault 不覆盖既有值）。
# 本文件原先在第 26-33 行另有一份手写副本：① 与 config.py 重复；
# ② 用裸 open() 未指定 encoding，在 Windows 默认 GBK 下遇到非 ASCII 值会抛
# UnicodeDecodeError 且无 try 包裹，会直接让 server 启动失败。
from config import DATA_DIR, UPLOAD_DIR, AGENT_ENGINE
from compliance.gate import audit_log, compliance_report
from agent_core import chat as local_chat
from chat_history import save_message, get_recent_unique_questions, get_full_conversation
from tools.multimodal_tools import (
    vision_understand, speech_to_text, text_to_speech, generate_image,
)
from tools.report_generator import generate_report
from http import HTTPStatus
from http_support import AssetVersion, RequestInputError, json_object, text_field, positive_limit

# 百炼 Agent 配置
BAILIAN_APP_ID = os.getenv("BAILIAN_APP_ID", "")
BAILIAN_API_KEY = os.getenv("BAILIAN_API_KEY", "")

app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app)
BASE = os.path.dirname(os.path.abspath(__file__))


@app.errorhandler(RequestInputError)
def invalid_request(error):
    return jsonify({"execution_status": "invalid_input", "error": str(error)}), 400


def _chat_input():
    data = json_object()
    message = text_field(data, "message")
    image = data.get("image_path")
    if image is not None and not isinstance(image, str):
        raise RequestInputError("image_path 必须是字符串")
    metadata = data.get("measurement_context")
    from perception.measurement_context import new_context
    try:
        new_context(metadata, query=message)
    except ValueError as error:
        raise RequestInputError(str(error)) from error
    return data, message, image or None, metadata




# ── 静态资源版本号（2026-10-17）────────────────────────────
# 问题：下方 after_request 让 /static/ 跳过 no-cache，浏览器遂按启发式
# 缓存 CSS/JS。改动样式后刷新页面仍拿旧文件 —— 本轮我自己就踩到过
# （改了 cards.js，截图却仍是旧渲染，排查了半天）。
# 方案：给 HTML 里的 css/js 引用自动追加 ?v=<内容指纹>。
# 指纹来自文件 mtime+size 的哈希，改动即变、无需手工升版本号；
# 计算后缓存 3 秒，避免每个请求都遍历目录。
_asset_versions = AssetVersion(Path(BASE)/"static")


def _asset_version() -> str:
    return _asset_versions.get()


_ASSET_REF = re.compile(r'(href|src)="(/static/(?:css|js)/[^"?]+)"')


def _serve_html_with_version(filename: str):
    """读取 HTML，给 css/js 引用加上版本号后返回（页面本身仍 no-cache）"""
    try:
        with open(os.path.join(BASE, filename), encoding="utf-8") as f:
            html = f.read()
        ver = _asset_version()
        html = _ASSET_REF.sub(lambda m: '%s="%s?v=%s"' % (m.group(1), m.group(2), ver), html)
        resp = Response(html, mimetype="text/html")
    except FileNotFoundError:
        return jsonify({"error": "页面不存在"}), 404
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/")
def index():
    return _serve_html_with_version("equis.html")


@app.route("/v2")
def v2():
    return _serve_html_with_version("equis-doubao.html")


@app.after_request
def add_no_cache(response):
    if request.path.startswith("/static/") or request.path.startswith("/api/data/"):
        return response
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data, msg, image_path, metadata = _chat_input()
    if not msg.strip():
        raise RequestInputError("message 不能为空")
    result = local_chat(msg,image_path=data.get("image_path"),measurement_context=metadata)
    return jsonify({"response": result})


@app.route("/api/chat/stream", methods=["POST"])
def api_chat_stream():
    """流式输出聊天（SSE）。支持 image_path（多模态入链）。"""
    data, msg, image_path, metadata = _chat_input()
    if not msg.strip() and not image_path:
        raise RequestInputError("message 和 image_path 不能同时为空")
    audit_log("chat", f"问: {msg[:150]}" + (f" | 图: {os.path.basename(image_path)}" if image_path else ""))

    def generate():
        full_text = ""
        result = None
        try:
            if AGENT_ENGINE == "graph":
                # ── graph 引擎：节点级流式（partial 事件，分支完成即推送初步结论） ──
                from graph.runner import stream_run
                from chat_history import get_recent_pairs, save_message
                from agent_core import _strip_cards
                recent = get_recent_pairs(3)
                save_message("user", msg)
                for ev in stream_run(msg, image_path=image_path, history=recent,
                                     measurement_context=metadata):
                    if "agent_status" in ev:
                        yield f"data: {json.dumps({'agent_status': ev['agent_status'], 'done': False})}\n\n"
                    elif "partial" in ev:
                        yield f"data: {json.dumps({'partial': ev['partial'], 'done': False})}\n\n"
                    elif "final" in ev:
                        result = ev["final"]
                        # 随 final 一并下发运行元数据（前端 inspector 的
                        # 运行时间轴据此拉 /api/trace/<run_id> 做真实耗时校准）。
                        # 这些字段此前被丢弃，前端只能靠本地计时估算。
                        meta = {k: ev.get(k) for k in
                                ("run_id", "trace", "plan", "sources",
                                 "router_decision", "router_notes", "error", "measurement_context")
                                if ev.get(k) is not None}
                        if meta:
                            yield f"data: {json.dumps({'run_meta': meta, 'done': False})}\n\n"
                        break
                if result is not None:
                    save_message("assistant", _strip_cards(result))
            else:
                # ── legacy 引擎：队列 + 线程 ──
                q = queue.Queue()

                def _run():
                    def status_cb(agent_id, state):
                        q.put(("agent_status", {"agent": agent_id, "state": state}))
                    try:
                        result = local_chat(msg, on_status=status_cb, image_path=image_path,
                                            measurement_context=metadata)
                        q.put(("result", result))
                    except Exception as e:
                        q.put(("error", str(e)))

                t = threading.Thread(target=_run, daemon=True)
                t.start()

                while True:
                    try:
                        msg_type, payload = q.get(timeout=1.0)
                        if msg_type == "agent_status":
                            yield f"data: {json.dumps({'agent_status': payload, 'done': False})}\n\n"
                        elif msg_type == "result":
                            result = payload
                            break
                        elif msg_type == "error":
                            result = f"[错误] {payload}"
                            break
                    except queue.Empty:
                        if not t.is_alive():
                            break
                        continue

                t.join(timeout=3)

            if result:
                # 审计修复（P0-10）：原实现是"人工打字机" —— 把已生成的完整
                # 文本切成约 300 片，每片 time.sleep(min(0.015, ...))，对
                # 1500 字的回答白等约 4.5 秒（纯观感开销，与模型生成速度无关），
                # 期间还占着服务端线程。
                #
                # 现改为分 12 片一次性下发、不再 sleep：首屏内容出现时间从
                # 约 8s 降到接近真实推理耗时。真正的逐 token 流式需要把
                # _llm_merge 改为 stream=True 并透传（路线图 P1-12），
                # 届时此处直接转发即可。
                n = len(result)
                step = max(1, n // 12)
                for i in range(0, n, step):
                    piece = result[i:i + step]
                    full_text += piece
                    yield f"data: {json.dumps({'token': piece, 'done': False})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'token': f'[错误] {str(e)}', 'done': True})}\n\n"
            return

        yield f"data: {json.dumps({'token': '', 'done': True})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── 历史乱码过滤（C4：统一到一处）──────────────────────────
# 背景：早期用 PowerShell 的 Invoke-WebRequest 发中文请求体，被按 ASCII 编码，
# 中文全变成 '?' 落库，产生 "JJG 21 ??????"、"????????????" 这类条目。
# 处置原则：**只做读取时过滤，不删除用户数据**（可逆）。
# 判定条件：疑问号 >= 3 且不含任何汉字。
# 2026-10-17：从 /api/history 的内联实现抽成公共函数，
# 并让 /api/conversation 同样生效 —— 否则点开一条正常历史，
# 对话里仍可能出现乱码轮次。
_GARBLED_Q_RE = re.compile(r"\?{3,}")


def is_garbled_text(s) -> bool:
    """判断一条历史文本是否是编码事故产生的乱码"""
    t = str(s or "")
    if not _GARBLED_Q_RE.search(t):
        return False
    return not re.search(r"[\u4e00-\u9fff]", t)


def filter_garbled(items):
    return [x for x in (items or []) if not is_garbled_text(x)]


@app.route("/api/history", methods=["GET"])
def api_history():
    qs = filter_garbled(get_recent_unique_questions(30))
    return jsonify({"questions": qs})


@app.route("/api/conversation", methods=["POST"])
def api_conversation():
    data = json_object()
    question = text_field(data, "question")
    # 乱码提问直接返回空对话（不展示、也不删除）
    if is_garbled_text(question):
        return jsonify({"conversation": []})
    conv = get_full_conversation(question) or []
    # 逐条过滤：只保留 role/content 结构完整且内容不是乱码的轮次
    cleaned = []
    for m in conv:
        if isinstance(m, dict):
            content = m.get("content", "")
            if is_garbled_text(content):
                continue
        cleaned.append(m)
    return jsonify({"conversation": cleaned})


# ── 统一上传入口（多模态入链）──

@app.route("/api/upload", methods=["POST"])
def api_upload():
    """保存上传图片到 UPLOAD_DIR，返回路径供 /api/chat/stream 使用"""
    if "file" not in request.files:
        return jsonify({"error": "未上传文件"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "文件名为空"}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
        return jsonify({"error": f"不支持的图片格式: {ext}"}), 400
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    name = f"up_{uuid.uuid4().hex}{ext}"
    path = os.path.join(UPLOAD_DIR, name)
    f.save(path)
    audit_log("upload", f"图片上传: {name} ({os.path.getsize(path)} bytes)")
    return jsonify({"ok": True, "path": path, "name": name, "url": f"/api/data/uploads/{name}"})


@app.route("/api/compliance", methods=["GET"])
def api_compliance():
    """数据合规与伦理风险防控自检报告（数据不出境 / 人脸脱敏 / 审计留痕）"""
    return jsonify(compliance_report())


@app.route("/api/trace", methods=["GET"])
def api_trace():
    """最近 n 次运行轨迹摘要（可观测性）"""
    from observability import trace
    n = positive_limit(request.args.get("n"))
    return jsonify({"runs": trace.recent(min(n, 100))})


@app.route("/api/trace/<int:run_id>", methods=["GET"])
def api_trace_detail(run_id: int):
    """单次运行的完整节点轨迹（各节点耗时，可测试性证据）"""
    from observability import trace
    d = trace.detail(run_id)
    if not d:
        return jsonify({"error": "run not found"}), 404
    return jsonify(d)


@app.route("/api/architecture", methods=["GET"])
def api_architecture():
    """系统架构：LangGraph 状态图 Mermaid 源码（前端渲染）"""
    from graph.build import export_mermaid
    return jsonify({"mermaid": export_mermaid()})


# ── 多模态 API ──

@app.route("/api/vision", methods=["POST"])
def api_vision():
    if "image" not in request.files:
        return jsonify({"error": "未上传图片"}), 400
    f = request.files["image"]
    q = request.form.get("question", "")
    path = os.path.join(DATA_DIR, f"upload_{uuid.uuid4().hex}_{secure_filename(f.filename)}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f.save(path)
    result = vision_understand(path, q)
    return jsonify({"response": result})


@app.route("/api/speech-to-text", methods=["POST"])
def api_stt():
    if "audio" not in request.files:
        return jsonify({"error": "未上传音频"}), 400
    f = request.files["audio"]
    path = os.path.join(DATA_DIR, f"stt_{uuid.uuid4().hex}_{secure_filename(f.filename)}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f.save(path)
    result = speech_to_text(path)
    return jsonify({"text": result})


@app.route("/api/text-to-speech", methods=["POST"])
def api_tts():
    data = json_object()
    text = text_field(data, "text", required=True)
    path = text_to_speech(text)
    return jsonify({"file": path, "url": "/api/tts-audio"})


@app.route("/api/tts-audio")
def api_tts_audio():
    path = os.path.join(DATA_DIR, "tts_output.mp3")
    if os.path.exists(path):
        return send_file(path, mimetype="audio/mpeg")
    return jsonify({"error": "文件不存在"}), 404


@app.route("/api/generate-image", methods=["POST"])
def api_gen_image():
    data = json_object()
    prompt = text_field(data, "prompt", required=True)
    path = generate_image(prompt)
    if not path or not os.path.exists(path):
        # generate_image 失败时返回的是 "[图像生成失败] ..." 文本，不是路径
        return jsonify({"error": str(path)[:300]}), 502
    # 同时给出可直接用于 <img src> 的 URL：
    # 原先只返回本地文件路径，浏览器无法使用（前端得自己猜 /api/data/ 前缀）。
    return jsonify({"file": path, "url": "/api/data/" + os.path.basename(path)})


@app.route("/api/data/<path:filename>")
def api_data_file(filename):
    """数据/上传文件访问（支持 uploads/ 子目录）"""
    # Only browser-facing media/reports are downloadable. Runtime databases,
    # checkpoint files and configuration must not be exposed by this route.
    allowed = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".pdf",
               ".mp3", ".wav", ".ogg", ".m4a"}
    if Path(filename.replace("\\", "/")).suffix.lower() not in allowed:
        return jsonify({"error": "文件不存在"}), 404
    return send_from_directory(DATA_DIR, filename)


@app.route("/api/generate-report", methods=["POST"])
def api_report():
    data = json_object()
    conversation = text_field(data, "conversation", required=True)
    try:
        path = generate_report(conversation)
        filename = os.path.basename(path)
        return jsonify({"file": filename, "url": f"/api/data/{filename}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sensors", methods=["GET"])
def api_sensors():
    """实时传感器数据（监控模式轮询用）"""
    try:
        from tools.sensor_reader import read_sensors_raw
        d = read_sensors_raw()
        if not isinstance(d, dict) or not d:
            return jsonify({"ok": False, "execution_status": "unavailable", "error": "传感器未连接或未收到有效数据"})
        values = {}
        for target, source, digits in (("temp", "t", 1), ("hum", "h", 1),
                                       ("ax", "ax", 3), ("ay", "ay", 3), ("az", "az", 3)):
            value = d.get(source)
            values[target] = round(value, digits) if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) else None
        ready = d.get("fresh") is True and all(v is not None for v in values.values())
        result = {"ok": ready, "execution_status": "success" if ready else "unavailable", **values,
                  "fresh": d.get("fresh"), "unit": d.get("unit"), "unit_source": d.get("unit_source"),
                  "protocol_version": d.get("protocol_version"), "firmware_version": d.get("firmware_version"),
                  "imu_model": d.get("imu_model"), "sequence": d.get("sequence"),
                  "received_at": d.get("received_at"), "hardware_synchronized": False}
        if not ready:
            result["error"] = "传感器数据不完整或已过期"
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


if __name__ == "__main__":
    print("Equis >> http://localhost:7860")
    print(f"  百炼 Agent: {BAILIAN_APP_ID}")
    host = os.getenv("EQUIS_HOST", "127.0.0.1")
    port = int(os.getenv("EQUIS_PORT", "7860"))
    if not 1 <= port <= 65535:
        raise ValueError("EQUIS_PORT must be in [1, 65535]")
    app.run(host=host, port=port, debug=False)
