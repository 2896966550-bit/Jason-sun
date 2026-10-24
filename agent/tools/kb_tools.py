"""
知识库查询工具 V2 —— 百炼 Embedding + ChromaDB 向量检索

v4 维护：PersistentClient 单例化，避免每次查询重建客户端

对应文档: v4 变更记录 #10 - KB 客户端单例
"""
import os, sys, re, threading
from collections import OrderedDict
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chromadb import PersistentClient
from dashscope import TextEmbedding
from config import DATA_DIR, LLM_API_KEY

DB_PATH = os.path.join(DATA_DIR, "chroma_db")
EMBED_MODEL = "text-embedding-v4"

_collection = None
_client = None
_client_lock = threading.Lock()

# 全量条目缓存（编号精确匹配用；115 条量级，全量载入成本极低）
_kb_all = None
_kb_all_lock = threading.Lock()

# 标准编号识别：JJG 21 / JJF 1059.1-2012 / GB/T 18779.1-2002 / ISO 10360 等
#
# P5-48 修正：原先前缀与数字之间只允许空白（\s*），于是 "JJG-56-2000"
# 这种**用连字符分隔前缀与序号**的写法只能截取到 "JJG"（数字没匹配上），
# 导致模糊兜底拿不到有效编号、无法命中库中已有的条目。
# 现允许前缀与序号之间出现空白或连字符（[-—–\s]*），覆盖：
#   JJG 21 / JJG21 / JJG-21 / JJG 21-2008 / JJG—21—2008 / GB/T 18779.1-2002
_STD_RE = re.compile(
    r'\b((?:JJF|JJG|GB\s*/\s*T|GB|ISO\s*/\s*IEC\s*Guide|ISO)[-—–\s]*\d+(?:\.\d+)?(?:[-—–]\d{2,4})?)',
    re.I,
)


def _norm_std(s: str) -> str:
    """编号归一化：仅去除空白，保留连字符与小数点。

    注意：不能去掉连字符——"JJG 21-2008" 若粘连为 "JJG212008"，
    会与防误配规则 `(?![0-9])` 冲突，导致 "JJG21" 匹配不上。
    """
    return re.sub(r'\s+', '', s).upper()


def _get_kb_client():
    """获取 ChromaDB PersistentClient 单例（线程安全 double-check）

    Returns:
        chromadb.PersistentClient 实例

    对应文档: v4 变更记录 #10 - KB 客户端单例
    """
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:  # double-check
            return _client
        _client = PersistentClient(path=DB_PATH)
        return _client


def _get_collection():
    """获取 ChromaDB collection 单例"""
    global _collection
    if _collection is not None:
        return _collection
    if not os.path.exists(DB_PATH):
        return None
    client = _get_kb_client()
    try:
        _collection = client.get_collection("metrology_kb")
    except Exception:
        return None
    return _collection


# ── 查询向量缓存（P1-16）──────────────────────────────────
# 实测背景：retrieval 节点中位耗时 5.4s，其中 embedding 一次网络往返约
# 1.5s，是 3 跳串行链路（embedding → Chroma → LLM 总结）的第一跳。
# 而 chat_history.db 显示 458 条消息里只有 92 个不同问题，
# 「JJG 21 是什么标准？」单独重复了 6 次（run 1/2/3/5/9/21）——
# 也就是说相同查询的 embedding 被反复重新计算。
# 另注：node_recall 与 node_retrieval 也会对同一 query 各算一次。
#
# 用带上限的 LRU 缓存，命中即省一次 HTTPS 往返。
_embed_cache = OrderedDict()
_embed_cache_max = int(os.getenv("EQUIS_EMBED_CACHE", "512"))
_embed_cache_lock = threading.Lock()
_embed_stats = {"hit": 0, "miss": 0, "error": 0}


def embed_cache_stats() -> dict:
    with _embed_cache_lock:
        tot = _embed_stats["hit"] + _embed_stats["miss"]
        return {
            "size": len(_embed_cache),
            "max": _embed_cache_max,
            "hit": _embed_stats["hit"],
            "miss": _embed_stats["miss"],
            "error": _embed_stats["error"],
            "hit_rate": round(_embed_stats["hit"] / tot, 3) if tot else 0.0,
        }


def clear_embed_cache() -> None:
    with _embed_cache_lock:
        _embed_cache.clear()


def _embed_query(text: str) -> list:
    """查询向量化（带 LRU 缓存；失败返回 None 且不缓存失败结果）

    缓存键为归一化后的查询文本（去首尾空白、内部空白折叠），
    避免"空格不同 → 缓存不命中"的无谓 miss。
    """
    if not text:
        return None
    key = re.sub(r"\s+", " ", str(text)).strip()[:6000]

    with _embed_cache_lock:
        hit = _embed_cache.get(key)
        if hit is not None:
            _embed_cache.move_to_end(key)
            _embed_stats["hit"] += 1
            return hit

    try:
        resp = TextEmbedding.call(
            api_key=LLM_API_KEY,
            model=EMBED_MODEL,
            input=key,
        )
    except Exception as e:
        with _embed_cache_lock:
            _embed_stats["error"] += 1
        print(f"[KB] Embedding 调用异常: {type(e).__name__}: {str(e)[:100]}")
        return None

    if resp.status_code != 200:
        with _embed_cache_lock:
            _embed_stats["error"] += 1
        print(f"[KB] Embedding 失败: HTTP {resp.status_code} {str(getattr(resp,'message',''))[:80]}")
        return None

    try:
        vec = resp.output["embeddings"][0]["embedding"]
    except Exception as e:
        with _embed_cache_lock:
            _embed_stats["error"] += 1
        print(f"[KB] Embedding 响应结构异常: {e}")
        return None

    if not vec:
        with _embed_cache_lock:
            _embed_stats["error"] += 1
        return None

    with _embed_cache_lock:
        _embed_stats["miss"] += 1
        _embed_cache[key] = vec
        _embed_cache.move_to_end(key)
        while len(_embed_cache) > _embed_cache_max:
            _embed_cache.popitem(last=False)     # LRU 淘汰，容量有界
    return vec


def _load_all(col):
    """载入全量条目（缓存），供编号精确匹配使用"""
    global _kb_all
    if _kb_all is not None:
        return _kb_all
    with _kb_all_lock:
        if _kb_all is not None:
            return _kb_all
        try:
            data = col.get()
            _kb_all = list(zip(data.get("ids", []), data.get("documents", []), data.get("metadatas", [])))
        except Exception as e:
            print(f"[KB] 全量载入失败: {e}")
            _kb_all = []
    return _kb_all


def _std_id_matches(query: str, col, return_level: bool = False):
    """标准编号精确匹配：返回命中条目的 [(id, doc, meta)]

    分三级：
      严格命中 —— 编号出现在条目标题(source)中，说明该条目就是这份标准；
      宽松命中 —— 编号仅出现在正文中（别的标准提及它），优先级低；
      模糊命中 —— P5-48 新增：编号在 source/doc 中**不连续出现**时
                  （标题写法有差异，如 "JJF1059.1" vs "JJF 1059.1-2012"、
                   或编号被括号/分隔符打断），做子串兜底匹配。

    为什么需要模糊兜底（P5-48）：
        线上实测出现过同一问题两次结论相反——
          #426 "JJG 21 是什么标准？" → 正确答出 JJG 21-2008
          #408 "JJG 21 是什么标准"  → 回答"依据现有知识库，未收录JJG 21"
        原因是编号通道一旦在 _norm_std + (?![0-9]) + 去年份回退这一串
        正则上脱靶就直接放弃，而语义通道对"JJG 21"这种标识符查询召回极差
        （1024 维 embedding 无法区分 JJG 21/99/124），两条通道同时失效。
        现在脱靶时降级为子串匹配，宁可多给几条相关条目也不轻易说"未收录"。

    Args:
        return_level: True 时返回 [(id, doc, meta, level)]，便于调用方标注命中方式

    为什么需要（原有理由）：纯向量检索对"JJG 21"这类编号查询召回很差——
    编号的语义嵌入弱，容易检索到同前缀的其他规程（JJG 99、JJG 124），
    从而出现"库中有该规程却回答未收录"的错误。
    """
    wanted = [_norm_std(m.group(1)) for m in _STD_RE.finditer(query)]
    wanted = [w for w in wanted if w]
    if not wanted:
        return []

    strict, loose, fuzzy = [], [], []
    strict_ids, loose_ids, fuzzy_ids = set(), set(), set()

    for doc_id, doc, meta in _load_all(col):
        src = str(meta.get("source", ""))
        src_norm = _norm_std(src)
        doc_norm = _norm_std(str(doc)[:300])
        for w in wanted:
            # 先用完整编号匹配；失败则退化为"去年份主体"（库中条目可能只存主体）
            candidates = [w]
            m = re.match(r'^(.*?)[-—–]\d{2,4}$', w)
            if m:
                candidates.append(m.group(1))

            level = None
            for cand in candidates:
                # 严格匹配：编号后不能再接数字（避免 JJG2 命中 JJG21）
                pat = re.escape(cand) + r'(?![0-9])'
                if re.search(pat, src_norm):
                    level = "strict"
                    break
                if re.search(pat, doc_norm):
                    level = "loose"
            if level is None:
                # P5-48 模糊兜底：把编号里的空白/连字符全部去掉后再找子串。
                # 这只在严格/宽松都失败时才用，因此不会让"引用者顶替本体"。
                flat = re.sub(r'[-—–]', '', w)
                src_flat = re.sub(r'[-—–]', '', src_norm)
                doc_flat = re.sub(r'[-—–]', '', doc_norm)
                if flat and flat in src_flat:
                    level = "fuzzy"
                elif flat and flat in doc_flat:
                    level = "fuzzy_doc"
            if level:
                if level == "strict" and doc_id not in strict_ids:
                    strict_ids.add(doc_id)
                    strict.append((doc_id, doc, meta, "strict") if return_level else (doc_id, doc, meta))
                elif level == "loose" and doc_id not in loose_ids and doc_id not in strict_ids:
                    loose_ids.add(doc_id)
                    loose.append((doc_id, doc, meta, "loose") if return_level else (doc_id, doc, meta))
                elif level.startswith("fuzzy") and doc_id not in fuzzy_ids \
                        and doc_id not in strict_ids and doc_id not in loose_ids:
                    fuzzy_ids.add(doc_id)
                    fuzzy.append((doc_id, doc, meta, "fuzzy") if return_level else (doc_id, doc, meta))
                break

    # 优先级：严格 > 宽松 > 模糊（严格命中存在时不再返回低级别，
    # 避免"引用者顶替本体"；全都为空时才逐级降级）
    if strict:
        return strict
    if loose:
        return loose
    return fuzzy


def search_knowledge_base(query: str, top_k: int = 3, min_sim: float = 0.35) -> str:
    """向量搜索计量标准知识库（双路召回）。

    通道 1：标准编号精确匹配（JJG 21 / JJF 1059.1-2012 等）
            含 P5-48 三级匹配：严格 → 宽松 → 模糊子串兜底
    通道 2：向量语义检索（自然语言问法）

    v4.1 维护：min_sim 相似度下限过滤——低于阈值视为不相关，避免误导 Agent。
    2026-09-12 修复：新增编号精确匹配通道（此前纯向量检索对编号查询召回差）。
    P5-48 修复：编号通道脱靶时降级为模糊子串匹配，并在输出中标注命中级别；
    同时把"未找到"的返回值改为**带上已尝试的检索词**，便于排障与二次提问。
    """
    col = _get_collection()
    if col is None:
        return "【提示】向量知识库尚未构建，请先运行 knowledge_base/build_kb_v2.py"

    lines = ["=== 知识库检索结果 ===\n"]
    found_srcs = []
    _LEVEL_CN = {"strict": "编号精确匹配", "loose": "编号出现在正文", "fuzzy": "编号模糊匹配"}

    # ── 通道 1：标准编号匹配（严格/宽松/模糊）──
    std_hits = _std_id_matches(query, col, return_level=True)
    for hit in std_hits:
        doc_id, doc, meta = hit[0], hit[1], hit[2]
        level = hit[3] if len(hit) > 3 else "strict"
        src = meta.get("source", "未知")
        if src in found_srcs:
            continue
        found_srcs.append(src)
        lines.append(f"【{len(found_srcs)}】来源: {src}  ({_LEVEL_CN.get(level, level)})")
        lines.append(str(doc)[:600].strip())
        lines.append("")

    # ── 通道 2：向量语义检索 ──
    q_vec = _embed_query(query)
    vec_hits = []          # [(doc_id, text, meta, sim)]
    if q_vec is not None:
        try:
            results = col.query(query_embeddings=[q_vec], n_results=max(top_k * 2, 6))
            if results.get("ids") and results["ids"][0]:
                for doc_id, dist, text, meta in zip(
                    results["ids"][0], results["distances"][0],
                    results["documents"][0], results["metadatas"][0],
                ):
                    sim = max(0, 1 - dist / 2)
                    if sim < min_sim:
                        continue
                    vec_hits.append((doc_id, text, meta, sim))
        except Exception as e:
            print(f"[KB] 向量检索失败: {e}")

    # ── 通道 3：BM25 关键词召回（P1-14）──
    # 与向量通道做 RRF 融合。BM25 对"编号 + 术语"类查询远强于 embedding，
    # 且不依赖网络（向量通道失败时它仍能兜住）。
    kw_hits = []           # [(doc_id, text, meta, bm25_score)]
    try:
        from tools import bm25 as _bm25
        idx = _bm25.get_index()
        if idx is None:
            items = _load_all(col)
            if items:
                # _load_all 返回 [(id, doc, meta)]
                idx = _bm25.build_index([(i, d, m) for i, d, m in items])
                print(f"[KB] BM25 索引已建立：{len(items)} 条文档")
        if idx is not None:
            _by_id = {i: (d, m) for i, d, m in _load_all(col)}
            for doc_id, score in idx.search(query, top_k=max(top_k * 2, 6)):
                if doc_id in _by_id:
                    d, m = _by_id[doc_id]
                    kw_hits.append((doc_id, d, m, score))
    except Exception as e:
        print(f"[KB] BM25 召回失败（不影响向量通道）: {type(e).__name__}: {e}")

    # ── RRF 融合：用名次而非原始分数，避免 BM25 分与余弦分不可比 ──
    fused_ids = []
    if kw_hits and vec_hits:
        try:
            from tools.bm25 import rrf_fuse
            fused_ids = [i for i, _ in rrf_fuse([
                [h[0] for h in kw_hits],
                [h[0] for h in vec_hits],
            ])]
            print(f"[KB] RRF 融合：BM25 {len(kw_hits)} 条 + 向量 {len(vec_hits)} 条 "
                  f"→ {len(fused_ids)} 条")
        except Exception as e:
            print(f"[KB] RRF 融合失败，退回依次追加: {e}")

    if fused_ids:
        _pool = {h[0]: h for h in (kw_hits + vec_hits)}
        ordered = [(_pool[i][0], _pool[i][1], _pool[i][2], _pool[i][3], "融合")
                   for i in fused_ids if i in _pool]
    else:
        # 未融合（只有一路可用）：向量优先，其后补 BM25 独有项
        _vids = {v[0] for v in vec_hits}
        ordered = [(d, t, m, s, "向量") for d, t, m, s in vec_hits]
        ordered += [(d, t, m, s, "关键词") for d, t, m, s in kw_hits if d not in _vids]

    # ── 相关性守门（缺陷 A 修复，由 P4-39 评测发现）────────────
    # 问题：BM25 通道**永远返回它的 top-N**，与查询是否相关无关；加上
    # `len(found_srcs) >= top_k + 2` 是唯一终止条件，只要 BM25 有输出，
    # found_srcs 就不可能为空 —— 于是下面的 `if not found_srcs:` 成了死代码，
    # 库外问题（"推荐几部科幻电影"）也会拿到格式规整、带"来源:"标注、
    # 看起来像权威依据的文本，把 RAG 的幻觉防线打穿（实测库外误召回 75%）。
    #
    # 修复思路：判据不能只看"词元重合数"——中文 bigram 会让 "怎么/什么/多少"
    # 这类高频词造成假重合。真正强相关的信号是**查询词元是否落在来源标题上**
    # （来源标题就是该条目的主题），因此以"来源标题重合"为主判据。
    try:
        from tools.bm25 import tokenize as _tok
        _q_tokens = set(_tok(query))
    except Exception:
        _q_tokens = set()

    # 单一汉字/高频虚词构成的 bigram 不具区分度，排除
    _STOP = {"什么", "怎么", "如何", "多少", "哪些", "可以", "怎么", "为什",
             "是不", "不是", "有没", "没有", "一下", "帮我", "请问", "谢谢"}

    def _title_overlap(meta: dict) -> int:
        """查询词元与**来源标题**的重合数（排除停用词）"""
        if not _q_tokens:
            return 0
        try:
            src_tokens = set(_tok(str(meta.get("source", ""))))
        except Exception:
            return 0
        return len((_q_tokens - _STOP) & src_tokens)

    _dropped = 0
    kept = []
    for item in ordered:
        if _title_overlap(item[2]) <= 0:
            _dropped += 1
            continue
        kept.append(item)
    if _dropped:
        print(f"[KB] 相关性守门：丢弃 {_dropped} 条与来源标题零重合的结果（避免编造依据）")
    ordered = kept

    for doc_id, text, meta, score, chan in ordered:
        src = meta.get("source", "未知")
        if src in found_srcs:
            continue
        found_srcs.append(src)
        # 向量分是余弦相似度（0~1）；BM25 分无上界，只作为相对排序信号，
        # 因此对关键词通道只标注通道名、不伪造百分比。
        _tag = f"相似度: {min(score, 1.0):.0%}" if chan == "向量" else \
               (f"关键词命中率: {min(score, 1.0):.0%}" if chan == "关键词" else "语义+关键词融合命中")
        lines.append(f"【{len(found_srcs)}】来源: {src}  ({_tag})")
        lines.append(str(text)[:500].strip())
        lines.append("")
        if len(found_srcs) >= top_k + 2:
            break

    if not found_srcs:
        # P5-48：把检索词与阈值一并回报。原先只回一句"未找到"，
        # 既无法排障，也容易让上层误判为"库里根本没有这份标准"。
        _w = ", ".join(m.group(1).strip() for m in _STD_RE.finditer(query)) or "（未识别到标准编号）"
        return (f"知识库中未找到相关内容（检索词：{query[:60]}；识别到的编号：{_w}；"
                f"相似度下限 {min_sim}）。请尝试换个问法，或直接给出标准编号。")
    return "\n".join(lines)


def list_kb_topics() -> str:
    """列出知识库主题"""
    col = _get_collection()
    if col is None:
        return "【提示】知识库尚未构建。"
    metas = col.get()["metadatas"]
    sources = sorted(set(m.get("source", "?") for m in metas))
    return f"知识库共 {len(sources)} 条记录，覆盖以下主题：\n" + "\n".join(f"  · {s}" for s in sources)


def add_to_knowledge_base(source: str, content: str) -> bool:
    """
    增量添加文档到向量知识库。
    自动向量化并写入 ChromaDB，下次查询即可命中。
    """
    global _collection, _kb_all

    if len(content.strip()) < 30:
        print("[KB_ADD] content too short, skipped")
        return False

    col = _get_collection()
    if col is None:
        client = PersistentClient(path=DB_PATH)
        try:
            col = client.get_or_create_collection("metrology_kb")
        except Exception as e:
            print(f"[KB_ADD] failed to get/create collection: {e}")
            return False

    # 生成唯一 ID
    try:
        existing = col.get()
        count = len(existing["ids"]) if existing and existing.get("ids") else 0
    except Exception:
        count = 0
    doc_id = f"auto_{count}"

    # 向量化
    vec = _embed_query(content)
    if vec is None:
        print(f"[KB_ADD] embedding failed for: {source[:40]}")
        return False

    # 写入
    try:
        col.add(
            ids=[doc_id],
            embeddings=[vec],
            documents=[content.strip()[:3000]],
            metadatas=[{"source": source}],
        )
        _collection = col
        _kb_all = None  # 失效全量缓存，下次检索重新载入
        # P1-14：BM25 索引跟随全量缓存一起失效，下次检索时重建
        try:
            from tools import bm25 as _bm25
            _bm25.invalidate()
        except Exception:
            pass
        print(f"[KB_ADD] SUCCESS: '{source[:40]}' ({len(content)} chars, id={doc_id})")
        return True
    except Exception as e:
        print(f"[KB_ADD] error: {e}")
        return False
