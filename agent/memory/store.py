"""长期记忆存储 —— 沉淀本系统的历史判定案例

与向量知识库(metrology_kb)职责分离：
  metrology_kb —— 国家计量标准条文（外部知识）
  agent_memory —— 本系统历次判定的案例（内部经验）

用途：相似案例召回（"上次同类工件是怎么判的、依据哪条规程"），
使 Agent 在多轮使用中积累经验，而非每次从零开始。
"""
import os, sys, time, threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.kb_tools import _embed_query, _get_kb_client

COLLECTION = "agent_memory"
_lock = threading.Lock()
_col = None


def _collection():
    """获取记忆 collection（复用知识库同一个 PersistentClient 实例）"""
    global _col
    if _col is not None:
        return _col
    with _lock:
        if _col is not None:
            return _col
        client = _get_kb_client()
        try:
            _col = client.get_collection(COLLECTION)
        except Exception:
            try:
                _col = client.get_or_create_collection(COLLECTION)
            except Exception as e:
                print(f"[Memory] collection 创建失败: {e}")
                return None
    return _col


def _norm_query(q: str) -> str:
    """查询归一化（用于去重比较）：去空白、去标点、小写化"""
    import re as _re
    s = _re.sub(r"\s+", "", str(q or ""))
    s = _re.sub(r"[，。！？、；：,.!?;:\"'（）()\[\]【】]", "", s)
    return s.lower()


def remember(query: str, conclusion: str, sources=None,
             object_name: str = "", image: str = "", dedupe: bool = True) -> bool:
    """写入一条历史记忆。

    query       用户问题原文
    conclusion  Agent 给出的结论（已剥离结构化卡片）
    sources     本次命中的标准编号列表
    object_name 识别到的工件类型（如有）
    image       关联图片文件名（如有）
    dedupe      P1-17：写入前按归一化问题查重（默认开启）

    P1-17 修复（写入去重）：
        原实现用 `f"mem_{int(time.time()*1000)}"` 做 ID，**无任何内容去重**。
        同一个问题问三次就会在库里留下三条几乎相同的记忆，
        使后续"命中 N 条历史相似案例"的 N 越滚越大而信息量不变
        （与 runner._persist_memory 里那个"防嵌套"补丁要解决的是同一类问题）。
        现按归一化问题 + 工件类型查重：命中则**更新**该条而非新增。
    """
    if not query or not conclusion:
        return False
    col = _collection()
    if col is None:
        return False

    text = f"{query}\n{object_name}\n{conclusion}"[:1500]
    vec = _embed_query(text)
    if vec is None:
        print("[Memory] 向量化失败，跳过写入")
        return False

    meta = {
        "query": str(query)[:200],
        "object": str(object_name)[:80],
        "sources": "；".join(str(s) for s in (sources or []))[:300],
        "image": str(image)[:120],
        "ts": time.time(),
    }

    # ── 查重：同一归一化问题 + 同一工件 → 更新既有记忆 ──
    if dedupe:
        try:
            data = col.get(include=["metadatas"])
            nq = _norm_query(query)
            for _id, m in zip(data.get("ids", []), data.get("metadatas", []) or []):
                if not isinstance(m, dict):
                    continue
                if _norm_query(m.get("query")) == nq and \
                   str(m.get("object", "")) == str(object_name)[:80]:
                    meta["ts"] = time.time()
                    meta["hits"] = int(m.get("hits", 1)) + 1
                    col.update(ids=[_id], embeddings=[vec], documents=[text], metadatas=[meta])
                    return True
        except Exception as e:
            print(f"[Memory] 去重查询失败（改为直接新增）: {type(e).__name__}: {str(e)[:80]}")

    doc_id = f"mem_{int(time.time() * 1000)}"
    meta.setdefault("hits", 1)
    try:
        col.add(ids=[doc_id], embeddings=[vec], documents=[text], metadatas=[meta])
        return True
    except Exception as e:
        print(f"[Memory] 写入失败: {e}")
        return False


def count() -> int:
    col = _collection()
    if col is None:
        return 0
    try:
        return col.count()
    except Exception:
        return 0


def recent(n: int = 5) -> list:
    """取最近 n 条记忆（按时间倒序）"""
    col = _collection()
    if col is None:
        return []
    try:
        data = col.get()
        items = list(zip(data.get("ids", []), data.get("documents", []), data.get("metadatas", [])))
    except Exception:
        return []
    items.sort(key=lambda x: x[2].get("ts", 0) if isinstance(x[2], dict) else 0, reverse=True)
    return [{"id": i, "document": d, "meta": m} for i, d, m in items[:n]]
