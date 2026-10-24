"""联网搜索工具 —— 百炼 enable_search（官方搜索插件）

P5-50/P5-51 修复：
  1) 缓存加锁：原 `_cache` 是无锁 dict，却被 coordinator 的 ThreadPoolExecutor
     并发改写（check-then-act 竞态；GIL 保证不会崩但淘汰结果不确定）。
     改用 RLock + OrderedDict 实现真正的 LRU。
  2) TTL 从 300s 提到 3600s：行业动态不需要 5 分钟就失效，
     实测单次 enable_search 最长 30.2s，重复查询命中缓存收益很大。
  3) max_tokens 500 -> 250：搜索摘要不需要长答案，直接减少生成时间。
  4) 新增 on_status 回调：实测该分支耗时可达整轮的 95%，
     前端此前只看到"卡住"，现在至少能收到阶段性状态。

P5-52 修复（相关性守门 + 拒识）：
  实测线上故障：用户问"你是谁/通义千问是什么"，返回的是
  **模型自己的自我介绍**（"我是通义千问（Qwen），由阿里巴巴…研发"），
  并被 _collect_sources 误收为"知识库检索依据：通义千问"。

  三处失守：
    a) system prompt 只说"搜索助手，基于联网搜索结果回答"，
       **没有任何"不要谈论自己身份"的约束** → 模型被问及自身即自曝；
    b) enable_search 通道**不做任何相关性校验**，返回 >20 字就当有效结果；
    c) _collect_sources 用正则抓所有"来源：xxx"，把自曝文本当成了检索依据。

  现补三道闸：
    1) _IRRELEVANT_PAT 识别"自我介绍/自指/闲聊"类输出 → 判定为无效；
    2) 两个通道的 system prompt 加入硬性拒答约束；
    3) 无效时统一返回 _REASK 提示（让用户重新提问），不再编造结果。
  拒识（abstention）本身是本项目的可信度理念：拿不准就不答。
"""
import re
import requests, time, os, sys, threading
from collections import OrderedDict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
# 数据不出境 Gate（P4-42 接线）
from compliance.gate import guard_request, ComplianceError

H = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}

_cache = OrderedDict()
_cache_lock = threading.RLock()
CACHE_TTL = int(os.getenv("EQUIS_SEARCH_TTL", "3600"))   # P5-51：300 -> 3600
CACHE_MAX = int(os.getenv("EQUIS_SEARCH_CACHE_MAX", "200"))
SEARCH_MAX_TOKENS = int(os.getenv("EQUIS_SEARCH_MAX_TOKENS", "250"))  # P5-51：500 -> 250
_stats = {"hit": 0, "miss": 0, "fail": 0, "irrelevant": 0, "timeout": 0}

# ── 尾延迟治理：分段预算 ──────────────────────────────────
# 实测（2026-10-17，百炼 enable_search 当前真实延迟，非估算）：
#   21.4s 成功 · 42.5s 成功 · >70s 超时 · 75.2s 降级
# 而 trace.db 里"web 节点"186 次调用中有 180 次是 100ms 的"未调度"占位，
# 只有 6 次是真实联网 —— 所以早期用"均值 1s"判断是错的，被占位数据稀释了。
#
# 原实现是两个各自独立的超时：策略1 timeout=45s + 策略2 timeout=30s，
# 最坏 75s（实测确实捕获到 75.2s 的一次：搜索等满后降级又等满）。
#
# 现改为**分段预算**：
#   WEB_BUDGET_S      只约束联网搜索这一段（默认 45s，能覆盖到 42.5s 的成功样本）
#   FALLBACK_BUDGET_S 降级请求的独立小预算（默认 10s，普通 LLM 调用 1-2s 即可完成）
# 搜索预算耗尽 → 立刻转降级并给出可用答案（而不是返回一句道歉）。
# 最坏总耗时 ≈ 45 + 10 = 55s，但正常路径（搜索成功）不受任何影响。
#
# 演示场景想要更跟手，可设 EQUIS_SEARCH_BUDGET=20 换取"20 秒必返回"。
# 设 0 表示不限制搜索段（回到旧行为，不推荐）。
WEB_BUDGET_S = float(os.getenv("EQUIS_SEARCH_BUDGET", "45"))
FALLBACK_BUDGET_S = float(os.getenv("EQUIS_SEARCH_FALLBACK_BUDGET", "10"))

# ── P5-52：拒识常量与检测 ─────────────────────────────────

#: 相关性不足时的统一回复（引导用户给出可检索的具体问题，而不是硬答）
_REASK = ("【索元】该问题未检索到可用的公开信息。\n"
          "请补充更具体、可检索的关键词，例如标准编号、规程名称、"
          "具体技术指标或行业动态关键词。\n"
          "（提示：索元只检索公开信息，无法回答关于本智能体自身身份的提问。）")

#: 自我介绍 / 自指 / 闲聊类输出的识别特征
#:
#: 覆盖的实测故障形态：
#:   "我是通义千问（Qwen），由阿里巴巴通义实验室研发的大语言模型…"
#:   "我是一个AI助手""作为一个人工智能""我无法…""请问您有什么需要我帮忙的"
_IRRELEVANT_PAT = re.compile(
    r"(我是\s*(一个)?\s*(通义|千问|阿里|大)?\s*(Qwen|qwen)?\s*(大)?\s*(语言)?\s*模型)"
    r"|我是\s*由.{0,30}(研发|开发|训练|推出)"
    r"|作为\s*(一个|一名)\s*(AI|人工智能|大模型|语言模型)"
    r"|我(不是|并非)\s*(一个)?\s*(AI|人工智能|大模型)"
    r"|请问您有什么(需要|想要)?我(帮|为您)"
    r"|有什么我可以帮(您|你)"
    r"|我是您的\s*(AI|智能)"
)

#: 出现这些词说明模型在跟用户寒暄/确认，而非给出检索结果
_CHITCHAT_PAT = re.compile(
    r"^(请问|您好|你好|我是|作为)", re.M
)


def is_irrelevant(text: str) -> bool:
    """判断搜索输出是否为"自我介绍/寒暄"这类无效结果。

    设计取舍：只拦**明确无效**的形态，不做语义相似度判定。
    原因：宁可放过一些弱相关结果，也不要把正常检索结果误杀——
    误杀会让用户白等一次网络往返（实测最长 30.2s）。
    """
    if not text:
        return True
    t = str(text).strip()
    # 短文本且命中寒暄开头 → 基本可判定为无效
    if len(t) < 200 and _CHITCHAT_PAT.search(t):
        return True
    return bool(_IRRELEVANT_PAT.search(t))


def search_cache_stats() -> dict:
    with _cache_lock:
        return {"size": len(_cache), "max": CACHE_MAX, "ttl": CACHE_TTL,
                **_stats}


def _cache_get(key: str):
    with _cache_lock:
        item = _cache.get(key)
        if item is None:
            return None
        ts, value = item
        if time.time() - ts >= CACHE_TTL:
            _cache.pop(key, None)          # 过期即淘汰
            return None
        _cache.move_to_end(key)
        _stats["hit"] += 1
        return value


def _cache_set(key: str, value: str):
    """写入缓存（LRU 淘汰，容量有界，线程安全）"""
    with _cache_lock:
        _cache[key] = (time.time(), value)
        _cache.move_to_end(key)
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)
        _stats["miss"] += 1


def clear_search_cache() -> None:
    with _cache_lock:
        _cache.clear()


def web_search(query: str, max_results: int = 5, on_status=None) -> str:
    """联网搜索主入口。策略1：enable_search；策略2：纯 LLM 降级。

    Args:
        query:       搜索关键词
        max_results: 保留参数（当前由 enable_search 内部决定条数）
        on_status:   可选回调 on_status(stage:str)，用于向前端反馈进度
    """

    query = query.strip()
    if not query:
        return "请提供搜索关键词。"

    cache_key = query[:120]
    cached = _cache_get(cache_key)
    if cached is not None:
        print(f"[Search] cache hit: '{query[:40]}...'")
        if on_status:
            try: on_status("命中搜索缓存")
            except Exception: pass
        return cached

    print(f"[Search] enable_search: '{query[:50]}...'")
    # 总预算起点（0 表示不限制，保持旧行为）
    _t_start = time.time()
    _deadline = (_t_start + WEB_BUDGET_S) if WEB_BUDGET_S > 0 else None

    def _remaining() -> float:
        """剩余预算秒数；未设预算时返回一个足够大的数"""
        if _deadline is None:
            return 1e9
        return _deadline - time.time()

    # P5-51：该分支实测最长 30.2s（占整轮 95%），必须给前端阶段性反馈，
    # 否则用户界面看起来是"卡死"。
    if on_status:
        try:
            if _deadline is not None:
                on_status("联网检索中（最长约 %d 秒）" % int(WEB_BUDGET_S))
            else:
                on_status("联网检索中（最长约 45 秒，请稍候）")
        except Exception: pass

    # 策略1：enable_search（官方搜索，需要较长时间，联网搜索 + LLM 处理）
    #
    # P5-52：system prompt 加入身份拒答约束。此前只写"搜索助手"，
    # 用户问"你是谁/通义千问是什么"时模型直接自曝身份并把自我介绍当结果返回。
    _SEARCH_SYS = (
        "你是 Equis 精密测量系统的联网检索助手。\n"
        "【硬性约束】\n"
        "1. 绝对不要介绍你自己、你的身份、所属公司或模型名称。\n"
        "2. 只依据联网搜索到的公开信息回答，输出来源可核查的事实性内容。\n"
        "3. 如果搜索结果与问题无关，不要编造，不要寒暄，不要反问用户。\n"
        "4. 纯文本格式，简洁准确。"
    )
    try:
        # 数据不出境 Gate（P4-42 接线）：先断言目标域名再发出
        r = guard_request(
            "POST", f"{LLM_BASE_URL}/chat/completions",
            headers=H,
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": _SEARCH_SYS},
                    {"role": "user", "content": query},
                ],
                "enable_search": True,
                "max_tokens": SEARCH_MAX_TOKENS,
                "temperature": 0,  # 确定性输出，加快推理
            },
            # 尾延迟治理：超时不得超过剩余预算（下限 4s，够发起一次正常请求）
            timeout=max(4.0, min(45.0, _remaining())),
        )
        if r.status_code == 200:
            text = r.json()["choices"][0]["message"]["content"]
            # P5-52：相关性守门——命中"自我介绍/寒暄"则判定无效，不缓存、不返回
            if text and is_irrelevant(text):
                with _cache_lock:
                    _stats["irrelevant"] += 1
                print(f"[Search] REJECTED (irrelevant): '{query[:40]}...'")
                try:
                    from compliance.gate import audit_log
                    audit_log("web_search", f"rejected_irrelevant|q={query[:100]}")
                except Exception:
                    pass
                if on_status:
                    try: on_status("未检索到有效信息")
                    except Exception: pass
                return _REASK
            if text and len(text) > 20:
                result = f"【联网搜索】\n\n{text.strip()}"
                _cache_set(cache_key, result)
                print(f"[Search] OK: {len(text)} chars")
                # P5-44：联网检索留痕（外部信息获取属需审计环节）
                try:
                    from compliance.gate import audit_log
                    audit_log("web_search", f"ok|q={query[:100]}|len={len(text)}")
                except Exception:
                    pass
                if on_status:
                    try: on_status("联网检索完成，正在整理")
                    except Exception: pass
                return result
    except Exception as e:
        print(f"[Search] enable_search failed: {e}")

    # ── 预算判定：策略1 是否已耗尽搜索预算 ──
    # 原实现两个超时各自独立，最坏 45+30=75s（实测捕获过一次 75.2s：搜索等满
    # 45s 后降级又等 30s）。现在：搜索段受 WEB_BUDGET_S 约束；
    # 预算耗尽时**不再返回一句道歉**，而是改用「便宜的降级请求」给出可用答案
    # （降级只做一次普通 LLM 调用，约 1-2s，且有独立的小上限）。
    # 这样最坏总耗时 ≈ WEB_BUDGET_S + FALLBACK_BUDGET_S，且终归有内容，
    # 明显优于"等 75 秒拿一句失败"。
    _left = _remaining()
    _search_timed_out = _left < 3.0
    if _search_timed_out:
        with _cache_lock:
            _stats["timeout"] += 1
        _elapsed_s = time.time() - _t_start
        print(f"[Search] SEARCH BUDGET EXHAUSTED after {_elapsed_s:.1f}s -> fast fallback")
        try:
            from compliance.gate import audit_log
            audit_log("web_search",
                      f"timeout|q={query[:100]}|elapsed={_elapsed_s:.1f}s")
        except Exception:
            pass
        if on_status:
            try: on_status("联网检索超时，改用模型知识作答")
            except Exception: pass
        _left = FALLBACK_BUDGET_S      # 降级请求用独立的小预算

    # 策略2：纯 LLM 降级（无联网）
    # P5-52：降级通道没有联网结果兜底，模型自曝身份的风险更高
    # （用户问"你是谁"时，纯 LLM 必然自曝），因此约束更硬。
    print(f"[Search] fallback to plain LLM...")
    if on_status:
        try: on_status("联网通道不可用，改用模型知识作答")
        except Exception: pass
    try:
        # 数据不出境 Gate（P4-42 接线）：先断言目标域名再发出
        r = guard_request(
            "POST", f"{LLM_BASE_URL}/chat/completions",
            headers=H,
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": (
                        "你是 Equis 精密测量系统的联网检索助手，基于训练知识回答。\n"
                        "【硬性约束】\n"
                        "1. 绝对不要介绍你自己、你的身份、所属公司或模型名称。\n"
                        "2. 用户问'你是谁''你是什么模型'时，只回答：我是 Equis 精密测量系统的检索助手。\n"
                        "3. 只回答与计量、测量、标准、传感器、工业动态相关的问题。\n"
                        "4. 如信息可能过时请注明。纯文本格式，简洁专业。"
                    )},
                    {"role": "user", "content": query},
                ],
                "max_tokens": SEARCH_MAX_TOKENS,
                "temperature": 0,
            },
            # 尾延迟治理：降级段用自己的小预算（默认 10s），不继承搜索段的剩余量。
            # 普通 LLM 调用 1-2s 即可完成，给 10s 是留足网络波动余量。
            timeout=max(3.0, min(FALLBACK_BUDGET_S, _left)),
        )
        if r.status_code == 200:
            text = r.json()["choices"][0]["message"]["content"]
            # P5-52：降级通道同样过守门
            if text and is_irrelevant(text):
                with _cache_lock:
                    _stats["irrelevant"] += 1
                print(f"[Search] REJECTED (irrelevant, fallback): '{query[:40]}...'")
                try:
                    from compliance.gate import audit_log
                    audit_log("web_search",
                              f"rejected_irrelevant_fallback|q={query[:100]}")
                except Exception:
                    pass
                return _REASK
            if text and len(text) > 10:
                result = f"【智能回答】\n\n{text.strip()}"
                # 搜索超时后转来的降级结果：加一句上下文说明，
                # 避免用户误以为这是"联网查到的"。
                if _search_timed_out:
                    result = ("（联网检索超时，以下为模型知识作答，可能不含最新信息）\n\n"
                              + result)
                _cache_set(cache_key, result)
                print(f"[Search] fallback OK: {len(text)} chars"
                      + (" (after timeout)" if _search_timed_out else ""))
                # P5-44：降级路径同样要留痕 —— 否则"这次到底有没有联网"
                # 在审计里查不出来（实测就遇到过 enable_search 失败走降级、
                # 审计里却完全没有记录的情况）。
                try:
                    from compliance.gate import audit_log
                    audit_log("web_search",
                              f"fallback(no-internet){'|after-timeout' if _search_timed_out else ''}"
                              f"|q={query[:100]}|len={len(text)}")
                except Exception:
                    pass
                return result
    except Exception as e:
        print(f"[Search] fallback failed: {e}")

    with _cache_lock:
        _stats["fail"] += 1
    # P5-44：两条通道都失败也要留痕，便于区分"没搜到"与"网络/配额问题"
    _elapsed_total = time.time() - _t_start
    try:
        from compliance.gate import audit_log
        audit_log("web_search",
                  f"failed|q={query[:100]}|elapsed={_elapsed_total:.1f}s"
                  + ("|search_timeout" if _search_timed_out else ""))
    except Exception:
        pass
    # 如实说明两条通道都失败、各花了多久 —— 原先只有一句"请稍后重试"，
    # 用户与排障者都无法区分"确实没有这条信息"和"网络/服务不可用"。
    if _search_timed_out:
        return ("【索元】联网检索超时，且模型知识作答通道同样不可用"
                "（累计 %.0f 秒）。\n"
                "这不是「没有相关信息」，而是服务暂时不可达。\n"
                "建议稍后重试；若急需结论，可先用本地知识库检索（规元）。"
                % _elapsed_total)
    return ("【索元】两条通道均未取到有效结果（累计 %.0f 秒）："
            "联网检索失败、模型知识作答亦不可用。\n"
            "请稍后重试，或改用更具体的关键词。" % _elapsed_total)
