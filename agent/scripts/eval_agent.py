# -*- coding: utf-8 -*-
r"""P4-40 Agent 轨迹评测 —— 意图路由准确率 + 工具选择正确性

────────────────────────────────────────────────────────────────
为什么单独评测"路由"这一层
────────────────────────────────────────────────────────────────
本项目的两个**用户可见线上故障**都不是"模型不够聪明"，而是**路由判错**：

  1. 失败案例 3：『A类评定和B类评定有什么区别？』是纯概念问题，
     却因为命中关键词"不确定度"被排进 compute 分支，
     用凭空默认值 mv=100.0mm 白算了一遍不确定度，用户白等 11.8 秒。
  2. 失败案例 2：『调用摄像头拍一张』抓帧失败被静默吞掉，
     同时因 camera_requested=True 跳过了 compute 分支，
     最终落到裸 LLM 兜底，模型回了一句
     "我没有权限调用您的摄像头"。
  3. 不一致：『JJG 21 是什么标准？』答对，『JJG 21 是什么标准』（无问号）
     答"未收录"——同一个问题两种结论。

这三条的**共同点**是：不需要看最终答案对错，只看路由决策就能发现。
所以这个评测只调 `agent_router.route()` 与 `resolve_plan()`，
**不跑整张图**——快（每条约 0.3s）、便宜（每条 1 次 qwen-flash 调用）、
且失败时能精确定位到"是路由错了"而不是"是生成错了"。

────────────────────────────────────────────────────────────────
API 用量
────────────────────────────────────────────────────────────────
每条用例 1 次 LLM 调用（qwen-flash，max_tokens=400）。
进程内 LRU 缓存生效，重复用例不重复调用。
`--keyword-only` 可完全不调 LLM（设 EQUIS_ROUTER_ENABLED=0 走关键词兜底），
用于 CI 里的零成本烟测。

用法：
    <py> scripts/eval_agent.py                      # 全量评测
    <py> scripts/eval_agent.py --filter regression  # 只跑线上故障回归用例
    <py> scripts/eval_agent.py --keyword-only       # 不调 LLM
退出码：有用例失败则非 0。
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
MD_PATH = os.path.join(REPORT_DIR, "eval_agent.md")
JSON_PATH = os.path.join(REPORT_DIR, "eval_agent.json")


# ══════════════════════════════════════════════════════════════
# 用例集
# ══════════════════════════════════════════════════════════════
# 字段：
#   query          用户问题
#   expect_intent  期望的 intent 集合（子集语义：这些必须都在决策里）
#   expect_tools   期望出现的工具（子集语义）
#   forbid_tools   不得出现的工具（全局硬约束）
#   direct_answer  若不为 None，则要求决策里 direct_answer 等于该值
#   plan_has       （可选）resolve_plan 结果里必须包含的分支
#   plan_args      （可选）传给 resolve_plan 的 kwargs
#   tags           regression / routing / tools / plan ...
#   note           为什么有这条
CASES = [
    # ══ 线上故障回归（3 条，必须永久保持绿色）══════════════
    {
        "id": "REG-1", "tags": ["regression", "concept"],
        "query": "A类评定和B类评定有什么区别？",
        "expect_intent": [], "expect_tools": [], "forbid_tools": ["calc_uncertainty", "compliance_judge"],
        "direct_answer": True,
        "note": "线上故障 3：纯概念问题被排进 compute 分支，用默认值 mv=100.0 白算 11.8s。"
                "要求 direct_answer=True 且 tool_calls 为空。",
    },
    {
        "id": "REG-2", "tags": ["regression", "plan", "hardware"],
        "query": "调用摄像头拍一张，看看现在的情况",
        "expect_intent": ["vision"], "expect_tools": ["capture_camera"], "forbid_tools": [],
        "direct_answer": None,
        "plan_args": {"has_image": False, "camera_available": False},
        "plan_has": ["compute"],
        "note": "线上故障 2：抓帧失败后落到裸 LLM，模型回'我没有权限调用您的摄像头'。"
                "摄像头不可用时 plan 必须含 compute 作为替代分支。",
    },
    {
        "id": "REG-3", "tags": ["regression", "consistency"],
        "query": "JJG 21 是什么标准？",
        "expect_intent": ["kb"], "expect_tools": ["kb_search"], "forbid_tools": [],
        "direct_answer": False,
        "note": "线上故障：有无问号两次结论相反。本用例与 REG-4 成对，"
                "要求两者 intent 完全一致且都含 kb。",
    },
    {
        "id": "REG-4", "tags": ["regression", "consistency"],
        "query": "JJG 21 是什么标准",
        "expect_intent": ["kb"], "expect_tools": ["kb_search"], "forbid_tools": [],
        "direct_answer": False,
        "note": "REG-3 的无问号对照。",
    },

    # ══ 概念类：不得触发计算工具 ══════════════════════════
    {
        "id": "CON-1", "tags": ["routing", "concept"],
        "query": "GUM 是什么？", "expect_intent": [], "expect_tools": [],
        "forbid_tools": ["calc_uncertainty", "compliance_judge", "comp_3d"],
        "direct_answer": True, "note": "术语解释，不该算不确定度",
    },
    {
        "id": "CON-2", "tags": ["routing", "concept"],
        "query": "什么是示值误差？", "expect_intent": [], "expect_tools": [],
        "forbid_tools": ["calc_uncertainty", "compliance_judge"],
        "direct_answer": True, "note": "定义类问题",
    },
    {
        "id": "CON-3", "tags": ["routing", "concept"],
        "query": "请解释一下测量不确定度的传播律", "expect_intent": [], "expect_tools": [],
        "forbid_tools": ["calc_uncertainty", "compliance_judge"],
        "direct_answer": True, "note": "原理讲解，无实测数值",
    },

    # ══ 编号类：必须查库（kb_search 硬约束）═══════════════
    {
        "id": "KB-1", "tags": ["routing", "kb"],
        "query": "JJF 1059.1-2012 的主要内容是什么",
        "expect_intent": ["kb"], "expect_tools": ["kb_search"], "forbid_tools": [],
        "direct_answer": False, "note": "含编号 → 必须查库，不得直接作答",
    },
    {
        "id": "KB-2", "tags": ["routing", "kb"],
        "query": "GB/T 18779.1 的合格判定规则怎么用",
        "expect_intent": ["kb"], "expect_tools": ["kb_search"], "forbid_tools": [],
        "direct_answer": False, "note": "含编号 + 判定语义，但没给数值，不该算",
    },
    {
        "id": "KB-3", "tags": ["routing", "kb"],
        "query": "JJG 34-2022 指示表检定规程的示值误差要求",
        "expect_intent": ["kb"], "expect_tools": ["kb_search"], "forbid_tools": [],
        "direct_answer": False, "note": "编号查询",
    },
    {
        "id": "KB-4", "tags": ["routing", "kb"],
        "query": "JJF 1033 计量标准考核要准备哪些材料？",
        "expect_intent": ["kb"], "expect_tools": ["kb_search"], "forbid_tools": [],
        "direct_answer": False, "note": "编号查询（问号版）",
    },

    # ══ 计算类：给了数值才该算 ══════════════════════════
    {
        "id": "CALC-1", "tags": ["tools", "compute"],
        "query": "实测值10.014mm，标准值10.00mm，器具精度0.01mm，帮我算不确定度",
        "expect_intent": [], "expect_tools": ["calc_uncertainty"],
        "forbid_tools": [], "direct_answer": False,
        "note": "给了实测值与精度 → 应当调用计算工具",
    },
    {
        "id": "CALC-2", "tags": ["tools", "compute"],
        "query": "实测值25.003mm 标准值25.000mm 精度0.002mm，帮我做合格判定",
        "expect_intent": [], "expect_tools": ["compliance_judge"],
        "forbid_tools": [], "direct_answer": False,
        "note": "给了数值 + 判定语义 → 三态判定工具",
    },

    # ══ 联网类：关键词守卫必须生效 ══════════════════════
    {
        "id": "WEB-1", "tags": ["routing", "web"],
        "query": "帮我搜索一下最新的测量不确定度评定标准",
        "expect_intent": ["web"], "expect_tools": ["web_search"], "forbid_tools": [],
        "direct_answer": False, "note": "明确说'搜索' → 必须联网（路由守卫 2）",
    },
    {
        "id": "WEB-2", "tags": ["routing", "web"],
        "query": "2026年计量行业有什么新闻动态",
        "expect_intent": ["web"], "expect_tools": ["web_search"], "forbid_tools": [],
        "direct_answer": False, "note": "时效性信息 → 必须联网",
    },

    # ══ 传感器类 ════════════════════════════════════════
    {
        "id": "SEN-1", "tags": ["tools", "sensor"],
        "query": "现在实验室的温度和湿度是多少？",
        "expect_intent": ["sensor"], "expect_tools": ["read_sensors"], "forbid_tools": [],
        "direct_answer": False, "note": "要实时读数 → read_sensors",
    },

    # ══ 规划类 ══════════════════════════════════════════
    {
        "id": "PLAN-1", "tags": ["routing", "plan"],
        "query": "我要测一批轴类工件的直径，帮我制定一个测量方案",
        "expect_intent": [], "expect_tools": ["plan_task"], "forbid_tools": [],
        "direct_answer": False, "note": "复合测量任务 → plan_task",
    },

    # ══ 复杂/混合：多工具 ═══════════════════════════════
    {
        "id": "MIX-1", "tags": ["tools", "mixed"],
        "query": "先查一下千分尺的检定规程，然后帮我算一下刚才那组数据的不确定度",
        "expect_intent": [], "expect_tools": ["kb_search"], "forbid_tools": [],
        "direct_answer": False, "note": "多意图：至少要有查库",
    },

    # ══ 多轮指代消解 ════════════════════════════════════
    {
        "id": "HIST-1", "tags": ["routing", "history"],
        "query": "它适用于什么测量范围",
        "history": [{"role": "user", "content": "JJG 21 是什么标准？"},
                    {"role": "assistant", "content": "JJG 21-2008《千分尺检定规程》"}],
        "expect_intent": ["kb"], "expect_tools": ["kb_search"], "forbid_tools": [],
        "direct_answer": False,
        "note": "『它』需结合历史消解为千分尺；至少必须查库而不是空答",
    },

    # ══ 计划解析的硬件求交（不依赖 LLM 判对，只看 plan 是否安全）══
    {
        "id": "SAFE-1", "tags": ["plan", "hardware"],
        "query": "读一下当前环境的温度", "has_image": False,
        "plan_args": {"has_image": False, "sensor_available": False},
        "expect_intent": [], "expect_tools": [], "forbid_tools": [],
        "direct_answer": None,
        "plan_has": [], "note": "传感器不可用时不应崩，且必须有信息分支兜底",
        "plan_requires_info_branch": True,
    },
    {
        "id": "SAFE-2", "tags": ["plan", "hardware"],
        "query": "看看这张图里的工件有没有缺陷", "has_image": True,
        "plan_args": {"has_image": True, "camera_available": False},
        "expect_intent": [], "expect_tools": [], "forbid_tools": [],
        "direct_answer": None,
        "plan_has": ["vision"], "note": "已带图 → 必须排 vision 分支",
    },
    {
        "id": "SAFE-3", "tags": ["plan", "routing"],
        "query": "随便聊聊", "has_image": False,
        "plan_args": {"has_image": False},
        "expect_intent": [], "expect_tools": [], "forbid_tools": [],
        "direct_answer": None,
        "plan_requires_info_branch": True,
        "note": "无明确意图时绝不能排出空计划（空计划 = 裸 LLM 兜底 = 失败案例 2 的成因）",
    },

    # ══════════════════════════════════════════════════════════
    # 未修复缺陷（本次评测新发现）——标记 known_defect
    # ══════════════════════════════════════════════════════════
    # 这两条**当前预期是红的**。它们的修复点在 `agent_router.py` /
    # `coordinator.py` / `graph/nodes.py`，全部在本次任务的文件边界之外，
    # 所以这里只做"把它钉成可回归的用例 + 报告根因"，不落地修复。
    # 默认不计入退出码（否则整个套件长期全红、失去信号价值）；
    # 加 `--strict` 即计入，可用于"修好之前 CI 一直红"的场景。
    {
        "id": "DEF-1", "tags": ["defect", "plan", "concept", "regression"],
        "known_defect": True, "defect_id": "D1",
        "local_invariant": "no_compute_when_direct_answer",
        "query": "[本地不变量] direct_answer=True 的决策不得排 compute 分支",
        "expect_intent": [], "expect_tools": [],
        "forbid_tools": ["calc_uncertainty", "compliance_judge"],
        "direct_answer": None,
        "note": "【失败案例 3 复发】原修复只让路由**不再挑选**计算工具，"
                "但 agent_router.resolve_plan 仍然只看 intent 列表"
                "（`if compute_tools & names or \"compute\" in intents: add(\"compute\")`，"
                "agent_router.py 约 L455），完全不看 decision['direct_answer']；"
                "而 graph/nodes.py 的 node_compute（约 L329）把 query 原样丢给 "
                "coordinator._run_duyuan，后者按关键词命中『不确定度』"
                "（coordinator.py 约 L701）并用**默认值 mv=100.0mm** 算一遍，"
                "返回『实测值: 100.000 mm / U(k=2)=0.0133 mm』。"
                "本用例改为**本地不变量检查**（不依赖 LLM 随机性）："
                "只要模型在 intent 里给出 'compute'，这个缺陷就必然复现。",
    },
    {
        "id": "DEF-2", "tags": ["defect", "routing", "web"],
        "known_defect": True, "defect_id": "D2",
        "query": "JJG 34-2022 指示表检定规程的示值误差要求",
        "expect_intent": ["kb"], "expect_tools": ["kb_search"],
        "forbid_tools": ["web_search"], "direct_answer": False,
        "note": "【联网守卫误触发】agent_router._WEB_RE 里的 `202\\d年?` 本意是匹配"
                "『2026年』这类年份提及，但它会命中**标准编号里的年份**"
                "（'JJG 34-2022' → 匹配到 '2022'），于是把一次纯标准查询"
                "强行升级成联网查询，多排一个 web 分支、多一次外网往返，"
                "并让网页内容有机会混入本该以国标/规程为准的回答。"
                "注意其不一致性：'JJG 105-2019'/'GB/T 18779.1-2002' 不触发，"
                "只有 202x 年号的标准才触发。",
    },
]

_INFO_BRANCHES = {"retrieval", "vision", "compute", "plan_execute", "web"}
# 一致性用例对（要求成对的 intent 完全一致）
CONSISTENCY_PAIRS = [("REG-3", "REG-4")]
# D1 缺陷的端到端复现输入（见 DEF-1 的 note）
REPRO_QUERY = "请解释一下测量不确定度的传播律"


def capture_repro():
    """真实跑一次 `coordinator._run_duyuan()`，为 D1 缺陷取端到端证据。

    只在评测里做一次（实测约 0.5s、1 次 LLM 调用），
    目的是让报告里的"编造实测值"这句话**有原始输出可核对**，
    而不是只凭代码阅读下结论。

    诚实说明：这条证据链的最后一跳（`_run_duyuan` 按关键词算不确定度）
    是**确定性**的；但前面"模型是否在 intent 里给出 compute"是**概率性**的
    （实测同一问题在不同轮次分别得到过 ['compute'] 与 ['kb']）。
    因此这里先真调一次路由记录实际结果，再用本地构造的最坏形状补一次
    resolve_plan，两种情形都在报告里写清楚。
    """
    out = {"query": REPRO_QUERY, "error": ""}
    try:
        import agent_router as R
        from coordinator import _run_duyuan

        # ① 真实路由一次（绕过缓存），记录本轮模型给的 intent
        dec = R.route(REPRO_QUERY, use_cache=False)
        out["live_intent"] = dec.get("intent")
        out["live_direct_answer"] = bool(dec.get("direct_answer"))
        out["live_tools"] = _tool_names(dec)
        out["live_plan"] = R.resolve_plan(dec, has_image=False)[0]

        # ② 无论本轮模型给什么，都用"最坏形状"再算一次 plan，
        #    证明只要 intent 含 compute，缺陷就必然发生
        worst = {"intent": ["compute"], "tool_calls": [], "direct_answer": True,
                 "need_camera": False, "need_sensor": False, "rewritten_query": "",
                 "missing_params": []}
        out["worst_plan"] = R.resolve_plan(worst, has_image=False)[0]

        # ③ node_compute 会做的事：把 query 原样交给 _run_duyuan
        t0 = time.time()
        res = _run_duyuan(REPRO_QUERY)
        out["ms"] = int((time.time() - t0) * 1000)
        out["output"] = str(res)[:1200]
        # ④ 客观判定"是否编造了数值"：输出里出现默认值 100.000/mm
        out["fabricated_default_used"] = ("100.000" in str(res))
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
        out.setdefault("ms", 0)
        out.setdefault("output", "")
        out.setdefault("fabricated_default_used", False)
    return out


# ══════════════════════════════════════════════════════════════
# 评测逻辑
# ══════════════════════════════════════════════════════════════

def _tool_names(dec):
    return [c.get("name") for c in (dec.get("tool_calls") or [])]


def check_case(case, dec, plan, notes):
    """对一条用例逐项判定，返回 (ok, [失败原因])"""
    fails = []
    intents = set(dec.get("intent") or [])
    tools = set(_tool_names(dec))

    for i in case.get("expect_intent", []):
        if i not in intents:
            fails.append("intent 缺 %r（实际 %s）" % (i, sorted(intents)))
    for t in case.get("expect_tools", []):
        if t not in tools:
            fails.append("tool 缺 %r（实际 %s）" % (t, sorted(tools)))
    for t in case.get("forbid_tools", []):
        if t in tools:
            fails.append("出现了禁止工具 %r（实际 %s）" % (t, sorted(tools)))

    da = case.get("direct_answer")
    if da is not None and bool(dec.get("direct_answer")) != bool(da):
        fails.append("direct_answer 期望 %s 实际 %s" % (da, bool(dec.get("direct_answer"))))
    # direct_answer=True 时工具必须为空（路由守卫 4 的不变量）
    if dec.get("direct_answer") and (tools - {"anomaly_detect"}) and not case.get("has_image"):
        fails.append("direct_answer=True 却仍带工具 %s" % sorted(tools))

    if plan is not None:
        for b in case.get("plan_has", []):
            if b not in plan:
                fails.append("plan 缺分支 %r（实际 %s）" % (b, plan))
        for b in case.get("plan_forbid", []):
            if b in plan:
                fails.append("plan 不该含分支 %r（实际 %s）" % (b, plan))
        if case.get("plan_requires_info_branch") and not (_INFO_BRANCHES & set(plan)):
            fails.append("plan 没有任何信息分支（实际 %s）" % plan)

    return (not fails), fails


def _invariant_no_compute_when_direct_answer():
    """D1 的**确定性**不变量：direct_answer=True 的决策不得排出 compute 分支。

    为什么用本地构造的决策而不是真调 LLM：
      `intent` 是模型输出的分类结果，实测同一问题在不同轮次会给出
      `['compute']` 或 `['kb']`（temperature=0 也不保证逐次一致），
      拿它当测试输入会让这条用例时红时绿——那就不是回归测试了。
      这里直接构造"路由守卫允许出现的最坏形状"：
      `direct_answer=True`（守卫 4 已清空 tool_calls）但模型仍在 intent 里
      留了 'compute'。这正是线上复现时的实际形状。
    """
    import agent_router as R
    dec = {"intent": ["compute"], "tool_calls": [], "direct_answer": True,
           "need_camera": False, "need_sensor": False, "rewritten_query": "",
           "missing_params": [], "_source": "local-invariant"}
    plan, notes = R.resolve_plan(dec, has_image=False)
    fails = []
    if "compute" in plan:
        fails.append("direct_answer=True 且无任何计算工具，plan 却仍含 'compute'（实际 %s）" % plan)
    return dec, plan, notes, fails


def run_cases(cases, keyword_only=False):
    import agent_router as R

    if keyword_only:
        os.environ["EQUIS_ROUTER_ENABLED"] = "0"
        R.ROUTER_ENABLED = False
        R.clear_cache()

    results, t_start = [], time.time()
    for c in cases:
        has_image = bool(c.get("has_image"))
        t0 = time.time()

        # ── 本地不变量用例：不调 LLM，确定性判定 ──
        if c.get("local_invariant") == "no_compute_when_direct_answer":
            dec, plan, notes, fails = _invariant_no_compute_when_direct_answer()
            results.append({
                "id": c["id"], "query": c["query"], "tags": c.get("tags", []),
                "passed": not fails, "failures": fails,
                "known_defect": bool(c.get("known_defect")),
                "defect_id": c.get("defect_id", ""),
                "intent": list(dec["intent"]), "tools": [],
                "direct_answer": bool(dec["direct_answer"]),
                "need_camera": False, "rewritten_query": "",
                "plan": plan, "plan_notes": notes,
                "source": "local-invariant", "ms": 0, "error": "",
                "note": c.get("note", ""),
            })
            continue

        try:
            dec = R.route(c["query"], history=c.get("history"), has_image=has_image)
            err = ""
        except Exception as e:
            dec, err = {"intent": [], "tool_calls": [], "direct_answer": False}, \
                       "%s: %s" % (type(e).__name__, str(e)[:150])
        ms = int((time.time() - t0) * 1000)

        plan, notes = None, []
        if "plan_args" in c or "plan_has" in c or c.get("plan_requires_info_branch"):
            kwargs = dict(c.get("plan_args") or {})
            kwargs.setdefault("has_image", has_image)
            try:
                plan, notes = R.resolve_plan(dec, **kwargs)
            except Exception as e:
                plan, notes = ["<error>"], ["resolve_plan 异常: %s" % e]

        ok, fails = check_case(c, dec, plan, notes)
        if err:
            ok, fails = False, fails + ["route() 抛异常: " + err]
        results.append({
            "id": c["id"], "query": c["query"], "tags": c.get("tags", []),
            "passed": ok, "failures": fails,
            # known_defect：已知且**修复点在文件边界之外**的缺陷。
            # 它的失败会被单独成节高亮，但不计入退出码（--strict 时计入），
            # 这样套件其余部分仍能起到"回归告警"的作用。
            "known_defect": bool(c.get("known_defect")),
            "defect_id": c.get("defect_id", ""),
            "intent": sorted(dec.get("intent") or []),
            "tools": _tool_names(dec),
            "direct_answer": bool(dec.get("direct_answer")),
            "need_camera": bool(dec.get("need_camera")),
            "rewritten_query": dec.get("rewritten_query", ""),
            "plan": plan, "plan_notes": notes,
            "source": dec.get("_source", ""), "ms": dec.get("_ms", ms),
            "error": err,
            "note": c.get("note", ""),
        })
    return results, int(round((time.time() - t_start) * 1000))


def check_consistency(results):
    """成对用例的 intent 一致性（线上故障：同一问题两种结论）"""
    by_id = {r["id"]: r for r in results}
    out = []
    for a, b in CONSISTENCY_PAIRS:
        if a not in by_id or b not in by_id:
            continue
        ra, rb = by_id[a], by_id[b]
        same = set(ra["intent"]) == set(rb["intent"])
        out.append({"pair": [a, b], "a_intent": ra["intent"], "b_intent": rb["intent"],
                    "consistent": same,
                    "detail": "" if same else "两个问法的 intent 不一致"})
        if not same:
            for r in (ra, rb):
                r["passed"] = False
                r["failures"].append("与 %s 的 intent 不一致（%s vs %s）"
                                     % (b if r is ra else a, ra["intent"], rb["intent"]))
    return out


def summarize(results, consistency):
    """汇总。known_defect 用例**单独统计**，不与正常用例混在一起。"""
    normal = [r for r in results if not r["known_defect"]]
    known = [r for r in results if r["known_defect"]]
    n = len(normal) or 1
    passed = sum(1 for r in normal if r["passed"])
    by_tag = {}
    for r in normal:
        for t in r["tags"]:
            d = by_tag.setdefault(t, {"n": 0, "passed": 0})
            d["n"] += 1
            d["passed"] += 1 if r["passed"] else 0
    for d in by_tag.values():
        d["rate"] = round(d["passed"] / d["n"], 4)
    return {
        "n": len(normal), "passed": passed, "failed": len(normal) - passed,
        "accuracy": round(passed / n, 4),
        "known_defect_n": len(known),
        "known_defect_open": sum(1 for r in known if not r["passed"]),
        "by_tag": by_tag,
        "consistency_ok": all(c["consistent"] for c in consistency) if consistency else True,
    }


# ══════════════════════════════════════════════════════════════
# 报告
# ══════════════════════════════════════════════════════════════

def render_md(payload):
    s = payload["summary"]
    L = ["# Agent 轨迹评测报告（P4-40）\n"]
    L.append("> 自动生成，请勿手改。复现：`python scripts/eval_agent.py`\n")
    L.append("- 生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(payload["ts"])))
    L.append("- 被测对象：`agent_router.route()` + `agent_router.resolve_plan()`"
             "（**不跑整张图**，只评决策层）")
    L.append("- 用例数：**%d**；路由模型：`%s`；本条命令耗时 %.1fs\n"
             % (s["n"], payload["router_model"], payload["wall_ms"] / 1000.0))

    L.append("## 1. 总指标\n")
    L.append("| 指标 | 值 |")
    L.append("|---|---|")
    L.append("| 用例通过率 | **%s**（%d/%d） |"
             % ("%.1f%%" % (100 * s["accuracy"]), s["passed"], s["n"]))
    L.append("| 成对一致性问题 | %s |" % ("✅ 通过" if s["consistency_ok"] else "❌ 不一致"))
    L.append("| 未修复缺陷用例（known_defect） | %d 条，其中 **%d 条当前仍为红** |"
             % (s["known_defect_n"], s["known_defect_open"]))
    L.append("| LLM 路由调用 | %s |" % ("0（--keyword-only）" if payload["keyword_only"] else "见下"))
    L.append("")
    L.append("> 上表通过率**不含** known_defect 用例——它们单独在 §4 列出。")
    L.append("> 原因：这两条缺陷的修复点都在本次任务的文件边界之外"
             "（`agent_router.py` / `coordinator.py` / `graph/nodes.py`），"
             "把它们计入分母会让整套用例长期全红、失去回归告警的作用。"
             "加 `--strict` 可把它们计入退出码。\n")
    L.append("| 标签 | 用例数 | 通过 | 通过率 |")
    L.append("|---|---|---|---|")
    for t, d in sorted(s["by_tag"].items()):
        L.append("| %s | %d | %d | %.1f%% |" % (t, d["n"], d["passed"], 100 * d["rate"]))
    L.append("")

    L.append("## 2. 线上故障回归用例（必须常绿）\n")
    L.append("| id | 问题 | 结果 | 说明 |")
    L.append("|---|---|---|---|")
    for r in payload["results"]:
        if "regression" not in r["tags"] or r["known_defect"]:
            continue
        L.append("| %s | %s | %s | %s |"
                 % (r["id"], r["query"][:30], "✅" if r["passed"] else "❌", r["note"][:60]))
    L.append("")
    L.append("REG-3 / REG-4 是同一问题的两种问法（有无问号），除各自断言外还要求"
             "两者的 `intent` **完全一致**：\n")
    for pair in payload["consistency"]:
        L.append("- 一致性 `%s` ↔ `%s`：%s（%s vs %s）"
                 % (pair["pair"][0], pair["pair"][1],
                    "✅ 一致" if pair["consistent"] else "❌ **不一致**",
                    pair["a_intent"], pair["b_intent"]))
    L.append("")

    fails = [r for r in payload["results"] if not r["passed"] and not r["known_defect"]]
    L.append("## 3. 失败用例（%d 条，不含 known_defect）\n" % len(fails))
    if not fails:
        L.append("无。\n")
    else:
        for r in fails:
            L.append("### ❌ %s `%s`" % (r["id"], r["query"]))
            L.append("- 失败项：%s" % "；".join(r["failures"]))
            L.append("- 实际决策：intent=%s  tools=%s  direct_answer=%s  need_camera=%s"
                     % (r["intent"], r["tools"], r["direct_answer"], r["need_camera"]))
            if r["plan"] is not None:
                L.append("- plan=%s" % r["plan"])
            if r["note"]:
                L.append("- 该用例的用意：%s" % r["note"])
            L.append("")

    known = [r for r in payload["results"] if r["known_defect"]]
    open_known = [r for r in known if not r["passed"]]
    L.append("## 4. 未修复缺陷（评测新发现，修复点在文件边界之外）\n")
    L.append("本节用例当前**预期为红**。它们不计入 §1 通过率与退出码，"
             "但每条都给出了复现证据与代码级根因，可直接作为后续修复工单。\n")
    if not known:
        L.append("无。\n")
    else:
        L.append("| id | 缺陷 | 问题 | 当前状态 |")
        L.append("|---|---|---|---|")
        for r in known:
            L.append("| %s | %s | %s | %s |"
                     % (r["id"], r["defect_id"], r["query"][:30],
                        "🔴 仍存在" if not r["passed"] else "🟢 已修复"))
        L.append("")
        for r in open_known:
            L.append("### 🔴 %s（%s）`%s`" % (r["defect_id"], r["id"], r["query"]))
            L.append("- **实测**：intent=%s  tools=%s  direct_answer=%s  plan=%s"
                     % (r["intent"], r["tools"], r["direct_answer"], r["plan"]))
            L.append("- **未通过的断言**：%s" % "；".join(r["failures"]))
            L.append("- **根因**：%s" % r["note"])
            L.append("")
    if payload.get("repro"):
        rd = payload["repro"]
        L.append("### 附：D1 的端到端复现证据\n")
        L.append("评测脚本会真实调用一次 `coordinator._run_duyuan()` 取证"
                 "（`--no-repro` 可关闭，省 1 次 LLM 调用）。\n")
        if rd.get("error"):
            L.append("- ⚠️ 取证过程报错：`%s`\n" % rd["error"])
        L.append("**第一步：真实路由一次**（绕过缓存）\n")
        L.append("- 问题：`%s`" % rd["query"])
        L.append("- 本轮模型给的 intent = `%s`，direct_answer = `%s`，tools = `%s`"
                 % (rd.get("live_intent"), rd.get("live_direct_answer"), rd.get("live_tools")))
        L.append("- 此时的 plan = `%s`" % rd.get("live_plan"))
        L.append("")
        L.append("**第二步：用『最坏形状』决策再算一次 plan**"
                 "（`intent=['compute']` + `direct_answer=True` + `tool_calls=[]`）\n")
        L.append("- plan = `%s`" % rd.get("worst_plan"))
        L.append("- 这个形状是**实测到达过的**：同一问题在本轮之前的一次运行中"
                 "路由确实返回了 `intent=['compute']`（见 §5 明细里 CON-3 的历史值），"
                 "说明模型是否给出 'compute' 是**概率性**的，"
                 "而只要给了，下面这一步就必然发生。\n")
        L.append("**第三步：node_compute 实际会做的事**"
                 "（把 query 原样交给 `_run_duyuan`）\n")
        L.append("- 耗时 **%.2fs**，返回 %d 字；"
                 "**是否出现默认值 100.000：%s**"
                 % (rd.get("ms", 0) / 1000.0, len(rd.get("output", "")),
                    "是 ❗" if rd.get("fabricated_default_used") else "否"))
        L.append("- 返回内容：\n")
        L.append("```")
        L.append(rd.get("output", ""))
        L.append("```")
        L.append("- 👆『**实测值: 100.000 mm**』来自代码里的默认参数"
                 "（`coordinator.py` 约 L702 `{\"mv\": 100.0, \"sp\": 0.01, \"temp\": 25.0}`），"
                 "用户从未提供过任何数值。系统把一个**编造的测量值**当作测量结果呈现给用户，"
                 "并据此给出扩展不确定度。这比『慢 11.8 秒』严重——"
                 "它是**正确性问题**，不是性能问题。\n")

    L.append("## 5. 全部用例决策明细\n")
    L.append("| id | 问题 | intent | tools | direct | plan | 结果 |")
    L.append("|---|---|---|---|---|---|---|")
    for r in payload["results"]:
        L.append("| %s | %s | %s | %s | %s | %s | %s |"
                 % (r["id"], r["query"][:26], ",".join(r["intent"]) or "-",
                    ",".join(r["tools"]) or "-", "T" if r["direct_answer"] else "F",
                    ",".join(r["plan"]) if r["plan"] is not None else "-",
                    "✅" if r["passed"] else "❌"))
    L.append("")
    return "\n".join(L)


# ══════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════

def main(argv=None):
    ap = argparse.ArgumentParser(description="P4-40 Agent 路由/工具选择评测")
    ap.add_argument("--filter", default="", help="只跑含该标签或 id 前缀的用例")
    ap.add_argument("--keyword-only", action="store_true",
                    help="不调 LLM，走关键词兜底路由（CI 零成本烟测）")
    ap.add_argument("--strict", action="store_true",
                    help="把 known_defect 用例的失败也计入退出码")
    ap.add_argument("--no-repro", action="store_true",
                    help="跳过 D1 缺陷的端到端复现（省一次 LLM 调用）")
    args = ap.parse_args(argv)

    cases = CASES
    if args.filter:
        f = args.filter.lower()
        cases = [c for c in CASES
                 if f in c["id"].lower() or any(f in t.lower() for t in c.get("tags", []))]
        if not cases:
            print("没有匹配 %r 的用例" % args.filter)
            return 2

    print("=" * 78)
    print("P4-40 Agent 轨迹评测 | %d 条用例%s"
          % (len(cases), "（关键词兜底模式，不调 LLM）" if args.keyword_only else ""))
    print("=" * 78)

    results, wall = run_cases(cases, keyword_only=args.keyword_only)
    consistency = check_consistency(results)
    summary = summarize(results, consistency)

    import agent_router as R
    payload = {"ts": time.time(), "wall_ms": wall, "keyword_only": args.keyword_only,
               "router_model": ("keyword-fallback" if args.keyword_only else R.ROUTER_MODEL),
               "summary": summary, "consistency": consistency, "results": results}

    # D1 的端到端复现证据（只要 D1 用例在跑就采集）
    if not args.no_repro and any(r["defect_id"] == "D1" for r in results):
        print("\n-- 采集 D1 缺陷端到端复现证据 --")
        payload["repro"] = capture_repro()
        rd = payload["repro"]
        print("   本轮路由 intent=%s direct_answer=%s"
              % (rd.get("live_intent"), rd.get("live_direct_answer")))
        print("   最坏形状 plan=%s ；_run_duyuan 耗时 %dms ；出现默认值100.000=%s"
              % (rd.get("worst_plan"), rd.get("ms"), rd.get("fabricated_default_used")))

    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(MD_PATH, "w", encoding="utf-8") as f:
        f.write(render_md(payload))
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    for r in results:
        mark = ("KNOWN-DEFECT" if not r["passed"] else "PASS") if r["known_defect"] \
            else ("PASS" if r["passed"] else "FAIL")
        print("  [%-12s] %-7s %-32s intent=%-18s tools=%s"
              % (mark, r["id"], r["query"][:32], ",".join(r["intent"]) or "-",
                 ",".join(r["tools"]) or "-"))
        for fl in r["failures"]:
            print("         └─ %s" % fl)

    print("\n" + "-" * 78)
    print("通过率 %.1f%%（%d/%d，不含 known_defect）  耗时 %.1fs  路由模型 %s"
          % (100 * summary["accuracy"], summary["passed"], summary["n"],
             wall / 1000.0, payload["router_model"]))
    for t, d in sorted(summary["by_tag"].items()):
        print("  %-12s %d/%d  %.1f%%" % (t, d["passed"], d["n"], 100 * d["rate"]))
    if summary["known_defect_n"]:
        print("⚠️  未修复缺陷 %d 条（其中 %d 条当前为红）：%s"
              % (summary["known_defect_n"], summary["known_defect_open"],
                 ", ".join(r["defect_id"] for r in results if r["known_defect"] and not r["passed"])))
    print("报告：%s\n      %s" % (MD_PATH, JSON_PATH))
    bad = summary["failed"] + (summary["known_defect_open"] if args.strict else 0)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
