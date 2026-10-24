r"""BM25 关键词召回 + RRF 融合（P1-14）

────────────────────────────────────────────────────────────────
为什么需要
────────────────────────────────────────────────────────────────
双路召回里"编号精确匹配"是正则精确匹配、"向量语义检索"靠 embedding，
两者对**中文标识符类查询**都不稳：
  - "JJG 21" 这类编号的语义嵌入极弱（1024 维向量区分不了 JJG 21/99/124）；
  - 一旦编号正则脱靶，就只剩语义通道，召回可能完全跑偏。
线上实测出现过同一问题两次结论相反（#426 答出、#408 说未收录）。

BM25 是纯词频/逆文档频率统计，对"编号 + 关键词"这类查询天然强：
只要文档里出现 "JJG" 和 "21"，它就能给出很高的分。

────────────────────────────────────────────────────────────────
为什么自己实现而不用库
────────────────────────────────────────────────────────────────
  - Chroma 的 FTS5 表虽在磁盘上，但 1.5.9 没有公开的全文检索 API，
    官方也建议自行处理检索；
  - 引入 rank_bm25 / jieba 会新增依赖（项目要求尽量零新增依赖）；
  - 全库仅 118 条短文档（中位 193 字），纯 Python BM25 一次查询 <10ms，
    完全够用。

────────────────────────────────────────────────────────────────
中文分词策略
────────────────────────────────────────────────────────────────
不引入分词器，采用**字符 bigram + 英文/数字 token** 的混合切分：
  "JJG 21 千分尺" → ["jjg", "21", "千分", "分尺", "千分尺"?]
  - 英文与数字按整词（\w+）切出 → 保住 "JJG"、"21"、"2008"
  - 中文按相邻两字 bigram → 无需词典也能有不错的区分度
这对"编号 + 术语"混合查询最有效，且完全无依赖。
"""
import math
import os
import re
import sys
from collections import Counter
from typing import Dict, List, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 英文/数字整词
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[./\-][A-Za-z0-9]+)*")
# 中文字符
_HAN_RE = re.compile(r"[\u4e00-\u9fff]+")


def tokenize(text: str) -> List[str]:
    r"""轻量中英混合切分：英文/数字取整词，中文取字符 bigram（单字也保留）。

    关键设计：带连字符的编号（如 "21-2008"、"18779.1-2002"）**既保留整体
    也拆出各部分**。否则查询 "JJG 21" 无法命中库里的 "JJG 21-2008"
    ——实测这会让 JJG 99 排在 JJG 21 前面（因为两者都只匹配到 "jjg"）。

    例："JJG 21-2008 千分尺检定规程"
        -> ['jjg', '21-2008', '21', '2008', '千分', '分尺', '尺检', '检定',
            '定规', '规程']
    """
    if not text:
        return []
    t = str(text)
    toks: List[str] = []
    for m in _WORD_RE.finditer(t):
        w = m.group(0).lower()
        toks.append(w)
        # 编号类 token 额外拆出各数字段（"21-2008" -> "21", "2008"）
        if any(ch in w for ch in "-./"):
            for part in re.split(r"[-./]", w):
                if part and part != w:
                    toks.append(part)
    for m in _HAN_RE.finditer(t):
        s = m.group(0)
        if len(s) == 1:
            toks.append(s)
        else:
            for i in range(len(s) - 1):
                toks.append(s[i:i + 2])
    return toks


class BM25Index:
    """极简 BM25（Okapi）实现，支持增量重建。

    参数取常用默认：k1=1.5, b=0.75。
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.docs: List[Tuple[str, str, Dict]] = []   # (id, text, meta)
        self.tf: List[Counter] = []
        self.df: Counter = Counter()
        self.doc_len: List[int] = []
        self.avg_len: float = 0.0

    def build(self, items: Sequence[Tuple[str, str, Dict]]) -> None:
        """items: [(id, document_text, metadata)]"""
        self.docs = list(items)
        self.tf, self.df, self.doc_len = [], Counter(), []
        for _id, text, _meta in self.docs:
            toks = tokenize(text)
            c = Counter(toks)
            self.tf.append(c)
            self.doc_len.append(len(toks))
            for term in c:
                self.df[term] += 1
        n = len(self.docs)
        self.avg_len = (sum(self.doc_len) / n) if n else 0.0

    def search(self, query: str, top_k: int = 10) -> List[Tuple[str, float]]:
        """返回 [(doc_id, bm25_score)]，按分数降序（分数为 0 的不返回）"""
        if not self.docs:
            return []
        q_terms = tokenize(query)
        if not q_terms:
            return []
        n = len(self.docs)
        scores: Dict[int, float] = {}
        for term in set(q_terms):
            df = self.df.get(term, 0)
            if df == 0:
                continue
            # BM25 的 IDF（加 1 防止为负）
            idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            for i, c in enumerate(self.tf):
                f = c.get(term, 0)
                if not f:
                    continue
                dl = self.doc_len[i] or 1
                denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avg_len or 1))
                scores[i] = scores.get(i, 0.0) + idf * f * (self.k1 + 1) / denom
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        return [(self.docs[i][0], s) for i, s in ranked]


# ── 全局索引（跟随 kb_tools 的全量缓存一起失效）──

_index = None
_index_lock = None


def _get_lock():
    global _index_lock
    if _index_lock is None:
        import threading
        _index_lock = threading.Lock()
    return _index_lock


def build_index(items: Sequence[Tuple[str, str, Dict]]) -> BM25Index:
    global _index
    idx = BM25Index()
    idx.build(items)
    with _get_lock():
        _index = idx
    return idx


def get_index():
    return _index


def invalidate() -> None:
    """全量缓存失效时调用（新增文档后）"""
    global _index
    with _get_lock():
        _index = None


def search(query: str, top_k: int = 10):
    idx = get_index()
    if idx is None:
        return []
    return idx.search(query, top_k=top_k)


# ── RRF 融合 ──────────────────────────────────────────────

def rrf_fuse(rankings: List[List[str]], k: int = 60) -> List[Tuple[str, float]]:
    """Reciprocal Rank Fusion：把多路召回结果按**名次**融合。

    score(d) = Σ_r 1 / (k + rank_r(d))
    k 默认 60（RRF 原论文推荐值）。用名次而非原始分数，避免
    BM25 分数（几~几十）与余弦相似度（0~1）量纲不可比的问题。

    Args:
        rankings: 每一路召回的 doc_id 有序列表（越靠前越相关）
    Returns:
        [(doc_id, fused_score)] 按分数降序
    """
    agg: Dict[str, float] = {}
    for r, ids in enumerate(rankings):
        for rank, did in enumerate(ids):
            agg[did] = agg.get(did, 0.0) + 1.0 / (k + rank + 1)
    return sorted(agg.items(), key=lambda kv: kv[1], reverse=True)


if __name__ == "__main__":
    # 自测
    docs = [
        ("d1", "JJG 21-2008 千分尺检定规程 适用于测量范围 0-500mm 的千分尺检定", {"source": "JJG 21-2008"}),
        ("d2", "JJG 99-2022 砝码检定规程 砝码等级 E1 E2 F1", {"source": "JJG 99-2022"}),
        ("d3", "JJF 1059.1-2012 测量不确定度评定与表示 GUM 方法", {"source": "JJF 1059.1"}),
        ("d4", "GB/T 18779.1-2002 按规范检验合格或不合格的判定规则 三态判定", {"source": "GB/T 18779.1"}),
    ]
    idx = BM25Index()
    idx.build(docs)
    for q in ("JJG 21", "砝码", "不确定度评定", "三态判定", "JJG 99-2022"):
        r = idx.search(q, top_k=3)
        print("  %-14s -> %s" % (q, [(d, round(s, 3)) for d, s in r]))
    print()
    print("  RRF 融合示例:", rrf_fuse([["d1", "d2", "d3"], ["d3", "d1"]])[:3])
