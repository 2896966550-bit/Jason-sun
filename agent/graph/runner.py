"""LangGraph 引擎对外入口

run()        一次性执行（invoke），返回最终回答字符串
stream_run() 流式执行（生成器），逐事件产出 agent_status / partial / final

- 签名与 agent_core.chat 兼容（供 EQUIS_ENGINE=graph 时切换调用）
- thread_id 用于 checkpoint 断点续跑；同一会话传相同 thread_id 可复用检查点
- 每次执行后自动沉淀长期记忆（下次同类问题可召回）
- history: 近几轮 Q/A（修多轮失忆 bug：graph 引擎此前每轮从零开始）
"""
import os, sys, re, json, time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from graph.build import get_graph

# P1-18：卡片解析收敛到 tools/cards.py 的单一实现
from tools.cards import strip_cards as _strip_cards


def _release_temporary_thread(graph, thread_id):
    """Discard single-request checkpoints; explicit caller sessions are retained."""
    checkpointer = getattr(graph, "checkpointer", None)
    delete = getattr(checkpointer, "delete_thread", None)
    if callable(delete):
        try:
            delete(thread_id)
        except Exception as error:
            print(f"[Graph] 临时检查点释放失败: {type(error).__name__}")


def _extract_object(final_state: dict) -> str:
    """从视觉预理解结果中提取工件类型"""
    raw = (final_state or {}).get("vision_pre") or ""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
        obj = str(data.get("object") or "")
        return "" if obj.startswith("scene:") else obj
    except Exception:
        return ""


def _persist_memory(final_state: dict, query: str, image_path: str) -> None:
    """长期记忆沉淀（run / stream_run 共用）"""
    try:
        from memory.store import remember
        raw_final = (final_state or {}).get("final") or ""
        # 剥掉 merge 节点加的"命中历史案例"展示前缀，避免记忆嵌套污染（命中数越滚越虚）
        raw_final = re.sub(r"^>?\s*🧠\s*命中\s*\d+\s*条历史相似案例（一致性可比对）\s*\n*", "", raw_final)
        _ok = remember(
            query=query,
            conclusion=_strip_cards(raw_final),
            sources=(final_state or {}).get("sources") or [],
            object_name=_extract_object(final_state),
            image=os.path.basename(image_path) if image_path else "",
        )
        # P5-44：记忆写入留痕（沉淀了什么、是否去重更新）
        try:
            from compliance.gate import audit_log
            audit_log("memory_write", f"{'ok' if _ok else 'skip'}|q={str(query)[:100]}")
        except Exception:
            pass
    except Exception as e:
        print(f"[Graph] 记忆沉淀失败: {e}")
        try:
            from compliance.gate import audit_log
            audit_log("error", f"memory_persist|{type(e).__name__}: {str(e)[:120]}")
        except Exception:
            pass


def _make_init(query: str, image_path: str, history: list, measurement_context=None) -> dict:
    from perception.measurement_context import new_context
    return {
        "query": query,
        "image_path": image_path or None,
        "has_image": bool(image_path and os.path.exists(image_path)),
        "history": history or [],
        "measurement_context": new_context(measurement_context,query=query),
        "percepts": [], "vision_pre": "", "percept_summary": "",
        "kb_result":"", "vision_result":"", "compute_result":"",
        "plan_result":"", "web_result":"", "judge_result":"", "final":"",
    }


# ── 一次性执行 ──────────────────────────────────────────

def run(query: str, image_path: str = None, on_status=None, thread_id: str = None,
        history: list = None, measurement_context=None) -> str:
    """执行一次图推理并返回最终回答"""
    if not query and not image_path:
        return "请输入问题或上传图片。"
    if not query:
        query = "[上传图片]"

    # C3：图**构建**失败也要留痕。
    # 原先 `graph = get_graph()` 在 try 之外，构建期异常会直接抛给调用方，
    # 审计里什么都看不到（实测注入异常时确实如此）。
    try:
        graph = get_graph()
    except Exception as e:
        err = f"[Graph 构建异常] {type(e).__name__}: {str(e)[:200]}"
        print(err)
        try:
            from compliance.gate import audit_log
            audit_log("error", "graph_build|%s: %s" % (type(e).__name__, str(e)[:160]))
        except Exception:
            pass
        return err

    temporary_thread = thread_id is None
    if temporary_thread:
        thread_id = "t_" + uuid.uuid4().hex

    config = {
        "configurable": {
            "thread_id": thread_id,
            "on_status": on_status,      # 节点内通过 config 取用，不入 state
        },
        "recursion_limit": 30,
    }
    init = _make_init(query, image_path, history, measurement_context)

    from observability import trace
    run_id = trace.start_run("graph", query, image_path or "")
    t0 = time.time()
    ok = True
    try:
        final_state = graph.invoke(init, config=config)
    except Exception as e:
        ok = False
        trace.end_run(run_id, int((time.time() - t0) * 1000), ok)
        err = f"[Graph 引擎异常] {type(e).__name__}: {str(e)[:200]}"
        print(err)
        # C3：图执行异常必须留痕。
        # 此前 EVENT_TYPES 里登记了 error，但生产路径从未触发过 ——
        # 审计只记成功、不记失败，等于"只报喜不报忧"，
        # 出问题时无法从审计里看出这一轮到底崩在哪。
        try:
            from compliance.gate import audit_log
            audit_log("error", "graph_invoke|%s: %s" % (type(e).__name__, str(e)[:160]))
        except Exception:
            pass
        return err
    finally:
        if temporary_thread:
            _release_temporary_thread(graph, thread_id)
        print(f"[Graph] 执行完成，耗时 {time.time() - t0:.1f}s")
    trace.end_run(run_id, int((time.time() - t0) * 1000), ok)

    final = (final_state.get("final") or "").strip()
    from tools.cards import attach_run_metadata
    final = attach_run_metadata(final,run_id,(final_state.get("measurement_context") or {}).get("event_id"))
    if not final:
        final = "分析完成但未产生结果，请重试或换一种问法。"

    _persist_memory(final_state, query, image_path)
    return final


# ── 流式执行（生成器）────────────────────────────────────

def stream_run(query: str, image_path: str = None, thread_id: str = None,
               history: list = None, measurement_context=None):
    """流式执行图推理（生成器）。

    逐事件 yield：
      {"agent_status": {"agent":..., "state":...}}   节点/子步骤状态
      {"partial": {"node":..., "text":...}}          分支一完成即推送初步结论（治"等20s"硬伤）
      {"final": "..."}                               最终回答（最后一个事件）

    使用 graph.stream(values 模式)：每个 super-step 产出全量状态，
    与上一帧 diff 出新增分支结果。on_status 回调经内部队列转事件流。
    """
    import queue

    if not query and not image_path:
        yield {"final": "请输入问题或上传图片。"}
        return
    if not query:
        query = "[上传图片]"

    try:
        graph = get_graph()
    except Exception as error:
        try:
            from compliance.gate import audit_log
            audit_log("error", f"graph_build|{type(error).__name__}: {str(error)[:160]}")
        except Exception:
            pass
        yield {"final": f"[Graph 构建异常] {type(error).__name__}: {str(error)[:200]}", "error": True}
        return
    temporary_thread = not thread_id
    thread_id = thread_id or "t_" + uuid.uuid4().hex

    q = queue.Queue()

    def on_status(agent_id, state_str):
        q.put({"agent_status": {"agent": agent_id, "state": state_str}})

    config = {
        "configurable": {"thread_id": thread_id, "on_status": on_status},
        "recursion_limit": 30,
    }
    init = _make_init(query, image_path, history, measurement_context)

    from observability import trace
    run_id = trace.start_run("graph", query, image_path or "")
    t0 = time.time()
    ok = True
    last = {}
    snapshots = None
    try:
        snapshots = graph.stream(init, config, stream_mode="values")
        for snap in snapshots:
            # 1) 冲刷状态队列（节点内部回调的状态）
            while True:
                try:
                    yield q.get_nowait()
                except queue.Empty:
                    break
            # 2) 分支结果增量 → partial 事件（先到先显示）
            for key, node in (("vision_result", "vision"),
                              ("kb_result", "retrieval"),
                              ("plan_result", "plan_execute"),
                              ("compute_result", "compute"),
                              ("web_result", "web")):
                v = snap.get(key)
                if v and last.get(key) != v:
                    yield {"partial": {"node": node, "text": str(v)[:500]}}
            last = snap
    except GeneratorExit:
        ok = False
        raise
    except Exception as e:
        ok = False
        # C3：流式路径的异常同样要留痕（与 invoke 路径保持一致）
        try:
            from compliance.gate import audit_log
            audit_log("error", "graph_stream|%s: %s" % (type(e).__name__, str(e)[:160]))
        except Exception:
            pass
        # 异常时也带上 run_id：trace.db 里已留下这次运行的记录，
        # 前端/排障可直接 GET /api/trace/<run_id> 看卡在哪个节点。
        yield {"final": f"[Graph 引擎异常] {type(e).__name__}: {str(e)[:200]}",
               "run_id": run_id, "error": True}
        return  # Never persist a failed/partial run or emit a second success final.
    finally:
        close = getattr(snapshots, "close", None)
        if callable(close):
            try:
                close()
            except Exception as error:
                print(f"[Graph] 流关闭失败: {type(error).__name__}")
        if temporary_thread:
            _release_temporary_thread(graph, thread_id)
        trace.end_run(run_id, int((time.time() - t0) * 1000), ok)
        print(f"[Graph][stream] 执行完成，耗时 {time.time() - t0:.1f}s")

    # 3) 冲刷残余状态
    while True:
        try:
            yield q.get_nowait()
        except queue.Empty:
            break

    final = (last.get("final") or "").strip()
    from tools.cards import attach_run_metadata
    final = attach_run_metadata(final,run_id,(last.get("measurement_context") or {}).get("event_id"))
    if not final:
        final = "分析完成但未产生结果，请重试或换一种问法。"

    _persist_memory(last, query, image_path)
    # 随 final 事件下发 run_id 与逐节点轨迹：前端 inspector 的"运行时间轴"
    # 依赖它去拉 /api/trace/<id> 做真实耗时校准（此前只 yield {"final": ...}，
    # 前端只能靠本地计时估算）。trace 数据本就在内存中，顺带下发零成本。
    yield {
        "final": final,
        "run_id": run_id,
        "trace": (last or {}).get("trace") or [],
        "plan": (last or {}).get("plan") or [],
        "router_decision": (last or {}).get("router_decision") or {},
        "router_notes": (last or {}).get("router_notes") or [],
        "sources": (last or {}).get("sources") or [],
        "measurement_context": (last or {}).get("measurement_context") or {},
    }
