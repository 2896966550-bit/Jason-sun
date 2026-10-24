"""长期记忆召回 —— 相似历史案例检索

在 Agent 处理新问题前调用，把"以前遇到过的同类情况"注入上下文，
使判定结论具备一致性（同类工件不应给出矛盾结论）。
"""
import os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.kb_tools import _embed_query
from memory.store import _collection, count


def recall(query: str, top_k: int = 3, min_sim: float = 0.60,
           half_life_days: float = 30.0, min_score: float = 0.55) -> list:
    """召回与当前问题相似的历史案例。

    P1-17 修复（相关性门槛 + 时间衰减 + 候选扩充）：

    原实现的问题（有线上证据）：固定 min_sim=0.45、无时间衰减、无重排，
    于是**无关案例也会被召回并显示**。实测 chat_history.db：
      * 一个"合规判定"问题命中了 3 条历史案例，但库里根本没有合规判定的
        成功案例（那两次判定都失败了，回答的是"缺少工件公差信息"）；
      * 记忆库只有 29 条，就已经在污染上下文。
    这些前缀（"> 🧠 命中 N 条历史相似案例"）对用户是减分项而非加分项。

    现在：
      1) 候选先多取（top_k*3）再重排，避免"取 3 条里恰好都不相关"；
      2) 综合分 = 相似度 × 时间衰减（半衰期 half_life_days 天）；
      3) 双重门槛：原始相似度 >= min_sim 且 综合分 >= min_score；
      4) 返回按综合分降序，并保留 raw_similarity 便于排查。

    Args:
        query:          当前问题
        top_k:          最终返回条数
        min_sim:        原始相似度下限（0.45 提高到 0.60）
        half_life_days: 时间衰减半衰期（天）
        min_score:      综合分下限
    """
    if not query:
        return []
    if count() == 0:
        return []
    col = _collection()
    if col is None:
        return []

    vec = _embed_query(query)
    if vec is None:
        return []

    n_cand = max(top_k * 3, 6)
    try:
        res = col.query(query_embeddings=[vec], n_results=n_cand)
    except Exception as e:
        print(f"[Memory] 召回失败: {e}")
        return []

    ids = (res.get("ids") or [[]])[0]
    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]

    now = time.time()
    scored = []
    for _id, doc, meta, dist in zip(ids, docs, metas, dists):
        if not isinstance(meta, dict):
            meta = {}
        sim = max(0.0, 1 - dist / 2)
        if sim < min_sim:
            continue
        ts = meta.get("ts", 0) or 0
        try:
            age_days = max(0.0, (now - float(ts)) / 86400.0) if ts else 0.0
        except (TypeError, ValueError):
            age_days = 0.0
        decay = 0.5 ** (age_days / half_life_days) if half_life_days > 0 else 1.0
        score = sim * decay
        if score < min_score:
            continue
        scored.append({
            "similarity": round(score, 3),           # 综合分（对外展示用）
            "raw_similarity": round(sim, 3),         # 原始余弦相似度
            "decay": round(decay, 3),
            "age_days": round(age_days, 1),
            "hits": int(meta.get("hits", 1) or 1),
            "query": str(meta.get("query", ""))[:120],
            "object": str(meta.get("object", ""))[:40],
            "sources": str(meta.get("sources", ""))[:200],
            "ts": ts,
            "excerpt": str(doc or "")[:200],
        })

    scored.sort(key=lambda m: m["similarity"], reverse=True)
    return scored[:top_k]


def format_memories(mems: list) -> str:
    """把召回结果格式化为可注入提示词的文本"""
    if not mems:
        return ""
    lines = ["【历史相似案例（来自本系统长期记忆）】"]
    for i, m in enumerate(mems, 1):
        obj = f"｜工件: {m['object']}" if m.get("object") else ""
        src = f"｜依据: {m['sources']}" if m.get("sources") else ""
        lines.append(f"{i}. 问：{m['query']}{obj}{src}（相似度 {m['similarity']:.0%}）")
    lines.append("（若本次结论与历史案例不一致，请在回答中说明原因）")
    return "\n".join(lines)
