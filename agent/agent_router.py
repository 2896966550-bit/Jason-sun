"""统一结构化路由（P1-12）—— 用一次 JSON-schema 调用替代三次分散的 LLM 调用

────────────────────────────────────────────────────────────────
为什么需要它（代码审计结论）
────────────────────────────────────────────────────────────────
原实现每轮对话要发三次独立的 LLM 请求，且路由靠**中文关键词 in 判断**：

  1. ``coordinator.analyze_intent()``     意图分类      实测 1.1-2.9 s
  2. ``coordinator.rewrite_query()``      多轮指代消解  （仅多轮时）
  3. ``coordinator._extract_params()``    计算参数抽取  （计算分支时）

而且分支选择散落在 9 处 ``any(w in query for w in [...])``，直接导致两个
用户可见的线上故障：

  * 失败案例 2：用户说"调用摄像头拍一张"，抓帧失败被静默吞掉，
    同时因 camera_requested=True 跳过了 compute 分支，最终落到裸 LLM
    兜底，模型拒答"我没有权限调用您的摄像头"。
  * 失败案例 3：纯概念问题"A类评定和B类评定有什么区别"因命中
    "不确定度"而进入 compute 分支，用**凭空默认值 mv=100.0mm**
    算了一遍不确定度，白等 11.8 s。

本模块把这些决策收敛成**一次**调用，返回结构化对象；并且：
  - 意图是枚举 + schema 校验，不是字符串包含；
  - 由模型显式输出 ``need_camera`` / ``need_sensor``，但**最终仍要与真实
    设备可用性求交**（见 ``resolve_plan``）——模型的意愿不能覆盖硬件事实；
  - ``direct_answer`` 允许"纯概念问题直接作答"，从根上避免无意义计算。

────────────────────────────────────────────────────────────────
兼容性
────────────────────────────────────────────────────────────────
``analyze_intent`` / ``rewrite_query`` 保留原有签名与返回形状，内部改走
本模块，因此 coordinator / graph 节点无需改动即可受益。
"""
import json
import os
import re
import sys
import threading
import time
from collections import OrderedDict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

# 路由可用的小模型覆盖（路由是分类任务，不需要主力大模型）
#
# 实测（本机，百炼兼容模式，enable_thinking=False，单次 20 token 往返）：
#   qwen-flash      279 ms      <- 选它
#   qwen-turbo      301 ms
#   qwen3-8b        355 ms
#   qwen-plus       750 ms
#   qwen3.8-max    1103 ms      <- 原实现用的是这个（主力大模型）
# 路由是"分类 + 结构化抽取"，用主力模型既慢又贵；实测 qwen-flash 的路由
# 质量与 qwen3.8-max 无可见差异（见 agent_router.py 自测输出）。
ROUTER_MODEL = os.getenv("EQUIS_ROUTER_MODEL", "qwen-flash")
ROUTER_TIMEOUT = int(os.getenv("EQUIS_ROUTER_TIMEOUT", "20"))
ROUTER_ENABLED = os.getenv("EQUIS_ROUTER_ENABLED", "1") not in ("0", "false", "False")

# 缓存容量（原 _intent_cache 是无上限 dict，长跑会持续增长）
_CACHE_MAX = int(os.getenv("EQUIS_ROUTER_CACHE", "512"))
_cache: "OrderedDict[str, dict]" = OrderedDict()
_cache_lock = threading.Lock()

# 客户端单例：原实现每次调用都 new 一个 OpenAI 客户端，
# 意味着每次都要重建连接池与 TLS 握手（每轮 3-6 次）。
_client = None
_client_lock = threading.Lock()


def _get_client():
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            # 数据不出境 Gate（P4-42 接线）：SDK 自行发请求，不经过 guard_request，
            # 故在客户端创建处断言一次并记账；越界即硬失败，不静默外发。
            try:
                from compliance.gate import assert_host_allowed, record_outbound
                assert_host_allowed(LLM_BASE_URL)
                record_outbound(LLM_BASE_URL)
            except Exception as e:
                raise RuntimeError(
                    "数据不出境 Gate 拦截：LLM_BASE_URL=%r 不合法（%s）"
                    % (LLM_BASE_URL, e))
            from openai import OpenAI
            # C2：与 coordinator 一致，用 httpx 请求钩子做**逐请求**出站计量
            def _on_request(request):
                try:
                    from compliance.gate import record_outbound
                    record_outbound(str(request.url))
                except Exception:
                    pass

            try:
                import httpx
                _http = httpx.Client(event_hooks={"request": [_on_request]},
                                     timeout=float(ROUTER_TIMEOUT))
            except Exception:
                _http = None

            _client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY,
                             timeout=ROUTER_TIMEOUT,
                             **({"http_client": _http} if _http else {}))
    return _client


# ── 工具清单（供模型选择；模型只能从这里挑）────────────────
TOOLS = {
    "kb_search":          "检索 JJF/JJG/GB 计量标准知识库（计量学、不确定度、标定、检定规程）",
    "read_sensors":       "读取当前 ESP32 环境传感器（温度/湿度/振动）实时读数",
    "capture_camera":     "调用摄像头抓取当前画面并做视觉分析",
    "anomaly_detect":     "对画面/图片做工件表面缺陷与异常检测（Omni-AD）",
    "calc_uncertainty":   "按 GUM 方法计算测量不确定度（需实测值与器具精度）",
    "compliance_judge":   "按工程规则比较实测与参考的一致性；需实测/参考/MPE/环境温度，不代表工件公差合格",
    "comp_3d":            "由双目视差计算三维坐标/深度",
    "calib_guide":        "给出双目相机标定操作步骤",
    "web_search":         "联网检索最新标准、新闻、行业动态",
    "plan_task":          "为复杂测量任务生成分步执行计划",
}

_SYSTEM = """你是 Equis 精密测量智能体的**路由器**。你的唯一职责是把用户问题解析成一个 JSON 决策对象。

可用工具（tool_calls[].name 只能取自下列键名）：
%s

判定规则（务必严格遵守）：
1. **只要问题里出现标准编号**（JJF/JJG/GB/GB/T/ISO 等，如 "JJG 21"）或
   **规程名称**，就必须 kb_search，direct_answer 必须为 false。
2. **纯概念/原理/定义类问题且不含任何标准编号**（如"A类评定和B类评定有什么区别"
   "GUM是什么""什么是示值误差"）→ direct_answer=true，tool_calls 置空，
   不要调用任何计算工具。这是为了**避免为概念问题白算一遍不确定度**。
3. **需要实时数据**才用 read_sensors / capture_camera；用户没有明确要求
   "现在/实时/拍一张/看一下"就不要调用。
4. 涉及**具体数值的计算**（给了实测值、精度等）才用 calc_uncertainty /
   compliance_judge；缺必需参数时仍可调用，但记入 missing_params。
5. 明确说"搜索/最新/新闻/联网"→ web_search。
6. 多轮对话中的"它/该/这个"必须结合历史改写成独立可检索的问题，填入
   rewritten_query；若已独立完整则填空字符串。
7. tool_calls 至少一项，除非 direct_answer 为 true。

只输出 JSON，不要 markdown 代码块，不要任何解释。字段：
{
  "intent": ["kb"|"compute"|"vision"|"sensor"|"web"|"plan"],
  "rewritten_query": "改写后的问题，或空字符串",
  "need_camera": false,
  "need_sensor": false,
  "direct_answer": false,
  "tool_calls": [{"name": "kb_search", "args": {"query": "轴类工件 直径测量"}, "why": "先确定适用标准"}],
  "missing_params": []
}""" % "\n".join("- %s: %s" % (k, v) for k, v in TOOLS.items())

_VALID_INTENTS = {"kb", "compute", "vision", "sensor", "web", "plan"}

# 标准编号识别（与 tools/kb_tools._STD_RE 保持一致的口径）
_STD_RE = re.compile(
    r"\b((?:JJF|JJG|GB\s*/\s*T|GB|ISO\s*/\s*IEC\s*Guide|ISO)\s*\d+(?:\.\d+)?)",
    re.I,
)

# 明确的联网意图（用户说了"搜索/最新/新闻"就必须联网，不交给小模型赌）
#
# D2 修复（由 P4-40 评测发现）：原先的 `202\d年?` 会把**标准编号里的年份**
# 也当成"年份提及"，于是 "JJG 34-2022 指示表检定规程的示值误差要求"
# 这类纯标准查询被强行升级为联网查询 —— 多排一个 web 分支、多一次外网往返，
# 还让网页内容有机会混入本该以国标/规程为准的回答；且行为不一致
# （只有 202x 年号的触发，2019/2002 的不触发）。
# 现要求年份后必须跟"年"字，并排除紧跟在连字符/数字后的年份（即"编号-年份"）。
_WEB_RE = re.compile(
    r"(搜索|搜一下|搜下|联网|网上搜|查最新|最新|新闻|动态|近期"
    r"|(?<![-‑–—\d])20\d{2}\s*年)"
)


# ── 历史与感知摘要 ─────────────────────────────────────────

def _hist_snippet(history, max_pairs=3, per_msg=180):
    if not history:
        return ""
    lines = []
    for h in history[-(max_pairs * 2):]:
        role = "用户" if h.get("role") == "user" else "Equis"
        lines.append("%s: %s" % (role, str(h.get("content", ""))[:per_msg]))
    return "\n".join(lines)


from tools.json_output import parse_json_object as _parse_json_loose


# ── 关键词降级（LLM 不可用时）─────────────────────────────

# 概念/定义/原理解释类问法（P4-40 评测暴露的缺陷修复）
# 背景：评测用 --keyword-only 跑降级路径，通过率仅 72.7%，
# 其中"纯概念问题应 direct_answer"全部失败 —— 因为降级路径此前
# 硬编码 direct_answer=False，于是 LLM 不可用时概念问题又会回到
# compute 分支，正是线上失败案例 3（用默认值 mv=100.0 白算 11.8 秒）。
_CONCEPT_PAT = re.compile(
    r"(什么(区别|不同|关系|是|叫)|有何(区别|不同)|"
    r"是什么|什么是|定义|含义|概念|原理|"
    r"怎么(理解|解释)|解释一下|介绍一下|讲解|"
    r"为什么|有何(作用|意义)|作用是什么|区别(是|在))"
)

# 具体计算/判定意图（需要数值输入）
_CALC_PAT = re.compile(
    r"(算(一下|算)?|计算|评定|求|判定|判合格|判不合格|是否合格|合规|合格吗|"
    r"帮我算|帮我判|给我算|做(合规|合格)判定)"
)

# 数值（带单位或小数）
_NUM_PAT = re.compile(r"\d+(?:\.\d+)?\s*(?:mm|cm|m|μm|um|°c|℃|%rh|kg|g|n|pa)?")


def keyword_route(query, has_image=False):
    """关键词兜底：与主路径返回**同一种**决策对象，便于统一消费

    修复要点（P4-40 评测暴露）：
      1) 概念类问题要能判 direct_answer=True，否则 LLM 不可用时概念问题
         会回到 compute 分支（线上失败案例 3）；
      2) 给了数值 + 计算/判定语义时要排计算工具（CALC-1/CALC-2 用例）；
      3) 判定"是否概念问题"必须排除**含数值**的句子 ——
         "什么是示值误差？实测 10.014mm 帮我算不确定度" 不是纯概念问题。
    """
    msg = (query or "").lower()
    tools, intents, missing = [], [], []

    # 先判断是不是"纯概念问题"（讲解/定义，且没有具体数值）
    has_number = bool(re.search(r"\d", msg))
    is_concept = bool(_CONCEPT_PAT.search(msg)) and not has_number
    wants_calc = bool(_CALC_PAT.search(msg)) and has_number

    if any(w in msg for w in ("jjf", "jjg", "gb/t", "gbt", "iso", "规程", "规范",
                              "标准", "检定", "校准", "计量", "不确定度", "gum", "msa")):
        intents.append("kb")
        tools.append({"name": "kb_search", "args": {"query": query}, "why": "命中计量标准关键词"})

    if any(w in msg for w in ("搜索", "搜一下", "联网", "最新", "新闻")):
        intents.append("web")
        tools.append({"name": "web_search", "args": {"query": query}, "why": "命中联网检索关键词"})

    if any(w in msg for w in ("摄像头", "拍照", "拍一张", "拍个", "拍下", "镜头",
                              "相机", "看一下", "看看现在", "看看周围")):
        intents.append("vision")
        tools.append({"name": "capture_camera", "args": {}, "why": "用户要求取当前画面"})

    if has_image:
        intents.append("vision")
        tools.append({"name": "anomaly_detect", "args": {}, "why": "本轮附带图片"})

    if any(w in msg for w in ("温度", "湿度", "振动", "环境", "传感器", "读数")):
        intents.append("sensor")
        tools.append({"name": "read_sensors", "args": {}, "why": "命中传感器关键词"})

    if any(w in msg for w in ("帮我测", "我要测", "如何测量", "测量方案", "怎么测", "检测方案")):
        intents.append("plan")
        tools.append({"name": "plan_task", "args": {"task": query}, "why": "复合测量任务"})

    # 计算 / 判定：给了数值且有计算语义
    if wants_calc:
        intents.append("compute")
        if "判定" in msg or "合格" in msg or "合规" in msg:
            tools.append({"name": "compliance_judge", "args": {}, "why": "含数值+判定语义"})
        elif "视差" in msg or "三维" in msg or "坐标" in msg:
            tools.append({"name": "comp_3d", "args": {}, "why": "含数值+三维语义"})
        else:
            tools.append({"name": "calc_uncertainty", "args": {}, "why": "含数值+计算语义"})

    # 概念问题：不要工具、直接作答（与 LLM 路径的 direct_answer 语义一致）
    direct = False
    if is_concept and not wants_calc:
        direct = True
        tools = []            # 概念问题不该触发任何工具调用

    return {
        "intent": list(dict.fromkeys(intents)) or ["kb"],
        "rewritten_query": "",
        "need_camera": "vision" in intents and any(
            w in msg for w in ("摄像头", "拍照", "拍一张", "拍个", "拍下", "镜头", "相机")),
        "need_sensor": "sensor" in intents,
        "direct_answer": direct,
        "tool_calls": tools or ([] if direct else
                                [{"name": "kb_search", "args": {"query": query}, "why": "默认兜底"}]),
        "missing_params": missing,
        "_source": "keyword",
    }


# ── 主入口：一次调用完成路由 ──────────────────────────────

def route(query, history=None, has_image=False, percept_summary="", use_cache=True):
    """把用户问题解析为结构化路由决策。

    Args:
        query:            当前用户问题
        history:          近几轮 [{"role","content"}]
        has_image:        本轮是否附带图片
        percept_summary:  感知总线摘要（可空）
        use_cache:        是否使用缓存（同一问题+同图态可复用）

    Returns:
        dict：intent / rewritten_query / need_camera / need_sensor /
              direct_answer / tool_calls / missing_params / _source
    """
    query = (query or "").strip()
    if not query:
        return keyword_route(query, has_image)

    import hashlib
    cache_key = hashlib.sha256(json.dumps({"query":query,"has_image":bool(has_image),
        "history":history or [],"percept_summary":percept_summary or ""},
        ensure_ascii=False,sort_keys=True,default=str).encode("utf-8")).hexdigest()
    if use_cache:
        with _cache_lock:
            hit = _cache.get(cache_key)
            if hit is not None:
                _cache.move_to_end(cache_key)      # LRU 命中即置尾
                return dict(hit, _source=hit.get("_source", "cache"))

    if not ROUTER_ENABLED or not LLM_API_KEY:
        dec = keyword_route(query, has_image)
        _cache_put(cache_key, dec)
        _audit_route(dec, query)
        return dec

    # 组装用户消息：历史 + 感知 + 当前问题
    parts = []
    hs = _hist_snippet(history)
    if hs:
        parts.append("对话历史（用于指代消解）：\n" + hs)
    if percept_summary:
        parts.append("本轮感知信息：\n" + percept_summary)
    if has_image:
        parts.append("注意：本轮用户**已附带图片**。(need_camera 可保持 false，"
                     "但请在 tool_calls 中加入 anomaly_detect)")
    parts.append("当前问题：%s" % query)
    user_msg = "\n\n".join(parts)

    t0 = time.time()
    try:
        client = _get_client()
        resp = client.chat.completions.create(
            model=ROUTER_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            temperature=0,
            max_tokens=400,
            response_format={"type": "json_object"},
            extra_body={"enable_thinking": False},
        )
        raw = (resp.choices[0].message.content or "").strip()
        # P5-51：路由每轮必调，把 token / 成本记入 trace。
        # 路由用 qwen-flash（实测 279ms），单次 token 少但调用频繁，
        # 累计值能直接说明"路由开销占整轮多少"，是优化决策依据。
        try:
            from observability.trace import record_usage_from_resp
            record_usage_from_resp(resp, node=ROUTER_MODEL + "/router")
        except Exception:
            pass
        dec = _parse_json_loose(raw)
        if not dec or not isinstance(dec, dict):
            raise ValueError("路由返回无法解析为 JSON: %r" % raw[:120])
        dec = _sanitize(dec, query, has_image)
        dec["_source"] = "llm"
        dec["_ms"] = int((time.time() - t0) * 1000)
        _cache_put(cache_key, dec)
        _audit_route(dec, query)
        return dec
    except Exception as e:
        print("[Router] LLM 路由失败(%s: %s)，降级关键词" % (type(e).__name__, str(e)[:120]))
        dec = keyword_route(query, has_image)
        dec["_error"] = "%s: %s" % (type(e).__name__, str(e)[:120])
        try:
            from compliance.gate import audit_log
            audit_log("error", "router_fallback|%s" % dec["_error"][:160])
        except Exception:
            pass
        _cache_put(cache_key, dec)
        _audit_route(dec, query)
        return dec


def _audit_route(dec, query):
    """P5-44：路由决策留痕。

    路由是"该调哪些工具"的决策点，属需审计环节；原先完全没有留痕。
    写成独立函数并吞掉所有异常 —— 审计失败绝不能让路由失败。
    """
    try:
        from compliance.gate import audit_log
        tools = ",".join(c.get("name", "") for c in (dec.get("tool_calls") or []))
        audit_log("router", "src=%s|intent=%s|tools=%s|direct=%s|q=%s" % (
            dec.get("_source", "?"), ",".join(dec.get("intent") or []),
            tools, int(bool(dec.get("direct_answer"))), str(query)[:70]))
    except Exception:
        pass


def _sanitize(dec, query, has_image):
    """校验与归一化模型输出：工具白名单、枚举、类型 + 确定性守卫。"""
    out = {}

    intents = dec.get("intent") or []
    if isinstance(intents, str):
        intents = [intents]
    out["intent"] = [i for i in intents if i in _VALID_INTENTS]

    rq = str(dec.get("rewritten_query") or "").strip().strip('"').strip("「」")
    out["rewritten_query"] = rq if 0 < len(rq) <= 300 else ""

    out["need_camera"] = bool(dec.get("need_camera", False))
    out["need_sensor"] = bool(dec.get("need_sensor", False))
    out["direct_answer"] = bool(dec.get("direct_answer", False))

    calls, seen = [], set()
    for c in (dec.get("tool_calls") or []):
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        if name not in TOOLS or name in seen:   # 白名单 + 去重
            continue
        seen.add(name)
        args = c.get("args") if isinstance(c.get("args"), dict) else {}
        calls.append({
            "name": name,
            "args": args,
            "why": str(c.get("why") or "")[:80],
        })
    out["tool_calls"] = calls

    mp = dec.get("missing_params") or []
    out["missing_params"] = [str(x)[:40] for x in mp if x] if isinstance(mp, list) else []

    # ── 确定性守卫（不依赖小模型是否听话）──────────────────
    # 守卫 1：问题含标准编号 → 必须查库，且不得走"直接作答"。
    # 原实现曾有"JJG 21 是什么标准？"被判 direct_answer 的不一致情况；
    # 这里用正则兜住，保证同一输入永远同一条路径。
    if _STD_RE.search(query or ""):
        out["direct_answer"] = False
        if "kb_search" not in seen:
            out["tool_calls"].append(
                {"name": "kb_search", "args": {"query": query}, "why": "问题含标准编号（路由守卫补齐）"})
            if "kb" not in out["intent"]:
                out["intent"].append("kb")

    # 守卫 2：明确的联网意图 → 必须带 web_search。
    # 实测 qwen-flash 会把"帮我搜索双目视觉标定的办法"判成 plan_task/calib_guide，
    # 而用户说了"搜索"就该联网。这类意图用关键词判定比模型更可靠也更便宜。
    if _WEB_RE.search(query or ""):
        if "web_search" not in {c["name"] for c in out["tool_calls"]}:
            out["tool_calls"].append(
                {"name": "web_search", "args": {"query": query}, "why": "用户明确要求联网（路由守卫补齐）"})
        if "web" not in out["intent"]:
            out["intent"].append("web")
        out["direct_answer"] = False

    # 守卫 3：图片场景必须带视觉工具
    if has_image and "anomaly_detect" not in {c["name"] for c in out["tool_calls"]}:
        out["tool_calls"].append({"name": "anomaly_detect", "args": {}, "why": "本轮附带图片（路由补齐）"})
        if "vision" not in out["intent"]:
            out["intent"].append("vision")

    # 守卫 4：direct_answer 时不应还带工具（图片场景除外）
    if out["direct_answer"] and out["tool_calls"] and not has_image:
        out["tool_calls"] = []

    # 守卫 5：既非 direct_answer 又没有任何工具 → 保守查库
    if not out["direct_answer"] and not out["tool_calls"]:
        out["tool_calls"] = [{"name": "kb_search", "args": {"query": query},
                              "why": "路由未给出工具，兜底查库"}]

    if not out["intent"] and out["tool_calls"]:
        out["intent"] = ["kb"]
    return out


def _cache_put(key, value):
    with _cache_lock:
        _cache[key] = value
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)      # LRU 淘汰（原实现无上限）


def cache_stats():
    with _cache_lock:
        return {"size": len(_cache), "max": _CACHE_MAX, "enabled": ROUTER_ENABLED,
                "model": ROUTER_MODEL}


def clear_cache():
    with _cache_lock:
        _cache.clear()


# ── 计划解析：把"模型意愿"与"硬件事实"求交 ───────────────

def resolve_plan(decision, has_image=False, camera_available=None, sensor_available=None):
    """把路由决策翻译成实际要执行的分支列表。

    这是修复失败案例 2 的关键：原实现用 ``camera_requested=True`` 直接跳过
    compute 分支，但抓帧可能失败。此处**先看设备是否真的可用**，不可用则
    退回到可用的替代分支，并把原因返回给调用方展示。

    Args:
        decision:          route() 的返回值
        has_image:         本轮是否已有图片
        camera_available:  None=未知（先试），True/False=已知
        sensor_available:  同上

    Returns:
        (plan: list[str], notes: list[str])
    """
    plan, notes = ["recall"], []
    intents = set(decision.get("intent") or [])
    names = {c["name"] for c in (decision.get("tool_calls") or [])}
    want_cam = bool(decision.get("need_camera")) or "capture_camera" in names
    want_sensor = bool(decision.get("need_sensor")) or "read_sensors" in names

    def add(b):
        if b not in plan:
            plan.append(b)

    # 视觉：已有图 → 直接分析；要拍照 → 先试，失败由调用方回退
    if has_image or want_cam:
        add("vision")

    # 传感器：感知阶段已统一采集，无需独立分支；仅在明确要读时给提示
    if want_sensor and sensor_available is False:
        notes.append("用户要求读取传感器，但当前未检测到设备，已跳过采集")
    elif want_sensor and sensor_available is None:
        notes.append("传感器按需采集（若未连接会自动跳过）")

    # 计算 / 判定：有明确数值计算意图才排
    compute_tools = {"calc_uncertainty", "compliance_judge", "comp_3d"}
    # DEF-1 修复（由 P4-40 评测发现，修复点在此处）：
    # 原判断只看 intent 列表，**完全不看 direct_answer**。于是当模型
    # 给出 intent=['compute'] 且 direct_answer=True 时，仍会排 compute 分支；
    # 而 node_compute 把 query 原样交给 coordinator._run_duyuan，后者按关键词
    # 命中"不确定度"并用**默认值 mv=100.0mm** 真算一遍 —— 失败案例 3 复发。
    # 现在 direct_answer=True 一律不排 compute（概念/原理类问题不需要数值计算），
    # 除非用户明确要取景（那种情况下 compute 是抓帧失败的替代分支）。
    _direct = bool(decision.get("direct_answer"))
    if (compute_tools & names or "compute" in intents) and not _direct:
        add("compute")
    elif _direct and (compute_tools & names or "compute" in intents):
        notes.append("概念/原理类问题（direct_answer）：已抑制 compute 分支，"
                     "避免用默认值做无意义的数值计算")

    # 关键修复：摄像头不可用时，compute 分支要保留，
    # 否则用户"拍一张"失败后连兜底分析都没有，会落到裸 LLM 拒答
    if want_cam and camera_available is False and "compute" not in plan:
        add("compute")
        notes.append("摄像头不可用：已改排计算/分析分支作为替代，避免落到无工具兜底")

    # 标定指南
    if "calib_guide" in names:
        add("compute")

    # 规划
    if "plan_task" in names or "plan" in intents:
        add("plan_execute")

    # 知识库：只要涉及标准/规程，或上面什么都没排
    if "kb_search" in names or "kb" in intents:
        add("retrieval")

    # 联网
    if "web_search" in names or "web" in intents:
        add("web")

    # 兜底：至少要有一个信息分支
    if not ({"retrieval", "vision", "compute", "plan_execute", "web"} & set(plan)):
        add("retrieval")
        notes.append("路由未给出明确分支，已兜底执行知识库检索")

    return plan, notes


# ── 自测 ──────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        ("JJG 21 是什么标准？", False, []),
        ("A类评定 和 B类评定 有什么区别？", False, []),
        ("实测值10.014 标准值10.00mm 精度0.01mm 帮我做合规判定", False, []),
        ("调用摄像头拍一张，看看现在的情况", False, []),
        ("它适用于什么测量范围", False,
         [{"role": "user", "content": "JJG 21 是什么标准？"},
          {"role": "assistant", "content": "JJG 21-2008《千分尺检定规程》"}]),
        ("帮我搜索双目视觉标定的办法", False, []),
    ]
    print("router cache:", cache_stats())
    print("=" * 78)
    for q, img, hist in tests:
        t0 = time.time()
        d = route(q, history=hist, has_image=img)
        dt = (time.time() - t0) * 1000
        plan, notes = resolve_plan(d, has_image=img)
        print("\nQ: %s" % q)
        print("  来源=%s  耗时=%.0fms" % (d.get("_source"), dt))
        print("  intent=%s  need_camera=%s  need_sensor=%s  direct_answer=%s"
              % (d["intent"], d["need_camera"], d["need_sensor"], d["direct_answer"]))
        if d["rewritten_query"]:
            print("  改写: %s" % d["rewritten_query"])
        print("  tools=%s" % [c["name"] for c in d["tool_calls"]])
        if d.get("missing_params"):
            print("  缺参数: %s" % d["missing_params"])
        print("  -> plan=%s" % plan)
        for n in notes:
            print("     note: %s" % n)
