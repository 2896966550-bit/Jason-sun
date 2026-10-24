"""图节点实现

设计原则：节点只负责"调用已有能力 + 更新状态"，不重复实现业务逻辑。
规元/度元/索元的既有实现（coordinator.py）被直接复用，
保证 graph 引擎与 legacy 引擎行为一致、结论可比。
"""
import os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from graph.state import AgentState, NODE_LABELS

# 触发传感器采集的意图关键词
_SENSOR_HINTS = ["环境", "温度", "湿度", "振动", "传感器", "采集", "现在", "当前", "实时"]


# ── 工具函数 ────────────────────────────────────────────

def _cb(config):
    """从 graph config 中取出状态回调（用于前端实时展示）"""
    if not config:
        return None
    return (config.get("configurable") or {}).get("on_status")


def _status(config, node_id, state_str):
    cb = _cb(config)
    if cb:
        try:
            cb(node_id, state_str)
        except Exception:
            pass


def _trace(node: str, t0: float, note: str = ""):
    """生成一条带 reducer 的执行轨迹（list，便于并行合并）

    同时写入 SQLite trace（通过 contextvars 自动归属当前 run，
    并行分支无需传 config，也不会串号）。
    """
    ms = int((time.time() - t0) * 1000)
    try:
        from observability.trace import step as _trace_step
        _trace_step(node, NODE_LABELS.get(node, node), ms, note)
    except Exception:
        pass  # 可观测失败不影响主流程
    return [{
        "node": node,
        "label": NODE_LABELS.get(node, node),
        "ms": ms,
        "note": str(note)[:120],
    }]


def _context(state: AgentState) -> str:
    """构造子 Agent 共享上下文：多轮历史 + 感知摘要 + 图片预理解 + 历史记忆"""
    parts = []
    hist = state.get("history") or []
    if hist:
        parts.append("对话历史：")
        for h in hist[-6:]:
            role = "用户" if h.get("role") == "user" else "Equis"
            parts.append(f"{role}: {str(h.get('content', ''))[:200]}")
    ps = state.get("percept_summary")
    if ps:
        parts.append("感知信息：\n" + ps)
    if state.get("vision_pre"):
        parts.append("图像理解结果（供检索标准与判定参考）：" + state["vision_pre"])
    if state.get("memory_text"):
        parts.append(state["memory_text"])
    return "\n\n".join(parts)


def _make_status_cb(config):
    """把 graph 的 config 适配成 coordinator 需要的 on_status 回调"""
    def _f(agent_id, state_str):
        _status(config, agent_id, state_str)
    return _f



def _audit_branch(branch: str, t0: float, ok: bool = True, note: str = ""):
    """P5-44：记录一次分支执行（工具调用留痕）。

    graph 引擎下子智能体由本文件的分支节点直接调用
    （node_retrieval / node_vision / node_compute / node_web / node_plan_execute），
    因此埋点必须放在这里，而不是 coordinator 的 legacy 并发调度处 ——
    否则默认引擎（graph）下 tool_call 永远只有测试数据。

    维护（2026-10-17）：同时写 trace.record_tool()。
    此前 trace 的 tool_calls 统计**从未被生产代码调用**（全项目只有定义与测试
    引用），实测 237 次运行里 tool_calls>0 的记录为 0 —— 与 LLM 用量记账当初
    的漏法完全相同：接口和测试都齐了，唯独没接线。
    两套留痕职责不同、都要写：
      · compliance.audit_tool_call  -> audit.db，面向合规追溯（不可篡改叙事）
      · observability.record_tool   -> trace.db，面向运行分析（与耗时/用量并列）
    """
    elapsed = int((time.time() - t0) * 1000)
    try:
        from compliance.gate import audit_tool_call
        audit_tool_call(branch, ok, "%dms%s" % (
            elapsed, ("|" + note) if note else ""))
    except Exception:
        pass
    try:
        from observability.trace import record_tool
        record_tool(branch, elapsed, ok)
    except Exception:
        pass

# ── 节点：感知采集 ──────────────────────────────────────

def node_perceive(state: AgentState, config=None) -> dict:
    """感知总线采集 + 图片预理解（一次采集，全图共享）"""
    t0 = time.time()
    _status(config, "perceive", "active")

    query = state.get("query", "") or ""
    image_path = state.get("image_path")
    from perception.measurement_context import new_context, bind_percepts
    measurement_context = state.get("measurement_context") or new_context(query=query)
    has_image = bool(image_path and os.path.exists(image_path))

    # 合规 Gate：图片进入视觉分析前自动人脸脱敏（检测到人脸才打码）
    #
    # 2026-10-17：redact_faces 改为返回 (path, n_faces, status)。
    # status="unavailable" 表示**检测器本身不可用**（如 cv2 5 移除了 Haar 级联），
    # 这与"检测器可用但图里没人脸"（status="none"）是两件事，不能混为一谈 ——
    # 原实现把前者也当成后者，于是"脱敏"在失败时静默通过（fail-open）。
    if has_image:
        try:
            from compliance.gate import redact_faces, audit_log
            image_path, n_faces, redact_status = redact_faces(image_path)
            if n_faces:
                audit_log("redact", f"{n_faces} 张人脸已自动脱敏: {os.path.basename(image_path)}")
            elif redact_status == "unavailable":
                # 不阻断主流程（图仍可分析），但要让状态可见，不能假装脱敏过了
                print("[Graph] 警告：人脸脱敏检测器不可用，本图未做脱敏校验")
        except Exception as e:
            print(f"[Graph] 图片脱敏失败: {e}")

    # 摄像头意图："调用摄像头/拍一张/看看现在" → 感知总线自动抓一帧
    _CAM_HINTS = ["摄像头", "拍照", "拍一张", "拍个", "拍下", "镜头", "相机", "看一下", "看看现在", "看看周围"]
    want_camera = (not has_image) and any(w in query for w in _CAM_HINTS + ["检测工件","表面","缺陷","外观"])

    want_sensor = any(w in query for w in _SENSOR_HINTS) or has_image
    percepts, summary_txt = [], ""
    try:
        from perception.bus import collect, to_state, summary
        ps = collect(query=query, image_path=image_path if has_image else None,
                     want_camera=want_camera, want_sensor=want_sensor)
        percepts = to_state(ps)
        summary_txt = summary(ps)
    except Exception as e:
        print(f"[Graph] 感知采集失败: {e}")

    # One canonical image, including a captured camera frame, is shared by all branches.
    if not has_image:
        for percept in percepts:
            if percept.get("type") == "camera":
                image_path = (percept.get("payload") or {}).get("path")
                if image_path and os.path.isfile(image_path):
                    try:
                        from compliance.gate import redact_faces
                        image_path, _faces, _redact_status = redact_faces(image_path)
                    except Exception as exc:
                        print(f"[Graph] 相机图像处理失败: {exc}")
                    percept["payload"]["path"] = image_path
                    has_image = True
                    break
    for percept in percepts:
        if percept.get("event_id") not in (None,measurement_context["event_id"]):
            raise ValueError("感知对象来自其他测量事件")
        percept["event_id"] = measurement_context["event_id"]
    measurement_context = bind_percepts(measurement_context,percepts)

    # 图片预理解：结果存 state.vision_pre，供 retrieval/vision 两分支共享，避免重复调用
    vision_pre = ""
    if has_image:
        try:
            from tools.multimodal_tools import inspect_image
            vision_pre = inspect_image(image_path)
        except Exception as e:
            print(f"[Graph] 图片预理解失败: {e}")

    _status(config, "perceive", "done")
    note = f"{len(percepts)} 项感知" + ("，含图像" if has_image else "")
    return {
        "percepts": percepts,
        "percept_summary": summary_txt,
        "vision_pre": vision_pre,
        "has_image": has_image,
        "image_path": image_path,
        "measurement_context": measurement_context,
        "trace": _trace("perceive", t0, note),
    }


# ── 节点：调度器 ────────────────────────────────────────

def node_supervisor(state: AgentState, config=None) -> dict:
    """意图分析 → 决定本次调度哪些并行分支

    P1-12 改造：原实现用**中文关键词 in 判断**做分支选择（9 处 any(...)），
    直接导致两个线上故障：
      * "调用摄像头拍一张"在抓帧失败后，因 camera_requested=True 跳过了
        compute 分支，最终落到裸 LLM 兜底，模型拒答"我没有权限调用摄像头"；
      * 概念问题命中"不确定度"而进入 compute，用凭空默认值算了一遍，
        白等 11.8 秒。
    现改为消费 agent_router 的结构化决策：意图是枚举 + schema 校验+确定性
    守卫；need_camera/need_sensor 由模型显式给出，并与**真实设备可用性求交**
    （agent_router.resolve_plan），模型的意愿不再能覆盖硬件事实。
    """
    t0 = time.time()
    _status(config, "supervisor", "active")

    query = state.get("query", "") or ""
    has_image = bool(state.get("has_image"))

    # ── 1. 结构化路由：一次调用得到意图 + 改写 + 工具清单 ──
    decision = {}
    try:
        from agent_router import route as _route
        decision = _route(
            query,
            history=state.get("history") or [],
            has_image=has_image,
            percept_summary=state.get("percept_summary") or "",
        )
    except Exception as e:
        print(f"[Graph] 结构化路由失败({type(e).__name__}: {str(e)[:100]})，降级关键词")
        try:
            from agent_router import keyword_route as _kw
            decision = _kw(query, has_image)
        except Exception:
            decision = {"intent": ["kb"], "tool_calls": [], "need_camera": False,
                        "need_sensor": False, "direct_answer": False,
                        "rewritten_query": ""}

    # Numeric measurement intent has a deterministic guard, independent of the LLM label.
    from tools.measurement_executor import infer_task
    task = infer_task(query)
    if task:
        name = "compliance_judge" if task == "product_conformity" else task
        calls = list(decision.get("tool_calls") or [])
        if not any(call.get("name")==name for call in calls):
            calls.append({"name":name,"args":{},"why":"grounded measurement intent"})
        decision={**decision,"direct_answer":False,"tool_calls":calls,
                  "intent":list(dict.fromkeys(list(decision.get("intent") or [])+["compute"]))}
    # 已采集快照是本请求的事实；调度阶段不得再次打开设备探测。
    from perception.measurement_context import bound_sensor
    camera_available = any(p.get("type")=="camera" for p in state.get("percepts") or [])
    sensor_available = bound_sensor(state.get("measurement_context")) is not None

    # ── 3. 决策 → 实际分支 + 可读说明 ──
    try:
        from agent_router import resolve_plan
        plan, notes = resolve_plan(decision, has_image=has_image,
                                   camera_available=camera_available,
                                   sensor_available=sensor_available)
    except Exception as e:
        print(f"[Graph] 计划解析失败({e})，退化为知识库兜底")
        plan, notes = ["recall", "retrieval"], [f"计划解析异常：{type(e).__name__}"]

    if "recall" not in plan:
        plan.insert(0, "recall")          # 记忆召回始终执行

    # ── 4. 指代消解：优先用路由给出的改写，省掉一次独立 LLM 调用 ──
    rewritten = str(decision.get("rewritten_query") or "").strip()
    if not rewritten and (state.get("history") or []):
        try:
            from coordinator import rewrite_query
            rewritten = rewrite_query(query, state["history"])
        except Exception as e:
            print(f"[Graph] 查询改写失败: {e}")

    _status(config, "supervisor", "done")
    note = "调度: " + "+".join(plan)
    if decision.get("direct_answer"):
        note += " | 概念类问题（不调用计算工具）"
    if rewritten:
        note += f" | 改写: {rewritten[:40]}"
    if notes:
        note += " | " + "；".join(notes)[:80]

    return {
        "plan": plan,
        "rewritten_query": rewritten,
        "router_decision": decision,      # 新增：供 merge/judge 参考
        "router_notes": notes,            # 新增：降级原因，前端可展示
        "trace": _trace("supervisor", t0, note),
    }


# ── 并行分支节点 ────────────────────────────────────────

def node_recall(state: AgentState, config=None) -> dict:
    """记忆召回：相似历史案例"""
    t0 = time.time()
    if "recall" not in (state.get("plan") or []):
        return {"trace": _trace("recall", t0, "未调度，跳过")}
    _status(config, "recall", "active")
    mems, text = [], ""
    try:
        from memory.recall import recall, format_memories
        mems = recall(state.get("query", ""), top_k=3)
        text = format_memories(mems)
    except Exception as e:
        print(f"[Graph] 记忆召回失败: {e}")
    _status(config, "recall", "done")
    return {"memories": mems, "memory_text": text, "trace": _trace("recall", t0, f"{len(mems)} 条")}


def node_retrieval(state: AgentState, config=None) -> dict:
    """规元：知识库标准检索（复用 coordinator 实现）

    多轮时用改写后的独立问题检索（supervisor 已做指代消解）。
    """
    t0 = time.time()
    if "retrieval" not in (state.get("plan") or []):
        return {"trace": _trace("retrieval", t0, "未调度，跳过")}
    search_query = state.get("rewritten_query") or state.get("query", "")
    try:
        from coordinator import _run_guiyuan
        out = _run_guiyuan(state.get("query", ""),
                           on_status=_make_status_cb(config),
                           context=_context(state),
                           search_query=search_query or None)
    except Exception as e:
        out = f"【规元】执行异常: {e}"
    _audit_branch("retrieval", t0, True)
    return {"kb_result": out, "trace": _trace("retrieval", t0)}


def node_vision(state: AgentState, config=None) -> dict:
    """度元：视觉理解（复用 coordinator 实现）

    图像来源二选一：用户上传图 > 感知总线相机帧（"调用摄像头"场景）。
    """
    t0 = time.time()
    if "vision" not in (state.get("plan") or []):
        return {"trace": _trace("vision", t0, "未调度，跳过")}

    img = state.get("image_path")
    if not img:
        for p in state.get("percepts") or []:
            if p.get("type") == "camera":
                img = p.get("payload", {}).get("path")
                break
    if not img or not os.path.exists(img):
        return {"trace": _trace("vision", t0, "无图像输入，跳过")}

    try:
        from coordinator import _run_duyuan_image
        out = _run_duyuan_image(state.get("query", ""),
                                img,
                                on_status=_make_status_cb(config),
                                context=_context(state))
    except Exception as e:
        out = f"【度元·视觉】执行异常: {e}"
    _audit_branch("vision", t0, True)
    return {"vision_result": out, "trace": _trace("vision", t0)}


def node_compute(state: AgentState, config=None) -> dict:
    """度元：计算/检测（无图路径）"""
    t0 = time.time()
    if "compute" not in (state.get("plan") or []):
        return {"trace": _trace("compute", t0, "未调度，跳过")}
    try:
        from coordinator import _run_duyuan
        out = _run_duyuan(state.get("query", ""),
                          on_status=_make_status_cb(config),
                          context=_context(state),measurement_context=state.get("measurement_context"),
                          router_decision=state.get("router_decision"))
    except Exception as e:
        out = f"【度元】执行异常: {e}"
    _audit_branch("compute", t0, True)
    return {"compute_result": out, "trace": _trace("compute", t0)}


def node_plan_execute(state: AgentState, config=None) -> dict:
    """Plan-Execute 分支：LLM 规划 + 逐步真实执行（复用 task_planner）"""
    t0 = time.time()
    if "plan_execute" not in (state.get("plan") or []):
        return {"trace": _trace("plan_execute", t0, "未调度，跳过")}
    try:
        from tools.task_planner import plan_measurement
        out = plan_measurement(state.get("query", ""),measurement_context=state.get("measurement_context"))
    except Exception as e:
        out = f"【规划执行】异常: {e}"
    _audit_branch("plan_execute", t0, True)
    return {"plan_result": out, "trace": _trace("plan_execute", t0)}


def node_web(state: AgentState, config=None) -> dict:
    """索元：联网检索（复用 coordinator 实现）"""
    t0 = time.time()
    if "web" not in (state.get("plan") or []):
        return {"trace": _trace("web", t0, "未调度，跳过")}
    try:
        from coordinator import _run_suoyuan
        out = _run_suoyuan(state.get("query", ""),
                           on_status=_make_status_cb(config),
                           context=_context(state))
    except Exception as e:
        out = f"【索元】执行异常: {e}"
    _audit_branch("web", t0, True)
    return {"web_result": out, "trace": _trace("web", t0)}


# ── 汇聚节点 ────────────────────────────────────────────

def node_judge(state: AgentState, config=None) -> dict:
    """合规判定汇总：收集含结构化卡片的分支结果"""
    t0 = time.time()
    _status(config, "judge", "active")
    cards = []
    for key in ("vision_result", "compute_result"):
        v = state.get(key) or ""
        if "card" in v:
            cards.append(v)
    _status(config, "judge", "done")
    return {"judge_result": "\n".join(cards), "trace": _trace("judge", t0, f"{len(cards)} 张卡片")}


def node_merge(state: AgentState, config=None) -> dict:
    """结果融合：复用 coordinator 的融合与依据收集逻辑，并附加记忆一致性提示"""
    t0 = time.time()
    _status(config, "merge", "active")

    query = state.get("query", "")
    has_image = bool(state.get("has_image"))

    results = {}
    calculation_parts=[state.get(key) for key in ("plan_result","vision_result","compute_result") if state.get(key)]
    if calculation_parts:
        results["duyuan"] = "\n\n".join(calculation_parts)
    if state.get("kb_result"):
        results["guiyuan"] = state["kb_result"]         # plan_execute 已内含检索，避免重复
    if state.get("web_result"):
        results["suoyuan"] = state["web_result"]

    sources = []
    try:
        from coordinator import _llm_merge, _collect_sources, SUB_AGENTS
        agents = list(results.keys())
        if not agents:
            final = "本次未能产生有效分析结果。"
        elif any("```compliance-card" in value for value in results.values()):
            final = "\n\n".join(results.values())  # authoritative numeric/error results are not rewritten by an LLM
        elif len(agents) == 1:
            only = agents[0]
            raw = results[only] or ""
            if raw.lstrip().startswith("```"):
                final = raw                                   # 保留结构化卡片原样
            else:
                final = raw if raw.startswith("【") else f"【{SUB_AGENTS[only]['name']}】\n{raw}"
        else:
            final = _llm_merge(query, results, agents, has_image=has_image)
        sources = _collect_sources(results)
    except Exception as e:
        final = "\n\n".join(v for v in results.values() if v) or f"融合失败: {e}"

    mems = state.get("memories") or []
    if mems:
        final = f"> 🧠 命中 {len(mems)} 条历史相似案例（一致性可比对）\n\n" + final
    if sources:
        final += "\n\n---\n📎 本次知识库检索依据：" + "；".join(sources[:6])

    # ── P1-18：把卡片提升为 state 一等字段 ──
    # 从各分支结果里抽出结构化卡片放进 state.cards（LangGraph list 字段），
    # final 只保留自然语言 + 卡片文本（文本是为了前端按 marked 的
    # code.language-xxx-card 选择器渲染，保持兼容）。
    # 这样服务端内部逻辑（记忆沉淀、依据收集、后续节点）不必再靠正则
    # 从文本里抠卡片，也避免"忘了剥卡片导致记忆污染"。
    all_cards = []
    try:
        from tools.cards import extract_cards
        for _v in results.values():
            _c, _ = extract_cards(_v or "")
            all_cards.extend(_c)
        # judge 节点汇总的卡片也一并纳入（去重按 kind+raw）
        _jc, _ = extract_cards(state.get("judge_result") or "")
        _seen = {(c["kind"], c["raw"]) for c in all_cards}
        for c in _jc:
            if (c["kind"], c["raw"]) not in _seen:
                all_cards.append(c)
    except Exception as e:
        print(f"[Graph] 卡片提取失败(不影响主流程): {type(e).__name__}: {e}")

    _status(config, "merge", "done")
    return {
        "final": final,
        "sources": sources,
        "cards": all_cards,
        "trace": _trace("merge", t0, f"{len(final)} 字｜{len(all_cards)} 张卡片"),
    }
