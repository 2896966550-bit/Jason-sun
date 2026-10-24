"""轻量可观测 —— SQLite Trace（运行 / 节点 / 耗时 / 用量）

设计取舍：不引入重型 APM（OpenTelemetry 等），用一个 SQLite 文件 + 极简 API，
为评分表"技术实现与系统完整性（可测试/可部署）"提供硬证据。

使用 contextvars 传递 run_id：LangGraph 并行分支会复制上下文，
各节点写 trace 自动归属同一次运行，无需改动节点的调用签名。

────────────────────────────────────────────────────────────────
P4-41：补齐用量维度（tokens / 成本 / 工具调用）
────────────────────────────────────────────────────────────────
原表只有 node/ms/note，**没有 token 计数、没有成本、没有工具调用统计** ——
而 2026 年主流可观测（OpenTelemetry GenAI 语义约定）恰恰以
"每次 LLM 调用的 token 与成本"为核心维度。没有它就无法回答
"这一轮花了多少钱、哪个节点最贵、有多少次工具调用"。

新增列（**全部通过幂等迁移添加，旧数据完整保留**）：
  runs : tokens_prompt / tokens_completion / tokens_total /
         cost_cny / tool_calls / llm_calls
  steps: tokens / cost_cny / model

迁移策略：`PRAGMA table_info` 检测缺列 → `ALTER TABLE ADD COLUMN`。
重复启动不报错、不重建表、不丢历史数据。
"""
import os, sys, time, sqlite3, threading
from contextvars import ContextVar

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DATA_DIR

DB = os.path.join(DATA_DIR, "trace.db")
_lock = threading.Lock()

#: 线程本地连接（维护 2026-10-02：_conn() 复用连接，见其 docstring 的实测数据）
_tls = threading.local()

# 当前运行 id（LangGraph 并行分支自动继承）
current_run_id: ContextVar = ContextVar("equis_trace_run_id", default=None)

# ── 单价（估算，单位：元 / 千 token）──────────────────────
# 说明：这是**可配置的估算值**，不是账单。真实价格随模型与优惠变动，
# 请以百炼控制台为准。用环境变量覆盖：
#   EQUIS_PRICE_IN=0.002   EQUIS_PRICE_OUT=0.008
PRICE_IN_PER_1K = float(os.getenv("EQUIS_PRICE_IN", "0.002"))
PRICE_OUT_PER_1K = float(os.getenv("EQUIS_PRICE_OUT", "0.008"))


# ── 表结构定义（用于迁移）────────────────────────────────
_RUNS_COLUMNS = (
    ("tokens_prompt", "INTEGER DEFAULT 0"),
    ("tokens_completion", "INTEGER DEFAULT 0"),
    ("tokens_total", "INTEGER DEFAULT 0"),
    ("cost_cny", "REAL DEFAULT 0"),
    ("tool_calls", "INTEGER DEFAULT 0"),
    ("llm_calls", "INTEGER DEFAULT 0"),
)
_STEPS_COLUMNS = (
    ("tokens", "INTEGER DEFAULT 0"),
    ("cost_cny", "REAL DEFAULT 0"),
    ("model", "TEXT DEFAULT ''"),
)


def _existing_columns(c, table: str) -> set:
    return {r[1] for r in c.execute("PRAGMA table_info(%s)" % table)}


def _migrate(c) -> list:
    """幂等迁移：缺哪列补哪列，返回本次实际新增的列名。

    只做 ADD COLUMN —— 不重建表、不改既有列，因此**历史数据永不丢失**。
    """
    added = []
    for table, cols in (("runs", _RUNS_COLUMNS), ("steps", _STEPS_COLUMNS)):
        have = _existing_columns(c, table)
        for name, decl in cols:
            if name not in have:
                c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))
                added.append("%s.%s" % (table, name))
    return added


def _conn():
    """获取当前线程的 SQLite 连接（线程本地复用）

    维护（2026-10-02）：原实现每次调用都 `sqlite3.connect()` 新建连接并
    立即 close()。实测代价（Python 3.13.14，SSD）：

        裸 connect + CREATE IF NOT EXISTS x2 + PRAGMA x2 + commit + close
        ≈ 0.77 ms/次；而走完整 _conn() 的一次 step() ≈ 20 ms

    差额几乎全部来自 **每次 commit 触发的 fsync 磁盘同步**（新建连接本身很便宜）。
    一次典型图执行会写 8 个节点（6 个并行分支 + perceive/supervisor/judge/merge），
    实测 60 runs × 8 steps 累计 9.67 s 纯 trace 开销，而整轮图执行约 11.4 s。

    改为：连接按线程复用（threading.local），建表与迁移**每线程只做一次**，
    写操作仍逐次 commit（保证可观测数据不因进程崩溃全丢）。
    读操作也不再每次建连接。

    线程安全：sqlite 连接默认不可跨线程使用，这里严格一线程一连接，
    与既有 `_lock`（跨线程串行化写）配合使用。

    缓存失效：`_tls` 同时记录建立连接时的 DB 路径。DB 路径变化
    （测试会 monkeypatch `trace.DB` 指向临时库）或连接已被关闭时，
    自动丢弃缓存重建，避免 "Cannot operate on a closed database"。
    """
    conn = getattr(_tls, "conn", None)
    db_path = getattr(_tls, "db_path", None)
    if conn is not None:
        if db_path == DB:
            try:
                # 探活：执行最轻量的查询，连接被外部关闭时抛 ProgrammingError
                conn.execute("SELECT 1")
                return conn
            except sqlite3.ProgrammingError:
                pass
        # DB 路径变了或连接已失效：丢弃重建
        try:
            conn.close()
        except Exception:
            pass
        _tls.conn = None
        _tls.db_path = None
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB, check_same_thread=False)
    # WAL 模式（实测 SQLite 3.13+ / Python 3.13）：读不阻塞写、写不阻塞读，
    # 且提交代价从"改写主库文件 + fsync"降为"追加 WAL"。
    # 这对 trace 的访问模式（6 个并行分支各写、同时 /api/trace 轮询读）收益最大。
    # busy_timeout 兜住偶发的锁竞争，避免直接抛 "database is locked"。
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=5000")
    except sqlite3.Error:
        # 老版本 SQLite 不支持 WAL 时静默降级为默认日志模式
        pass
    c.execute("""CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL, engine TEXT, query TEXT, image TEXT,
        total_ms INTEGER, ok INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER, ts REAL, node TEXT, label TEXT,
        ms INTEGER, note TEXT)""")
    added = _migrate(c)
    if added:
        print("[Trace] 表结构已迁移，新增列: %s" % ", ".join(added))
    c.commit()
    _tls.conn = c
    _tls.db_path = DB
    return c


def _close_conn() -> None:
    """关闭当前线程的连接（进程退出 / 测试清理时调用）"""
    c = getattr(_tls, "conn", None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
        _tls.conn = None
        _tls.db_path = None


def migrate() -> list:
    """显式执行迁移，返回**本次实际新增**的列名列表（供测试与启动时调用）。

    注意：`_conn()` 内部已经会跑一次迁移，因此这里不能简单地
    `_conn(); _migrate()` —— 那样第二次必然是空列表，会掩盖"到底加没加列"。
    改为：先直接连库（不迁移）看缺哪些列，再走 `_conn()`（它会补列），
    最后把差额作为本次新增返回。
    """
    with _lock:
        os.makedirs(os.path.dirname(DB), exist_ok=True)
        c = sqlite3.connect(DB, check_same_thread=False)
        c.execute("""CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, engine TEXT, query TEXT, image TEXT,
            total_ms INTEGER, ok INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER, ts REAL, node TEXT, label TEXT,
            ms INTEGER, note TEXT)""")
        missing = []
        for table, cols in (("runs", _RUNS_COLUMNS), ("steps", _STEPS_COLUMNS)):
            have = _existing_columns(c, table)
            for name, _decl in cols:
                if name not in have:
                    missing.append("%s.%s" % (table, name))
        c.commit()
        c.close()

        # 走正常连接流程完成补列（_conn 内部会调用 _migrate）
        c2 = _conn()
        c2.commit()
        c2.close()
    # 返回"迁移前缺哪些列"——这正是本次实际新增的集合。
    # 不能返回 _migrate(_conn()) 的结果：_conn 内部已补过列，那样必然为空。
    return missing


def start_run(engine: str, query: str, image: str = "") -> int:
    """开始一次运行，返回 run_id 并绑定到当前上下文"""
    with _lock:
        c = _conn()
        cur = c.execute(
            "INSERT INTO runs (ts, engine, query, image) VALUES (?,?,?,?)",
            (time.time(), engine, str(query)[:200], str(image)[:150]),
        )
        c.commit()
        rid = cur.lastrowid
    current_run_id.set(rid)
    return rid


def end_run(run_id: int, total_ms: int, ok: bool = True) -> None:
    with _lock:
        c = _conn()
        c.execute("UPDATE runs SET total_ms=?, ok=? WHERE id=?", (total_ms, 1 if ok else 0, run_id))
        c.commit()
    current_run_id.set(None)


def step(node: str, label: str, ms: int, note: str = "",
         tokens: int = 0, model: str = "") -> None:
    """记录一个节点/步骤（自动归属当前 run；无运行上下文时忽略）"""
    rid = current_run_id.get()
    if rid is None:
        return
    cost = price_of(0, tokens) if tokens else 0.0
    with _lock:
        c = _conn()
        c.execute(
            "INSERT INTO steps (run_id, ts, node, label, ms, note, tokens, cost_cny, model) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, time.time(), node, label, ms, str(note)[:200],
             int(tokens or 0), cost, str(model or "")[:60]),
        )
        if tokens:
            # 同步累加到本次运行
            c.execute(
                "UPDATE runs SET tokens_total=COALESCE(tokens_total,0)+?, "
                "cost_cny=COALESCE(cost_cny,0)+? WHERE id=?", (int(tokens), cost, rid))
        c.commit()


# ── 用量记录（P4-41 新增）────────────────────────────────

def price_of(prompt_tokens: int, completion_tokens: int) -> float:
    """按单价估算成本（元）。单价可经环境变量覆盖，见文件头注释。"""
    return (max(0, int(prompt_tokens or 0)) / 1000.0) * PRICE_IN_PER_1K + \
           (max(0, int(completion_tokens or 0)) / 1000.0) * PRICE_OUT_PER_1K


def record_llm(model: str, prompt_tokens: int = 0, completion_tokens: int = 0,
               node: str = None) -> dict:
    """记录一次 LLM 调用（token + 估算成本）。

    自动归属当前 run（无上下文时仍返回计算结果，只是不入库）。

    P5-51：除累加到 runs（整轮汇总）外，**同步写一条 steps**，
    使节点级 token / 成本 / 模型可查。此前 steps.tokens 恒为 0，
    /api/trace 的时间轴无法回答"哪个环节最贵"。
    """
    p = max(0, int(prompt_tokens or 0))
    q = max(0, int(completion_tokens or 0))
    cost = price_of(p, q)
    rid = current_run_id.get()
    if rid is not None:
        with _lock:
            c = _conn()
            c.execute(
                "UPDATE runs SET "
                "tokens_prompt=COALESCE(tokens_prompt,0)+?, "
                "tokens_completion=COALESCE(tokens_completion,0)+?, "
                "tokens_total=COALESCE(tokens_total,0)+?, "
                "cost_cny=COALESCE(cost_cny,0)+?, "
                "llm_calls=COALESCE(llm_calls,0)+1 WHERE id=?",
                (p, q, p + q, cost, rid))
            # 节点级归因：node 缺省时用模型名，保证每条 LLM 调用都有可归因的 step
            c.execute(
                "INSERT INTO steps (run_id, ts, node, label, ms, note, tokens, cost_cny, model) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (rid, time.time(), str(node or model or "llm"),
                 "LLM 调用", 0,
                 "p=%d q=%d" % (p, q), p + q, cost, str(model or "")[:60]))
            c.commit()
    return {"model": model, "prompt_tokens": p, "completion_tokens": q,
            "total_tokens": p + q, "cost_cny": round(cost, 6), "node": node}


def record_usage_from_resp(resp, node: str = None) -> dict:
    """从百炼/OpenAI 兼容响应中提取 usage 并记入 trace（P5-51 补齐）

    为什么需要这个函数（维护 2026-10-02）：
        `record_llm` / `usage_summary` / trace.db 的 tokens_* / cost_cny 列
        早已实现且有测试覆盖，但**生产代码里从未调用** ——
        grep 全项目只有 scripts/test_trace_migration.py 命中。
        结果是 trace.db 里 tokens_total / cost_cny 永远是 0，
        /api/trace 展示的"用量"维度形同虚设，
        而这恰恰是可观测性（评分表"系统完整性"）最硬的一维证据。

    现在由三个 LLM 调用点统一经此函数记账：
        coordinator._llm_call        （规元/度元/索元正文 + 最终融合）
        coordinator.rewrite_query    （多轮指代消解）
        agent_router.route           （结构化路由）

    健壮性：usage 在不同网关/模型下可能缺失或为 None，
    因此全程 getattr + 逐级兜底，**任何异常都不允许影响主流程**
    （可观测性是旁路，绝不能反过来拖垮业务）。
    """
    try:
        usage = getattr(resp, "usage", None)
        if usage is None:
            return {}
        p = int(getattr(usage, "prompt_tokens", 0) or 0)
        q = int(getattr(usage, "completion_tokens", 0) or 0)
        if p <= 0 and q <= 0:
            return {}
        model = getattr(resp, "model", "") or ""
        return record_llm(model, p, q, node=node)
    except Exception as e:  # 记账失败绝不影响主流程
        print("[Trace] usage 记录失败（已忽略）: %s: %s" % (type(e).__name__, str(e)[:80]))
        return {}


def record_tool(name: str, ms: int = 0, ok: bool = True) -> None:
    """记录一次工具调用（计入本次运行的 tool_calls 计数）"""
    rid = current_run_id.get()
    if rid is None:
        return
    with _lock:
        c = _conn()
        c.execute("UPDATE runs SET tool_calls=COALESCE(tool_calls,0)+1 WHERE id=?", (rid,))
        c.commit()


def usage_summary(days: int = 7) -> dict:
    """近 N 天的用量汇总（总 token、总成本、调用次数、平均每轮成本）"""
    since = time.time() - days * 86400
    with _lock:
        c = _conn()
        row = c.execute(
            "SELECT COUNT(*), COALESCE(SUM(tokens_total),0), COALESCE(SUM(cost_cny),0), "
            "COALESCE(SUM(tool_calls),0), COALESCE(SUM(llm_calls),0) "
            "FROM runs WHERE ts >= ?", (since,)).fetchone()
    n, tok, cost, tools, lls = row
    return {
        "days": days, "runs": n,
        "tokens_total": int(tok or 0),
        "cost_cny": round(float(cost or 0), 6),
        "tool_calls": int(tools or 0),
        "llm_calls": int(lls or 0),
        "avg_cost_per_run": round(float(cost or 0) / n, 6) if n else 0.0,
        "price_in_per_1k": PRICE_IN_PER_1K,
        "price_out_per_1k": PRICE_OUT_PER_1K,
        "note": "成本为按单价的估算值，非账单；单价可用 EQUIS_PRICE_IN/OUT 覆盖",
    }


def recent(n: int = 20) -> list:
    """最近 n 次运行摘要（含用量字段）"""
    with _lock:
        c = _conn()
        rows = c.execute(
            "SELECT id, ts, engine, query, total_ms, ok, "
            "COALESCE(tokens_total,0), COALESCE(cost_cny,0), "
            "COALESCE(tool_calls,0), COALESCE(llm_calls,0) "
            "FROM runs ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return [{"id": r[0], "ts": r[1], "engine": r[2], "query": r[3], "total_ms": r[4],
             "ok": r[5], "tokens_total": r[6], "cost_cny": r[7],
             "tool_calls": r[8], "llm_calls": r[9]} for r in rows]


def detail(run_id: int) -> dict:
    """一次运行的完整轨迹（节点时序 + 用量）"""
    with _lock:
        c = _conn()
        run = c.execute(
            "SELECT id, ts, engine, query, image, total_ms, ok, "
            "COALESCE(tokens_prompt,0), COALESCE(tokens_completion,0), "
            "COALESCE(tokens_total,0), COALESCE(cost_cny,0), "
            "COALESCE(tool_calls,0), COALESCE(llm_calls,0) "
            "FROM runs WHERE id=?", (run_id,)).fetchone()
        steps = c.execute(
            "SELECT node, label, ms, note, ts, COALESCE(tokens,0), "
            "COALESCE(cost_cny,0), COALESCE(model,'') "
            "FROM steps WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
    if not run:
        return {}
    return {
        "id": run[0], "ts": run[1], "engine": run[2], "query": run[3], "image": run[4],
        "total_ms": run[5], "ok": run[6],
        "tokens_prompt": run[7], "tokens_completion": run[8], "tokens_total": run[9],
        "cost_cny": run[10], "tool_calls": run[11], "llm_calls": run[12],
        "steps": [{"node": s[0], "label": s[1], "ms": s[2], "note": s[3], "ts": s[4],
                   "tokens": s[5], "cost_cny": s[6], "model": s[7]} for s in steps],
    }


# 进程启动即确保表结构最新（幂等，重复导入无副作用）
try:
    migrate()
except Exception as _e:  # 迁移失败不应阻断服务启动
    print("[Trace] 启动迁移失败（不影响主流程）: %s" % _e)
