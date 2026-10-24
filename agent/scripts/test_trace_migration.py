"""P4-41 验收测试：trace 表迁移幂等 + 用量记录。

运行：<解释器> -m unittest agent.scripts.test_trace_migration -v
 或：  <解释器> agent/scripts/test_trace_migration.py
"""
import io
import os
import sqlite3
import sys
import tempfile
import unittest

_AGENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _AGENT)
sys.path.insert(0, os.path.dirname(_AGENT))


class TraceMigrationTest(unittest.TestCase):
    """覆盖：空库建表 / 旧结构升级 / 重复迁移幂等 / 用量读写 / 历史数据保留"""

    def setUp(self):
        # 每个用例用独立的库文件，互不干扰。
        # 模块导入时会执行一次 migrate()（面向真实库），因此这里**不 reload**，
        # 而是把模块的 DB 指向临时文件；临时文件由各用例自行建表。
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db)
        import observability.trace as t
        self.t = t
        self._orig_db = t.DB
        t.DB = self.db

    def tearDown(self):
        self.t.DB = self._orig_db
        try:
            os.unlink(self.db)
        except OSError:
            pass

    def _make_legacy_db(self):
        """在临时库里造出"改造前"的表结构 + 一行历史数据"""
        c = sqlite3.connect(self.db)
        c.execute("""CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, engine TEXT, query TEXT, image TEXT,
            total_ms INTEGER, ok INTEGER)""")
        c.execute("""CREATE TABLE steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER, ts REAL, node TEXT, label TEXT,
            ms INTEGER, note TEXT)""")
        c.execute("INSERT INTO runs (ts, engine, query, total_ms, ok) VALUES (?,?,?,?,?)",
                  (123.0, "graph", "历史查询", 999, 1))
        c.execute("INSERT INTO steps (run_id, ts, node, label, ms) VALUES (?,?,?,?,?)",
                  (1, 123.0, "retrieval", "规元", 500))
        c.commit()
        c.close()

    # ── 1. 空库建表 ──
    def test_empty_db_creates_full_schema(self):
        c = self.t._conn()
        runs_cols = {r[1] for r in c.execute("PRAGMA table_info(runs)")}
        steps_cols = {r[1] for r in c.execute("PRAGMA table_info(steps)")}
        c.close()
        for col in ("tokens_prompt", "tokens_completion", "tokens_total",
                    "cost_cny", "tool_calls", "llm_calls"):
            self.assertIn(col, runs_cols, "runs 缺少 %s" % col)
        for col in ("tokens", "cost_cny", "model"):
            self.assertIn(col, steps_cols, "steps 缺少 %s" % col)

    # ── 2. 旧结构升级（模拟改造前的表 + 已有数据）──
    def test_legacy_schema_upgrade_preserves_rows(self):
        # 造一个"改造前"的库：只有旧列，并写入一行历史数据
        self._make_legacy_db()

        added = self.t.migrate()
        self.assertTrue(added, "应报告新增了列")

        c = sqlite3.connect(self.db)
        # 历史数据必须还在
        row = c.execute("SELECT query, total_ms FROM runs WHERE id=1").fetchone()
        self.assertEqual(row[0], "历史查询")
        self.assertEqual(row[1], 999)
        srow = c.execute("SELECT node, ms FROM steps WHERE id=1").fetchone()
        self.assertEqual(srow[0], "retrieval")
        self.assertEqual(srow[1], 500)
        # 新列存在且默认值为 0
        r2 = c.execute("SELECT COALESCE(tokens_total,0) FROM runs WHERE id=1").fetchone()
        self.assertEqual(r2[0], 0)
        c.close()

    # ── 3. 重复迁移幂等 ──
    def test_migrate_is_idempotent(self):
        self.t.migrate()
        a2 = self.t.migrate()
        a3 = self.t.migrate()
        self.assertEqual(a2, [], "第二次迁移不应再新增列")
        self.assertEqual(a3, [], "第三次迁移不应再新增列")

    # ── 4. record_llm 累加 ──
    def test_record_llm_accumulates(self):
        rid = self.t.start_run("graph", "用量测试")
        self.t.record_llm("qwen3.8-max", 1000, 500)
        self.t.record_llm("qwen-flash", 200, 100)
        self.t.record_tool("kb_search", 120, True)
        self.t.record_tool("web_search", 30000, True)
        self.t.end_run(rid, 1234, True)

        d = self.t.detail(rid)
        self.assertEqual(d["tokens_prompt"], 1200)
        self.assertEqual(d["tokens_completion"], 600)
        self.assertEqual(d["tokens_total"], 1800)
        self.assertEqual(d["llm_calls"], 2)
        self.assertEqual(d["tool_calls"], 2)
        self.assertGreater(d["cost_cny"], 0)

    # ── 5. 成本计算正确性 ──
    def test_cost_formula(self):
        self.t.PRICE_IN_PER_1K = 0.002
        self.t.PRICE_OUT_PER_1K = 0.008
        # 1000 输入 + 1000 输出 = 0.002 + 0.008 = 0.010
        self.assertAlmostEqual(self.t.price_of(1000, 1000), 0.010, places=9)
        self.assertAlmostEqual(self.t.price_of(0, 0), 0.0, places=9)
        self.assertAlmostEqual(self.t.price_of(-5, -5), 0.0, places=9)

    # ── 6. step() 带 token 时同步累加到 run ──
    def test_step_with_tokens_rolls_up(self):
        rid = self.t.start_run("graph", "step 用量")
        self.t.step("retrieval", "规元·标准检索", 500, "命中 3 条",
                    tokens=800, model="qwen3.8-max")
        self.t.end_run(rid, 600, True)
        d = self.t.detail(rid)
        self.assertEqual(d["tokens_total"], 800)
        self.assertEqual(len(d["steps"]), 1)
        self.assertEqual(d["steps"][0]["tokens"], 800)
        self.assertEqual(d["steps"][0]["model"], "qwen3.8-max")

    # ── 7. recent / usage_summary 可读回新字段 ──
    def test_recent_and_usage_summary(self):
        rid = self.t.start_run("graph", "摘要测试")
        self.t.record_llm("m", 100, 100)
        self.t.end_run(rid, 100, True)

        rs = self.t.recent(5)
        self.assertTrue(rs)
        self.assertIn("tokens_total", rs[0])
        self.assertIn("cost_cny", rs[0])
        self.assertIn("llm_calls", rs[0])

        us = self.t.usage_summary(days=1)
        self.assertGreaterEqual(us["runs"], 1)
        self.assertEqual(us["tokens_total"], 200)
        self.assertIn("note", us)

    # ── 8. 无运行上下文时 record_* 不崩 ──
    def test_record_without_run_context(self):
        # 不调用 start_run，current_run_id 应为 None
        try:
            self.t.record_llm("m", 10, 10)
            self.t.record_tool("t", 1, True)
            self.t.step("x", "y", 1)     # 无上下文应静默忽略
        except Exception as e:
            self.fail("无运行上下文时不应抛异常: %r" % e)


if __name__ == "__main__":
    unittest.main(verbosity=2)
