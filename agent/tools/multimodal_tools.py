"""多模态工具 —— 视觉理解 / 语音识别 / 语音合成 / 图像生成

百炼 API（境内），全部走兼容模式。
2026-09-12 升级：
  - 模型名统一由 config 提供（qwen3-vl-flash 主 / qwen-vl-plus 降级）
  - 新增 inspect_image()：面向计量检测的结构化视觉分析（供 Agent 工具调用）
"""
import os, sys, base64, json, time, re, requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import (
    LLM_API_KEY, LLM_BASE_URL, DATA_DIR,
    LLM_VL_MODEL, LLM_VL_FALLBACK, LLM_ASR_MODEL, LLM_TTS_MODEL, LLM_T2I_MODEL,
)
# 数据不出境 Gate：所有对外请求都必须先过这里（P4-42 接线的落地）
from compliance.gate import (
    guard_request, assert_resource_allowed,
    record_outbound, ComplianceError,
)

KEY = LLM_API_KEY
BASE = LLM_BASE_URL

_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "bmp": "image/bmp", "gif": "image/gif",
}


# ── 视觉 ────────────────────────────────────────────────

def _encode_image(image_path: str) -> str:
    """图片文件 → data URI"""
    ext = os.path.splitext(image_path)[1].lower().lstrip(".")
    mime = _MIME.get(ext, "image/jpeg")
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return f"data:{mime};base64,{b64}"


def _vl_call(model: str, data_uri: str, prompt: str, max_tokens: int = 1000, timeout: int = 45):
    """单次视觉调用。返回 (ok: bool, text: str)

    显式关闭思考模式（enable_thinking=False）：视觉理解属结构化抽取任务，
    关思考可提速约 2 倍且不影响字段准确率。
    """
    body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_uri}},
                {"type": "text", "text": prompt},
            ],
        }],
        "max_tokens": max_tokens,
        "enable_thinking": False,
    }
    try:
        # 数据不出境 Gate：每个对外请求前断言目标域名（越界即抛 ComplianceError，
        # 由下面的 except 捕获成"调用失败"，不会静默发出去）
        resp = guard_request(
            "POST", f"{BASE}/chat/completions",
            headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
            json=body, timeout=timeout,
        )
        if resp.status_code == 200:
            return True, resp.json()["choices"][0]["message"]["content"]
        return False, f"[{model}] HTTP {resp.status_code}: {resp.text[:180]}"
    except Exception as e:
        return False, f"[{model}] {type(e).__name__}: {str(e)[:150]}"


def vision_understand(image_path: str, question: str = "") -> str:
    """视觉理解：图片 → 描述。主模型 qwen3-vl-flash，失败自动降级 qwen-vl-plus。"""
    if not os.path.exists(image_path):
        return f"文件不存在: {image_path}"
    prompt = question or "请描述这张图片的内容，提取所有可观测的关键信息。"
    uri = _encode_image(image_path)
    ok, out = _vl_call(LLM_VL_MODEL, uri, prompt)
    if ok:
        return out
    ok2, out2 = _vl_call(LLM_VL_FALLBACK, uri, prompt)
    return out2 if ok2 else f"视觉理解失败：\n{out}\n{out2}"


_INSPECT_PROMPT = (
    "你是工业计量检测的视觉分析助手。请分析这张图片，严格按以下 JSON 格式输出，"
    "不要输出任何额外解释、不要使用 markdown 代码块：\n"
    "{\n"
    '  "object": "图中主体名称。若是工件/零件填名称（如 轴、齿轮、法兰、PCB板）；'
    '若是设备/场景则填 scene: 后接简述（如 scene:双目视觉测量装置）",\n'
    '  "material_hint": "材质推测（如 钢/铝/塑料/复合材料，无法判断填 unknown）",\n'
    '  "surface_state": "表面状态描述（光洁/有划痕/有锈蚀/有油污等）",\n'
    '  "visible_defects": ["可见缺陷1", "可见缺陷2"],\n'
    '  "measurable_features": ["可测量特征1", "可测量特征2"],\n'
    '  "inspection_suggestion": "建议的检测项目与标准类型，例如「几何量尺寸检验（外径/孔径）」'
    '「表面缺陷检验」「直线度/同轴度等形位误差」，不要写具体标准编号",\n'
    '  "confidence": 0.0\n'
    "}\n"
    "硬性约束：\n"
    "1. inspection_suggestion 只描述检测项目与标准类型，**严禁编造或推测具体标准编号**"
    "（如 JJG/JJF/GB/T 编号）；标准编号由后续知识库检索环节给出。\n"
    "2. 只描述图中确实可见的内容；不确定的字段填 unknown 或空数组，不要臆测。\n"
    "3. 若图中主体是设备/装置而非工件，object 以 scene: 开头，visible_defects 填 []。"
)


from tools.json_output import parse_json_object as _parse_json_loose


def inspect_image(image_path: str, context: str = "") -> str:
    """面向计量检测的结构化视觉分析（供 Agent 工具调用）。

    返回 JSON 字符串：工件类型 / 表面状态 / 可见缺陷 / 可测量特征 / 检测建议。
    """
    if not os.path.exists(image_path):
        return json.dumps({"error": f"文件不存在: {image_path}"}, ensure_ascii=False)

    prompt = _INSPECT_PROMPT
    if context:
        prompt += f"\n补充上下文（来自传感器/对话）：{context}"

    uri = _encode_image(image_path)
    ok, out = _vl_call(LLM_VL_MODEL, uri, prompt, max_tokens=800)
    if not ok:
        ok, out = _vl_call(LLM_VL_FALLBACK, uri, prompt, max_tokens=800)
    if not ok:
        return json.dumps({"error": out}, ensure_ascii=False)

    data = _parse_json_loose(out)
    if not data:
        data = {"raw": out[:600], "parse_failed": True}
    data["_image"] = os.path.basename(image_path)
    return json.dumps(data, ensure_ascii=False)


# ── 语音 / 图像生成 ──────────────────────────────────────

def speech_to_text(audio_path: str) -> str:
    """语音识别：音频文件 → 文字"""
    if not os.path.exists(audio_path):
        return f"文件不存在: {audio_path}"
    url = f"{BASE}/audio/transcriptions"
    with open(audio_path, "rb") as f:
        resp = guard_request(
            "POST", url,
            headers={"Authorization": f"Bearer {KEY}"},
            files={"file": (os.path.basename(audio_path), f, "audio/wav")},
            data={"model": LLM_ASR_MODEL},
            timeout=45,
        )
    if resp.status_code == 200:
        return resp.json().get("text", "")
    return f"[语音识别失败] {resp.status_code}: {resp.text[:200]}"


def text_to_speech(text: str, output_path: str = None) -> str:
    """语音合成：文字 → mp3 文件路径"""
    if not text.strip():
        return "[语音合成失败] 文本为空"
    if output_path is None:
        output_path = os.path.join(DATA_DIR, "tts_output.mp3")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    resp = guard_request(
        "POST", f"{BASE}/audio/speech",
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        json={
            "model": LLM_TTS_MODEL,
            "input": text[:1000],
            "voice": "longxiaochun",
            "response_format": "mp3",
        },
        timeout=45,
    )
    if resp.status_code == 200:
        with open(output_path, "wb") as f:
            f.write(resp.content)
        return output_path
    return f"[语音合成失败] {resp.status_code}: {resp.text[:200]}"


def generate_image(prompt: str, output_path: str = None, size: str = "1024x1024") -> str:
    """文生图：提示词 → png 文件路径"""
    if not prompt.strip():
        return "[图像生成失败] 提示词为空"
    if output_path is None:
        output_path = os.path.join(DATA_DIR, f"gen_{int(time.time())}.png")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # ── 2026-10-17 重写 ────────────────────────────────────
    # 原实现走兼容模式同步端点 {BASE}/images/generations，实测该端点在本账号下
    # **恒返回 404 且响应体为空**（换任何模型名都一样），即端点本身不可用，
    # 因此文生图功能长期是坏的。
    #
    # 零成本探测（故意不传 prompt，以便区分"模型不存在"与"端点/参数问题"）结果：
    #   /compatible-mode/v1/images/generations  -> 404（空体）
    #   /api/v1/services/aigc/text2image/image-synthesis：
    #       wanx2.1-t2i-turbo / wanx2.1-t2i-plus / wanx-v1
    #         -> 400 {"code":"InvalidParameter","message":"input.prompt should not be null"}
    #   说明：原生端点正确、模型存在、鉴权通过，只是缺 prompt。
    #
    # 因此改为调用**原生异步接口**：
    #   1) POST 创建任务（需 X-DashScope-Async: enable）
    #   2) 轮询 /api/v1/tasks/{task_id} 直到 SUCCEEDED
    #   3) 下载 results[0].url（经 assert_resource_allowed 白名单校验）
    t2i_root = LLM_BASE_URL.split("/compatible-mode")[0]   # https://dashscope.aliyuncs.com
    create_url = f"{t2i_root}/api/v1/services/aigc/text2image/image-synthesis"
    # 原生接口的尺寸写法是 1024*1024（星号），不是 1024x1024
    native_size = str(size).replace("x", "*").replace("X", "*")
    headers = {
        "Authorization": f"Bearer {KEY}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }
    try:
        resp = guard_request(
            "POST", create_url, headers=headers,
            json={
                "model": LLM_T2I_MODEL,
                "input": {"prompt": prompt},
                "parameters": {"size": native_size, "n": 1},
            },
            timeout=30,
        )
    except ComplianceError:
        raise
    if resp.status_code != 200:
        return f"[图像生成失败] 创建任务 HTTP {resp.status_code}: {resp.text[:200]}"

    try:
        task_id = resp.json()["output"]["task_id"]
    except Exception:
        return f"[图像生成失败] 响应缺少 task_id: {resp.text[:200]}"

    # 轮询（文生图是异步的，实测通常 10-40 秒）
    poll_url = f"{t2i_root}/api/v1/tasks/{task_id}"
    deadline = time.time() + 180
    last = ""
    while time.time() < deadline:
        time.sleep(3)
        try:
            pr = guard_request("GET", poll_url, headers=headers, timeout=20)
        except ComplianceError:
            raise
        if pr.status_code != 200:
            last = f"HTTP {pr.status_code}: {pr.text[:160]}"
            continue
        data = pr.json()
        status = (data.get("output") or {}).get("task_status") or data.get("task_status")
        if status == "SUCCEEDED":
            results = (data.get("output") or {}).get("results") or []
            if not results:
                return f"[图像生成失败] 任务成功但无结果: {str(data)[:200]}"
            img_url = results[0].get("url", "")
            if not img_url:
                return f"[图像生成失败] 结果缺少 url: {str(results[0])[:160]}"
            # 资源下载 Gate：URL 来自外部响应体（远端决定我们连谁），
            # 单独用 assert_resource_allowed：仅 https + 阿里云境内存储，
            # 并拒绝香港/海外 region。
            assert_resource_allowed(img_url)
            record_outbound(img_url)
            img_resp = requests.get(img_url, timeout=60)
            with open(output_path, "wb") as f:
                f.write(img_resp.content)
            return output_path
        if status in ("FAILED", "CANCELED", "UNKNOWN"):
            reason = (data.get("output") or {}).get("message") or str(data)[:200]
            return f"[图像生成失败] 任务 {status}: {reason}"
        last = status or last
    return f"[图像生成失败] 等待超时（180s），最后状态: {last or '未知'}"
