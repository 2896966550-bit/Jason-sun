"""合规 Gate —— 数据不出境自检 / 人脸脱敏 / 审计日志

对应大赛二号通知硬性要求：
  1. 数据合规合法，数据不出境，禁止调用境外 API 接口
  2. 敏感人脸、语音、地图等数据必须脱敏处理
  3. 材料真实性、可追溯

三件套：
  check_data_residency() —— 运行时自检：当前所有 LLM/多模态调用是否全部指向境内域名
  redact_faces()        —— 图片入库前人脸检测打码（OpenCV Haar，零新增依赖）
  audit_log()           —— 全链路审计日志（提问/上传/脱敏/工具调用均留痕）
"""
import os, sys, time, sqlite3, threading
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DATA_DIR, LLM_BASE_URL

AUDIT_DB = os.path.join(DATA_DIR, "audit.db")
_lock = threading.Lock()

#: 线程本地连接（维护 2026-10-02：_conn() 复用连接，见其 docstring 的实测数据）
_tls = threading.local()

# 允许的境内服务域名白名单（数据不出境约束的机器可校验形式）
ALLOWED_HOSTS = (
    "dashscope.aliyuncs.com",   # 阿里云百炼（LLM / VL / ASR / TTS / T2I / Embedding）
)


# ── 数据不出境自检 ──────────────────────────────────────

def check_data_residency() -> dict:
    """校验当前配置的所有外部调用是否指向境内白名单域名"""
    host = (urlparse(LLM_BASE_URL).hostname or "").lower()
    ok = any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)
    return {
        "ok": ok,
        "llm_base_url_host": host,
        "allowed_hosts": list(ALLOWED_HOSTS),
        "rule": "所有模型调用必须指向境内域名（数据不出境）",
        # P4-42：暴露本次进程**实际出站**过的域名清单
        "outbound_domains": outbound_summary().get("domains", {}),
    }


# ── 出境 Gate（P4-42）────────────────────────────────────
# 原 check_data_residency() 只读了 LLM_BASE_URL 一个常量就返回 ok，
# 属于"自检"而非"拦截"：任何新增的 requests 调用都能绕过它。
# 下面这组函数把它变成**请求级断言**，并提供实际出站域名台账。

class ComplianceError(RuntimeError):
    """违反数据不出境约束（出境域名不在白名单内）"""


def host_of(url: str) -> str:
    """从 URL 提取 host（小写，去端口）。非 http(s) 或无法解析时抛 ComplianceError。"""
    if not url or not isinstance(url, str):
        raise ComplianceError("非法 URL：%r" % (url,))
    p = urlparse(url.strip())
    if p.scheme.lower() not in ("http", "https"):
        raise ComplianceError("仅允许 http/https，收到 %r（URL=%s）" % (p.scheme, url[:120]))
    host = (p.hostname or "").lower()
    if not host:
        raise ComplianceError("无法从 URL 解析出 host：%s" % url[:120])
    return host


def is_host_allowed(url_or_host: str) -> bool:
    """判断主机是否在白名单内。

    匹配规则（**精确或子域**）：
      dashscope.aliyuncs.com        -> 允许
      api.dashscope.aliyuncs.com    -> 允许（子域）
      dashscope.aliyuncs.com.evil.com -> 拒绝（后缀伪装，必须以点号边界匹配）
      evildashscope.aliyuncs.com    -> 拒绝
    """
    try:
        s = str(url_or_host).strip()
        host = host_of(s) if "://" in s else s.lower().split(":")[0]
    except ComplianceError:
        return False
    if not host:
        return False
    for h in ALLOWED_HOSTS:
        h = h.lower()
        if host == h or host.endswith("." + h):
            return True
    return False


def assert_host_allowed(url: str) -> str:
    """断言 URL 指向白名单主机，返回 host；否则抛 ComplianceError。

    用法（推荐在**每个对外请求前**调用）：
        assert_host_allowed(url)
        requests.post(url, ...)
    """
    host = host_of(url)
    if not is_host_allowed(host):
        raise ComplianceError(
            "数据不出境约束被触发：域名 %r 不在白名单 %s 内（URL=%s）。"
            "如需新增境内服务，请先在 compliance/gate.py 的 ALLOWED_HOSTS 中登记。"
            % (host, list(ALLOWED_HOSTS), url[:160])
        )
    return host


# ── 资源下载 Gate（供应商返回的 URL）────────────────────────
# 场景：文生图接口返回一个图片 URL，我们再去 GET 它。
# 这个 URL **来自外部响应体**，是典型的"外部 URL 引用"，
# 若不加约束就相当于让远端决定我们连哪台服务器。
#
# 但它的域名规则与 API 调用不同：资源在对象存储（OSS）上，
# 不是 dashscope.aliyuncs.com。若用 API 白名单去拦，会直接打断出图功能。
#
# 因此单独一套规则：
#   允许  https + *.aliyuncs.com      （阿里云境内对象存储）
#   拒绝  境外 region（hongkong / ap-* 等），因为"数据不出境"按境内区域判定
#   拒绝  非 https、非阿里云域名
RESOURCE_HOST_SUFFIX = ".aliyuncs.com"

#: 境外/特别行政区的 region 标识 —— 命中即拒绝
_OVERSEAS_REGION_TOKENS = (
    "hongkong", "ap-southeast", "ap-northeast", "ap-south", "ap-east",
    "us-west", "us-east", "eu-central", "eu-west", "me-", "sa-",
)


def assert_resource_allowed(url: str) -> str:
    """断言"供应商返回的资源 URL"可安全下载，返回 host；否则抛 ComplianceError。

    与 assert_host_allowed 的区别：允许对象存储子域，但同样拒绝境外。
    """
    host = host_of(url)                      # 内部已强制 http/https
    if not url.lower().startswith("https://"):
        raise ComplianceError(
            "资源下载必须走 https，收到：%s" % url[:120])
    if not host.endswith(RESOURCE_HOST_SUFFIX):
        raise ComplianceError(
            "资源下载域名 %r 不在允许范围（仅 %s 的子域，即阿里云境内存储）"
            % (host, RESOURCE_HOST_SUFFIX.lstrip(".")))
    for tok in _OVERSEAS_REGION_TOKENS:
        if tok in host:
            raise ComplianceError(
                "资源位于境外区域（命中 %r）：%s —— 违反数据不出境约束"
                % (tok, host))
    return host


def guard_request(method: str, url: str, session=None, **kwargs):
    """包装 requests 出口：先断言域名，再转发，并记录实际出站域名。

    用法：
        guard_request("POST", url, json=body, timeout=30)
        # 等价于 requests.post(url, json=body, timeout=30)，但多了一道 Gate
    """
    host = assert_host_allowed(url)
    if session is not None:
        fn = getattr(session, method.lower())
    else:
        import requests
        fn = getattr(requests, method.lower())
    try:
        resp = fn(url, **kwargs)
    finally:
        record_outbound(url)      # 无论成败都记（便于发现被拦截的调用）
    return resp


# ── 实际出站域名台账 ─────────────────────────────────────

_outbound = {}
_outbound_lock = threading.Lock()


def record_outbound(url: str) -> None:
    """记录一次实际出站（内存计数 + 审计留痕）"""
    try:
        host = host_of(url)
    except ComplianceError:
        host = "(unparsable)"
    with _outbound_lock:
        _outbound[host] = _outbound.get(host, 0) + 1
    try:
        audit_log("outbound", "%s|%s" % (host, "allowed" if is_host_allowed(host) else "BLOCKED"))
    except Exception:
        pass


def outbound_summary() -> dict:
    """本次运行实际出站过的域名清单与次数（内存）"""
    with _outbound_lock:
        domains = dict(_outbound)
    violations = [h for h in domains if not is_host_allowed(h)]
    return {
        "domains": domains,
        "all_allowed": not violations,
        "violations": violations,
        "allowed_hosts": list(ALLOWED_HOSTS),
    }


def reset_outbound() -> None:
    """清空出站台账（测试用）"""
    with _outbound_lock:
        _outbound.clear()


# ── 人脸脱敏 ────────────────────────────────────────────
#
# 2026-10-17 排查：默认环境的 opencv-python 是 5.0.0，**Haar 级联 API 已被移除**
# （cv2.CascadeClassifier 不存在，cv2.objdetect 也不存在），且
# cv2.data.haarcascades 目录里只剩 __init__.py、没有任何 cascade XML。
# 结果是原实现直接抛 AttributeError，被外层 except 吞掉 → 返回 (原图, 0)，
# 即**静默地跳过脱敏，却在调用方看来是"处理成功、无人脸"**。
# 这是合规控件最坏的失败方式：失败开放（fail-open）。
#
# 现在的策略（三道，逐级降级，且失败必须可见）：
#   1) Haar 级联（cv2 < 5 可用）
#   2) YuNet DNN（cv2 >= 5 可用；需提供 onnx 模型，路径由
#      EQUIS_YUNET_MODEL 指定，或在 weights/ 下自动发现）
#   3) 两者都不可用 → 返回 status="unavailable"，写入审计，
#      并在合规报告里暴露，绝不谎报"已脱敏"
# 返回三元组 (path, n_faces, status)，status ∈ {"ok","none","unavailable","error"}。

_YUNET_CANDIDATES = (
    "face_detection_yunet_2023mar.onnx",
    "yunet.onnx",
    "face_detection_yunet.onnx",
)


def _find_yunet_model():
    """查找 YuNet onnx 模型（环境变量优先，其次 weights/ 目录）"""
    p = os.getenv("EQUIS_YUNET_MODEL", "").strip()
    if p and os.path.exists(p):
        return p
    try:
        from config import BASE_DIR
        roots = [os.path.join(BASE_DIR, "weights"),
                 os.path.join(BASE_DIR, "omni_ad", "weights")]
    except Exception:
        roots = []
    for r in roots:
        for name in _YUNET_CANDIDATES:
            fp = os.path.join(r, name)
            if os.path.exists(fp):
                return fp
    return None


def redact_faces(image_path: str, min_size: int = 40):
    """检测图片中的人脸并高斯模糊打码（脱敏）。

    返回 (path, n_faces, status)：
        ("原路径", 0, "none")        未检出人脸（检测器可用）
        ("*_redacted 路径", n, "ok") 检出并已打码
        ("原路径", 0, "unavailable") **检测器不可用**（cv2 版本/模型缺失）
        ("原路径", 0, "error")       处理异常
    调用方必须把 "unavailable" 当作"未脱敏"对待，不要当成"无人脸"。
    """
    try:
        import cv2
    except Exception as e:
        _audit_redact(image_path, "unavailable", "cv2 导入失败: %s" % type(e).__name__)
        return image_path, 0, "unavailable"

    try:
        img = cv2.imread(image_path)
        if img is None:
            return image_path, 0, "error"

        faces = []

        # ① Haar 级联（cv2 < 5）
        if hasattr(cv2, "CascadeClassifier"):
            try:
                xml = os.path.join(getattr(cv2, "data", None).haarcascades
                                   if hasattr(cv2, "data") else "",
                                   "haarcascade_frontalface_default.xml")
                if os.path.exists(xml):
                    cascade = cv2.CascadeClassifier(xml)
                    if not cascade.empty():
                        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                        found = cascade.detectMultiScale(
                            gray, scaleFactor=1.1, minNeighbors=5,
                            minSize=(min_size, min_size))
                        faces = [(int(x), int(y), int(w), int(h)) for x, y, w, h in found]
            except Exception as e:
                print(f"[Redact] Haar 检测失败: {type(e).__name__}: {str(e)[:80]}")

        # ② YuNet DNN（cv2 >= 5，需要 onnx 模型）
        if not faces and hasattr(cv2, "FaceDetectorYN_create"):
            model = _find_yunet_model()
            if model:
                try:
                    h, w = img.shape[:2]
                    det = cv2.FaceDetectorYN_create(model, "", (w, h), 0.8, 0.3, 5000)
                    _n, dets = det.detect(img)
                    if dets is not None:
                        for d in dets:
                            x, y, bw, bh = int(d[0]), int(d[1]), int(d[2]), int(d[3])
                            if bw >= min_size and bh >= min_size:
                                faces.append((x, y, bw, bh))
                except Exception as e:
                    print(f"[Redact] YuNet 检测失败: {type(e).__name__}: {str(e)[:80]}")

        # ③ 没有可用检测器：明确报 unavailable，绝不谎报"无人脸"
        if not faces and not (hasattr(cv2, "CascadeClassifier")
                              or _find_yunet_model()):
            reason = ("cv2 %s 无 CascadeClassifier（Haar 已在 OpenCV 5 移除）且未提供 YuNet onnx 模型"
                      % getattr(cv2, "__version__", "?"))
            _audit_redact(image_path, "unavailable", reason)
            print("[Redact] 人脸检测器不可用：%s" % reason)
            return image_path, 0, "unavailable"

        if not faces:
            return image_path, 0, "none"

        for (x, y, w, h) in faces:
            x = max(0, x); y = max(0, y)
            roi = img[y:y + h, x:x + w]
            if roi.size:
                img[y:y + h, x:x + w] = cv2.GaussianBlur(roi, (51, 51), 30)

        base, ext = os.path.splitext(image_path)
        out = f"{base}_redacted{ext}"
        cv2.imwrite(out, img)
        _audit_redact(image_path, "ok", "faces=%d" % len(faces))
        return out, len(faces), "ok"
    except Exception as e:
        _audit_redact(image_path, "error", "%s: %s" % (type(e).__name__, str(e)[:80]))
        return image_path, 0, "error"


def _audit_redact(image_path: str, status: str, detail: str) -> None:
    """脱敏留痕（P5-44 的 redact 事件此前从未触发过，这里补上真实写入）"""
    try:
        audit_log("redact", "%s|%s|%s" % (status, os.path.basename(str(image_path))[:60], detail[:160]))
    except Exception:
        pass


def face_redaction_status() -> dict:
    """人脸脱敏能力状态（供合规报告自证，避免"宣称已脱敏但实际没做"）"""
    info = {"available": False, "backend": None, "reason": ""}
    try:
        import cv2
    except Exception as e:
        info["reason"] = "cv2 不可用: %s" % type(e).__name__
        return info
    info["cv2_version"] = getattr(cv2, "__version__", "?")
    if hasattr(cv2, "CascadeClassifier"):
        xml = os.path.join(getattr(cv2, "data", None).haarcascades
                           if hasattr(cv2, "data") else "",
                           "haarcascade_frontalface_default.xml")
        if os.path.exists(xml):
            info.update(available=True, backend="haar")
            return info
        info["reason"] = "cv2 有 CascadeClassifier 但缺少 haarcascade XML 文件"
    if hasattr(cv2, "FaceDetectorYN_create") and _find_yunet_model():
        info.update(available=True, backend="yunet")
        return info
    if not info["reason"]:
        info["reason"] = ("cv2 %s 无 Haar 级联（OpenCV 5 已移除），且未提供 YuNet onnx 模型；"
                          "建议安装 opencv-python<5 或设置 EQUIS_YUNET_MODEL"
                          % info["cv2_version"])
    return info


# ── 审计日志 ────────────────────────────────────────────

def _conn():
    """获取当前线程的审计库连接（线程本地复用 + WAL）

    维护（2026-10-02）：原实现每次 audit_log() 都 connect→建表→commit→close()。
    实测瓶颈不在建连接（0.77 ms）而在 **commit 的落盘**：
    200 次「INSERT+commit」= 2852 ms，而合并成单事务只要 13 ms。
    审计写入发生在每轮对话的多个节点上（chat / upload / redact /
    tool_call / outbound / kb_hit / verdict / memory_write），是热路径。

    现在：线程本地连接复用 + WAL 模式（读不阻塞写），
    写语义不变（每次调用仍 commit），崩溃最多丢最后一次未提交的记录。
    """
    conn = getattr(_tls, "conn", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.ProgrammingError:
            try:
                conn.close()
            except Exception:
                pass
            _tls.conn = None
    os.makedirs(os.path.dirname(AUDIT_DB), exist_ok=True)
    c = sqlite3.connect(AUDIT_DB, check_same_thread=False)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=5000")
    except sqlite3.Error:
        pass
    c.execute("""CREATE TABLE IF NOT EXISTS audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL, event TEXT, detail TEXT)""")
    c.commit()
    _tls.conn = c
    return c


def audit_log(event: str, detail: str = "") -> None:
    """追加一条审计记录（提问 / 上传 / 脱敏 / 工具调用 / 判定 均留痕）"""
    with _lock:
        try:
            c = _conn()
            c.execute("INSERT INTO audit (ts, event, detail) VALUES (?,?,?)",
                      (time.time(), event, str(detail)[:400]))
            c.commit()
        except Exception as e:
            print(f"[Audit] 写入失败: {e}")


# ── 事件类型常量（P5-44）────────────────────────────────
# 审计事件类型集中登记，避免各处手写字符串导致口径不一致、
# 也让 /api/compliance 能自证"留痕覆盖了哪些环节"。
EVENT_TYPES = (
    "chat",          # 用户提问
    "upload",        # 图片上传
    "redact",        # 人脸脱敏
    "router",        # 意图路由决策
    "tool_call",     # 工具/子智能体调用
    "kb_hit",        # 知识库检索命中
    "web_search",    # 联网检索
    "outbound",      # 实际出站域名（P4-42）
    "verdict",       # 合规判定结论
    "memory_write",  # 长期记忆沉淀
    "error",         # 异常
)


def audit_tool_call(tool: str, ok: bool = True, extra: str = "") -> None:
    """记录一次工具/子智能体调用（P5-44）

    原先 audit_log 只在 3 处被调用（server.py 的 chat/upload、nodes.py 的
    redact），工具调用与判定完全没留痕，与"全链路可追溯"的表述不符。
    """
    audit_log("tool_call", f"{tool}|{'ok' if ok else 'fail'}|{extra}"[:400])


def audit_kb_hit(query: str, sources, level: str = "") -> None:
    """记录一次知识库命中的检索依据（P5-44）——这是"引用可追溯"的证据链"""
    srcs = "；".join(str(s) for s in (sources or []))[:300]
    audit_log("kb_hit", f"查询={str(query)[:80]}｜命中={srcs}｜级别={level}"[:400])


def audit_verdict(verdict: str, detail: str = "") -> None:
    """记录一次合规判定结论（P5-44）"""
    audit_log("verdict", f"{verdict}｜{detail}"[:400])


def audit_stats() -> dict:
    """按事件类型统计留痕覆盖度（P5-44，供 /api/compliance 自证）"""
    with _lock:
        c = _conn()
        rows = c.execute(
            "SELECT event, COUNT(*) FROM audit GROUP BY event ORDER BY 2 DESC").fetchall()
        total = c.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    counts = {r[0]: r[1] for r in rows}
    return {
        "total": total,
        "by_event": counts,
        "known_types": list(EVENT_TYPES),
        "missing_types": [t for t in EVENT_TYPES if t not in counts],
    }


def audit_recent(n: int = 20) -> list:
    with _lock:
        c = _conn()
        rows = c.execute("SELECT ts, event, detail FROM audit ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return [{"ts": r[0], "event": r[1], "detail": r[2]} for r in rows]


# ── 综合自检报告 ────────────────────────────────────────

def compliance_report() -> dict:
    """生成数据合规与伦理风险防控的自检报告（可直接用于项目报告与 /api/compliance）"""
    residency = check_data_residency()
    _astats = audit_stats()
    # 2026-10-17：报告必须反映**真实能力**，不能写死 enabled=True。
    # 默认环境 opencv-python 5.0.0 已移除 Haar 级联 API，实际检测能力为不可用。
    _fr = face_redaction_status()
    return {
        "data_residency": residency,
        "face_redaction": {
            "enabled": bool(_fr.get("available")),
            "available": bool(_fr.get("available")),
            "backend": _fr.get("backend"),
            "cv2_version": _fr.get("cv2_version"),
            "reason": _fr.get("reason", ""),
            "method": ("OpenCV Haar cascade + 高斯模糊" if _fr.get("backend") == "haar"
                       else "OpenCV YuNet DNN + 高斯模糊" if _fr.get("backend") == "yunet"
                       else "（当前不可用，见 reason）"),
            "trigger": "上传图片进入视觉分析前自动执行",
            "degraded_mode": (not _fr.get("available")),
            "degraded_note": ("" if _fr.get("available") else
                              "检测器不可用时不会静默跳过：审计写入 redact|unavailable，"
                              "本字段 available=false 如实暴露；"
                              "修复方式见 reason（安装 opencv-python<5 或提供 YuNet onnx）"),
        },
        "audit": {
            "enabled": True,
            "db": AUDIT_DB,
            "recent_count": len(audit_recent(1000)),
            # P5-44：留痕覆盖度自证 —— 原先只报"有多少条"，
            # 现在报"覆盖了哪些环节、还缺哪些环节"，可直接用于评审追问。
            "coverage": _astats,
        },
        "privacy": {
            "image_storage": "本机 agent/data/uploads（不出境、不向第三方共享）",
            "sensitive_data": "人脸自动脱敏；语音仅用于本地语音识别通道",
        },
    }
