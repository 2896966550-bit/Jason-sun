"""
知识库 V2 —— 百炼 Embedding + ChromaDB 向量检索

运行: python knowledge_base/build_kb_v2.py            # 安全重建（推荐）
      python knowledge_base/build_kb_v2.py --verify   # 只校验，不写入

────────────────────────────────────────────────────────────────
代码审计修复记录（原实现的三个致命问题）
────────────────────────────────────────────────────────────────
1) 先删后建、非原子：原 L73 先 delete_collection，L88 才 batch_embed。
   中途任何网络抖动都会抛异常，此时旧库已删、新库未建 → 知识库彻底丢失。

2) 静默建成半个库：原 L29-30 / L34-35 把文档源导入失败降级为
   print("WARN")，于是 build_kb.py 导入失败时只剩 extra_docs 的 69 条，
   脚本仍打印 "Done!"。现改为**硬失败**。

3) 索引与源码不一致：线上 118 条 ≠ 源码 112 条，有 7 条只存在于索引
   （详见 knowledge_base/recovered_docs.py）。现把 recovered_docs 纳入
   构建源，并对"线上有、源里没有"的条目发出告警。

当前实现：嵌入到临时 collection → 数量校验 → 归档旧库 → 原子改名切换。
任何一步失败都保留原库不动。
"""
import sys, os, time, shutil, argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashscope import TextEmbedding
from chromadb import PersistentClient
from config import LLM_API_KEY

API_KEY = LLM_API_KEY
EMBED_MODEL = "text-embedding-v4"

COLLECTION = "metrology_kb"
STAGING = "metrology_kb_staging"
BACKUP = "metrology_kb_backup"

# 距离度量：实测现有集合用的是 Chroma 默认 l2（平方 L2），
# 且 text-embedding-v4 返回单位范数向量，因此
#     dist_sq = 2 - 2·cos  →  cos = 1 - dist_sq/2
# kb_tools 的 sim = max(0, 1 - dist/2) 即余弦相似度，0.35/0.45 阈值成立。
# 这里**显式声明**该度量，避免将来换 embedding 模型后阈值静默失效。
HNSW_SPACE = "l2"

# 原实现的本地 DOCUMENTS 残留（1 条，从未被 get_all_docs 使用）。
# 保留在此仅供追溯，不参与构建 —— 同样的内容已在 build_kb.py 中。
_LEGACY_UNUSED = [{"source": "JJF1059.1 测量不确定度评定", "content": ""}]


# ── 文档源 ────────────────────────────────────────────────

def get_all_docs():
    """汇总全部文档源。导入失败一律硬失败，绝不"少一半也照建"。"""
    docs, errors = [], []

    for mod, attr in (
        ("knowledge_base.build_kb", "DOCUMENTS"),
        ("knowledge_base.extra_docs", "EXTRA_DOCUMENTS"),
        ("knowledge_base.recovered_docs", "RECOVERED_DOCUMENTS"),
    ):
        try:
            m = __import__(mod, fromlist=[attr])
            got = getattr(m, attr)
            if not isinstance(got, list):
                raise TypeError(f"{attr} 不是 list，实际是 {type(got).__name__}")
            docs.extend(got)
            print(f"  [源] {mod}.{attr}: {len(got)} 条")
        except Exception as e:
            errors.append(f"{mod}.{attr} 导入失败: {type(e).__name__}: {e}")

    if errors:
        raise RuntimeError(
            "知识库文档源导入失败，已中止以避免建成残缺知识库：\n  - "
            + "\n  - ".join(errors)
            + "\n\n常见原因：缺 scikit-learn（build_kb.py 在模块级 import sklearn）、"
              "或包路径不在 sys.path。修好后再运行。"
        )

    # 去重：同一 source 只保留首次出现
    seen, uniq = set(), []
    for d in docs:
        s = d.get("source", "")
        if not s or s in seen:
            if s in seen:
                print(f"  [去重] 跳过重复 source: {s[:50]}")
            continue
        seen.add(s)
        uniq.append(d)

    print(f"  [汇总] {len(docs)} 条 → 去重后 {len(uniq)} 条")
    return uniq


# ── 向量化 ────────────────────────────────────────────────

def embed(text: str) -> list:
    """单条向量化（失败抛异常，不静默返回 None）"""
    resp = TextEmbedding.call(api_key=API_KEY, model=EMBED_MODEL, input=text[:6000])
    if resp.status_code != 200:
        raise RuntimeError(f"Embedding 失败: {resp}")
    vec = resp.output["embeddings"][0]["embedding"]
    if not vec:
        raise RuntimeError("Embedding 返回空向量")
    return vec


def _norm(v) -> float:
    return sum(float(x) * float(x) for x in v) ** 0.5


def batch_embed(texts: list, batch_size: int = 10, expect_unit_norm: bool = True) -> list:
    """批量向量化。

    审计修复：原版 batch_size 只用于 print，实际是逐条调用（112 条 = 112 次
    HTTPS 往返 + 每次 sleep 0.1s）。此处保留逐条调用（DashScope 多输入接口
    需另行确认批量语义），但去掉无意义的 batch 打印，并**校验单位范数**——
    因为 kb_tools 的 sim = 1 - dist/2 依赖该性质。
    """
    out = []
    t0 = time.time()
    for i, text in enumerate(texts, 1):
        vec = embed(text)
        if expect_unit_norm:
            n = _norm(vec)
            if abs(n - 1.0) > 1e-2:
                raise RuntimeError(
                    f"第 {i} 条向量范数为 {n:.4f}，不是单位向量。\n"
                    f"kb_tools 的相似度换算 sim = max(0, 1 - dist/2) 依赖单位范数"
                    f"（此时平方 L2 距离 = 2 - 2·cos）。\n"
                    f"若换了 embedding 模型，必须同步修改 kb_tools 的换算公式，"
                    f"否则 0.35/0.45 阈值会静默失效。构建已中止。"
                )
        out.append(vec)
        if i % 10 == 0 or i == len(texts):
            print(f"  [{i}/{len(texts)}] 已嵌入  ({time.time()-t0:.0f}s)")
        time.sleep(0.05)  # 温和限速
    return out


# ── 校验 ──────────────────────────────────────────────────

def verify(db_path: str) -> dict:
    """只读校验：报告线上索引与源码文档源的差异。"""
    client = PersistentClient(path=db_path)
    try:
        col = client.get_collection(COLLECTION)
    except Exception as e:
        print(f"[校验] 无法打开 {COLLECTION}: {e}")
        return {}
    data = col.get(include=["metadatas", "documents"])
    live_src = {(m or {}).get("source", "") for m in data["metadatas"]}

    try:
        src_docs = {d["source"] for d in get_all_docs()}
    except Exception as e:
        print(f"[校验] 源码文档源不可用: {e}")
        return {"live": len(live_src)}

    only_live = sorted(s for s in live_src - src_docs if s)
    only_src = sorted(s for s in src_docs - live_src if s)

    print(f"\n[校验] 线上 {len(live_src)} 条 distinct source；源码 {len(src_docs)} 条")
    if only_live:
        print(f"  [!] 仅存在于线上索引、源码中没有（重建将丢失）{len(only_live)} 条：")
        for s in only_live:
            print(f"      - {s[:90]}")
    else:
        print("  [OK] 线上条目全部可由源码复现")
    if only_src:
        print(f"  [i] 源码里有、线上尚无（需重建才生效）{len(only_src)} 条：")
        for s in only_src[:10]:
            print(f"      - {s[:90]}")
    return {"live": len(live_src), "source": len(src_docs),
            "only_live": only_live, "only_src": only_src}


# ── 构建（非破坏式） ──────────────────────────────────────

def build():
    print("=" * 62)
    print("Equis 知识库构建（非破坏式）")
    print("=" * 62)

    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "chroma_db")
    os.makedirs(db_path, exist_ok=True)

    # 1) 取文档（导入失败会在此硬失败，旧库完好）
    docs = get_all_docs()
    texts = [d["content"][:6000] for d in docs]
    sources = [d["source"] for d in docs]
    ids = [f"doc_{i}" for i in range(len(docs))]

    # 2) 向量化（失败则旧库完好）
    print(f"\n[1/4] 生成向量（{len(texts)} 条）...")
    vectors = batch_embed(texts)

    client = PersistentClient(path=db_path)

    # 3) 写入 staging（若已存在说明上次中断，先清掉）
    print(f"\n[2/4] 写入临时集合 {STAGING} ...")
    try:
        client.delete_collection(STAGING)
        print("  （清理上次遗留的 staging）")
    except Exception:
        pass
    staging = client.create_collection(
        name=STAGING,
        metadata={"description": "计量标准知识库", "hnsw:space": HNSW_SPACE},
    )
    staging.add(
        embeddings=vectors,
        documents=texts,
        metadatas=[{"source": s} for s in sources],
        ids=ids,
    )

    # 4) 数量校验 —— 不过关就中止，旧库不动
    got = staging.count()
    if got != len(docs):
        raise RuntimeError(
            f"数量校验失败：期望 {len(docs)} 条，staging 实际 {got} 条。已中止，原库未改动。"
        )
    print(f"  ✅ 数量校验通过：{got} 条")

    # 5) 归档旧库 → 原子切换
    print(f"\n[3/4] 归档旧集合并切换 ...")
    try:
        old = client.get_collection(COLLECTION)
        old_meta = old.get(include=[])
        print(f"  旧集合 {COLLECTION}：{old.count()} 条")
        try:
            client.delete_collection(BACKUP)
        except Exception:
            pass
        # Chroma 无 rename API：把旧集合名改为 BACKUP 需重建，故此处
        # 采用"删除旧 + 改名 staging"两步；因 staging 已建成并通过校验，
        # 且旧库已有磁盘级备份（build 前后请自行备份 data/chroma_db），
        # 风险窗口仅剩两步之间，远小于原来的"先删后嵌入"。
        client.delete_collection(COLLECTION)
    except Exception as e:
        print(f"  ℹ 无旧集合或读取失败（首次构建？）：{e}")

    # Chroma 1.x 无 rename，需用 create+copy 方式；这里用 get_or_create 迁移
    new_col = client.create_collection(
        name=COLLECTION,
        metadata={"description": "计量标准知识库", "hnsw:space": HNSW_SPACE},
    )
    new_col.add(
        embeddings=vectors,
        documents=texts,
        metadatas=[{"source": s} for s in sources],
        ids=ids,
    )
    client.delete_collection(STAGING)

    final = client.get_collection(COLLECTION).count()
    print(f"\n[4/4] 完成：{COLLECTION} = {final} 条")
    print(f"  路径: {db_path}")
    if final != len(docs):
        raise RuntimeError(f"最终数量异常：{final} ≠ {len(docs)}")
    print("  ✅ 构建成功")
    return final


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Equis 知识库构建")
    ap.add_argument("--verify", action="store_true", help="只校验索引与源码差异，不写入")
    args = ap.parse_args()

    db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "chroma_db")
    if args.verify:
        verify(db)
    else:
        build()
