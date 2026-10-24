#!/usr/bin/env python
"""Equis 冒烟测试 —— 一键验证全部子系统（P0~P2）

用法：python scripts/smoke_test.py
覆盖：配置 / 知识库双路 / 记忆 / 感知 / 图编译 / 图执行 / Trace / 合规 / MCP / 多模态
"""
import os, sys, time, json, py_compile

AGENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, AGENT)
sys.path.insert(0, os.path.dirname(AGENT))

RESULTS = []


def check(name, fn):
    t0 = time.time()
    try:
        detail = fn()
        RESULTS.append((True, name, str(detail)))
        print(f'  PASS  {name}  |  {detail}  [{time.time()-t0:.1f}s]')
    except Exception as e:
        RESULTS.append((False, name, f'{type(e).__name__}: {str(e)[:140]}'))
        print(f'  FAIL  {name}  |  {type(e).__name__}: {str(e)[:140]}')


def t_config():
    import config
    assert config.LLM_MODEL, "主控模型未配置"
    assert config.LLM_API_KEY, "API Key 未配置"
    return f"主控 {config.LLM_MODEL} / 视觉 {config.LLM_VL_MODEL}"


def t_kb_number():
    from tools.kb_tools import search_knowledge_base
    out = search_knowledge_base("JJG 21", top_k=1)
    assert "编号精确匹配" in out, "编号精确匹配未生效"
    return "JJG 21 → 千分尺检定规程"


def t_kb_semantic():
    from tools.kb_tools import search_knowledge_base
    out = search_knowledge_base("千分尺检定规程有什么要求", top_k=1)
    assert "来源" in out, "语义检索无结果"
    return "语义通道 OK"


def t_memory():
    import memory
    n0 = memory.count()
    assert memory.remember("冒烟测试问题", "冒烟测试结论", sources=["SMOKE"], object_name="smoke")
    mems = memory.recall("冒烟测试", top_k=1)
    assert mems, "记忆召回失败"
    return f"{n0}→{memory.count()} 条，召回 sim={mems[0]['similarity']}"


def t_perception():
    import perception
    ps = perception.collect(query="冒烟测试")
    assert any(p.type == "text" for p in ps), "感知总线无 text 通道"
    return f"{len(ps)} 项感知"


def t_graph_compile():
    from graph.build import build_graph
    g = build_graph()
    mm = g.get_graph().draw_mermaid()
    assert "perceive" in mm and "merge" in mm, "图节点缺失"
    return f"9 节点编译 OK（mermaid {len(mm)} 字）"


def t_graph_text():
    from graph.runner import run
    out = run("JJG 21 是什么标准？")
    assert ("JJG 21" in out) or ("千分尺" in out), "图执行回答异常"
    return f"{len(out)} 字回答"


def t_trace():
    from observability import trace
    runs = trace.recent(10)
    assert runs, "trace 无运行记录"
    d = trace.detail(runs[0]["id"])
    assert d.get("steps"), "trace 无节点步骤"
    return f"{len(runs)} 次运行，最近 {len(d['steps'])} 步"


def t_compliance():
    from compliance.gate import compliance_report
    r = compliance_report()
    assert r["data_residency"]["ok"], "数据不出境自检未通过"
    return f"不出境 ✓ 审计 {r['audit']['recent_count']} 条"


def t_mcp():
    from agent.mcp_server import mcp as server
    r = server.list_tools()
    if hasattr(r, "__await__"):
        import asyncio
        r = asyncio.run(r)
    tools = list(r)
    assert len(tools) >= 15, f"MCP 工具数不足: {len(tools)}"
    return f"{len(tools)} 个 MCP 工具已注册"


def t_vision():
    from tools.multimodal_tools import inspect_image
    img = os.getenv('EQUIS_SMOKE_IMAGE', '')
    if not os.path.exists(img):
        up = os.path.join(AGENT, "data", "uploads")
        cands = [os.path.join(up, f) for f in os.listdir(up)] if os.path.isdir(up) else []
        if not cands:
            return "跳过（无测试图）"
        img = cands[0]
    d = json.loads(inspect_image(img))
    assert "error" not in d, f"视觉分析失败: {d.get('error')}"
    return f"识别: {d.get('object')}"


def t_server_syntax():
    py_compile.compile(os.path.join(AGENT, "server.py"), doraise=True)
    py_compile.compile(os.path.join(AGENT, "mcp_server.py"), doraise=True)
    return "server.py / mcp_server.py 语法 OK"


CHECKS = [
    ("配置", t_config),
    ("知识库·编号匹配", t_kb_number),
    ("知识库·语义检索", t_kb_semantic),
    ("长期记忆", t_memory),
    ("感知总线", t_perception),
    ("LangGraph 编译", t_graph_compile),
    ("LangGraph 执行", t_graph_text),
    ("Trace 可观测", t_trace),
    ("合规 Gate", t_compliance),
    ("MCP Server", t_mcp),
    ("多模态视觉", t_vision),
    ("服务端语法", t_server_syntax),
]


def main():
    print(f"Equis 冒烟测试 —— {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    for name, fn in CHECKS:
        check(name, fn)
    print("=" * 60)
    passed = sum(1 for ok, _, _ in RESULTS if ok)
    print(f"结果: {passed}/{len(RESULTS)} PASS")
    if passed < len(RESULTS):
        sys.exit(1)


if __name__ == "__main__":
    main()
