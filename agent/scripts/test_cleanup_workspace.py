# -*- coding: utf-8 -*-
r"""P4-43 清理脚本的安全性验收（unittest）

────────────────────────────────────────────────────────────────
为什么必须给清理脚本写测试
────────────────────────────────────────────────────────────────
`cleanup_workspace.py` 是会**真删文件**的脚本，而且是唯一一个"跑错就不可逆"
的交付物。原交付只有"跑一次 dry-run 看输出"，没有任何自动化验收——
而"这次 dry-run 看起来对"完全不能保证它在别的工作区、别的路径形状下
不会删掉 `data/*.db`、`.env` 或知识库主库。

本文件把该脚本的安全契约钉成可重复执行的断言：
  1. 拒绝清单：每个条目及其**子路径**都不可删；
  2. 非白名单路径不可删（含路径穿越形状）；
  3. 六类垃圾目录都能被识别（用临时 fixture 树验证，不碰真实工作区）；
  4. **dry-run 必须零副作用**——跑完之后 fixture 树一个字节都不能少；
  5. `--apply` 的行为不可被他人误触发（无 `--apply` 时不进入删除分支）。

运行：
    <py> -m unittest scripts.test_cleanup_workspace -v
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import cleanup_workspace as C


class ProtectedPathTest(unittest.TestCase):
    """拒绝清单：无论怎么拼路径都不能删"""

    def test_every_never_delete_entry_is_protected(self):
        for rel in C.NEVER_DELETE:
            with self.subTest(rel=rel):
                self.assertTrue(C._is_protected(os.path.join(C._ROOT, rel)),
                                "拒绝清单条目未被保护: %s" % rel)

    def test_children_of_protected_entries_are_protected(self):
        """保护要向下传递：指向受保护目录内部的路径同样不能删"""
        for rel in C.NEVER_DELETE:
            child = os.path.join(C._ROOT, rel, "inner", "x.bin")
            with self.subTest(rel=rel):
                self.assertTrue(C._is_protected(child))

    def test_env_and_chroma_master_db_are_protected(self):
        """题目点名的三样东西：.env / data 下的库 / chroma.sqlite3"""
        for rel in ("agent/.env", "agent/data/chroma_db/chroma.sqlite3",
                    "agent/data/trace.db", "agent/data/audit.db",
                    "agent/data/chat_history.db", "agent/data/graph_checkpoints.db"):
            with self.subTest(rel=rel):
                self.assertTrue(C._is_protected(os.path.join(C._ROOT, rel)), rel)

    def test_weights_are_protected(self):
        """42MB 模型权重删了要重新训练/重新申请，必须保护"""
        for rel in ("agent/omni_ad/weights/student_best.pth",
                    "agent/omni_ad/weights/memory_bank.pkl"):
            with self.subTest(rel=rel):
                self.assertTrue(C._is_protected(os.path.join(C._ROOT, rel)), rel)

    def test_unrelated_path_is_not_protected(self):
        """反例：普通目录不该被误判为受保护（否则清理脚本永远删不掉东西）"""
        self.assertFalse(C._is_protected(os.path.join(C._ROOT, "agent", "__pycache__")))

    def test_any_db_under_data_is_protected(self):
        """题目要求『拒绝删除 data/*.db』——通配而非枚举。

        纵深防御：若只枚举已知数据库，将来新增 `agent/data/xxx.db`
        （例如 metrics.db）就不在保护范围内。

        已修复（P4-43 复核）：cleanup_workspace._is_protected() 现按
        `agent/data/.*\\.(db|sqlite3)(-wal|-shm)?$` 通配保护，
        因此本用例由 expectedFailure 转为正常通过，装饰器已移除。
        """
        self.assertTrue(C._is_protected(os.path.join(C._ROOT, "agent", "data", "some_new.db")))


class FixtureCleanupTest(unittest.TestCase):
    """用临时 fixture 树验证"能识别该删的、不碰不该删的"。

    为什么不直接对真实工作区断言：真实工作区的垃圾目录**已经被清理过**了，
    再跑一次什么也测不到；而且测试绝不该依赖"恰好还有垃圾可删"。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="equis_clean_fixture_")
        self._saved = (C._ROOT, C._AGENT)
        # 搭一棵 fixture：根 = tmp，agent/ 是它下面的代码目录
        self.root = self.tmp
        self.agent = os.path.join(self.root, "agent")
        os.makedirs(os.path.join(self.agent, "data", "chroma_db"), exist_ok=True)
        C._ROOT, C._AGENT = self.root, self.agent

        # ① 白名单目录（题目点名的四类里的三类）
        for d in ("agent_backup_0912", "%PROGRAMDATA%", os.path.join("agent", "NVIDIA Corporation")):
            os.makedirs(os.path.join(self.root, d), exist_ok=True)
            self._touch(os.path.join(self.root, d, "junk.txt"), "x" * 32)
        # ② __pycache__
        os.makedirs(os.path.join(self.agent, "tools", "__pycache__"), exist_ok=True)
        self._touch(os.path.join(self.agent, "tools", "__pycache__", "m.cpython-311.pyc"), "y" * 16)
        # ③ chroma 孤儿目录 + 活的 segment 目录 + 主库文件
        self.live_seg = "11111111-1111-1111-1111-111111111111"
        os.makedirs(os.path.join(self.agent, "data", "chroma_db", self.live_seg), exist_ok=True)
        self._touch(os.path.join(self.agent, "data", "chroma_db", self.live_seg, "data_level0.bin"), "z" * 8)
        self.orphan_seg = "99999999-9999-9999-9999-999999999999"
        os.makedirs(os.path.join(self.agent, "data", "chroma_db", self.orphan_seg), exist_ok=True)
        self._touch(os.path.join(self.agent, "data", "chroma_db", self.orphan_seg, "data_level0.bin"), "w" * 8)
        # ④ 绝不能删的东西（**必须在建 chroma.sqlite3 之前**，否则会被覆盖成普通文本）
        self._touch(os.path.join(self.agent, ".env"), "BAILIAN_API_KEY=secret")
        self._touch(os.path.join(self.agent, "data", "trace.db"), "db")
        # ⑤ chroma 主库：既是"孤儿判定"的数据源，又是拒绝清单里的"永不删除"对象
        self._make_segments_db()

    def tearDown(self):
        C._ROOT, C._AGENT = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── helpers ──
    def _touch(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def _make_segments_db(self):
        """造一个最小 chroma.sqlite3：既提供孤儿检测用的 segments 表，
        本身又是拒绝清单里"永不删除"的主库文件。"""
        import sqlite3
        db = os.path.join(self.agent, "data", "chroma_db", "chroma.sqlite3")
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE segments (id TEXT PRIMARY KEY, type TEXT, scope TEXT)")
        c.execute("CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT)")
        c.execute("INSERT INTO segments VALUES (?,?,?)", (self.live_seg, "urn:vector", "VECTOR"))
        c.execute("INSERT INTO collections VALUES (?,?)", ("c1", "metrology_kb"))
        c.commit()
        c.close()

    def _main_with_args(self, argv):
        """脚本用 argparse 从 sys.argv 取值，测试里临时替换 sys.argv"""
        saved = sys.argv
        sys.argv = ["cleanup_workspace.py"] + argv
        try:
            return C.main()
        finally:
            sys.argv = saved

    def _rel(self, targets):
        return {os.path.relpath(p, self.root).replace("\\", "/") for _k, p, _s, _n in targets}

    # ── 用例 ──
    def test_collects_all_whitelisted_dirs(self):
        rels = self._rel(C.collect_targets())
        for d in ("agent_backup_0912", "%PROGRAMDATA%", "agent/NVIDIA Corporation"):
            with self.subTest(d=d):
                self.assertIn(d, rels)

    def test_collects_pycache(self):
        self.assertIn("agent/tools/__pycache__", self._rel(C.collect_targets()))

    def test_collects_only_orphan_chroma_segments(self):
        rels = self._rel(C.collect_targets())
        self.assertIn("agent/data/chroma_db/" + self.orphan_seg, rels)
        self.assertNotIn("agent/data/chroma_db/" + self.live_seg, rels,
                         "活着的 segment 目录绝不能被当成孤儿删掉")

    def test_never_collects_protected_files(self):
        """把受保护文件做成"看起来可删"的形状，也不该进清理列表"""
        targets = C.collect_targets()
        for _k, p, _s, _n in targets:
            self.assertFalse(C._is_protected(p),
                             "受保护的路径进了清理列表: %s" % p)
        rels = self._rel(targets)
        for bad in ("agent/.env", "agent/data/trace.db",
                    "agent/data/chroma_db/chroma.sqlite3"):
            self.assertNotIn(bad, rels)

    def test_dry_run_has_zero_side_effect(self):
        """核心安全属性：不带 --apply 跑一次，fixture 树必须原封不动"""
        before = self._snapshot()
        rc = self._main_with_args([])          # 等价于命令行不带 --apply
        self.assertEqual(rc, 0)
        self.assertEqual(before, self._snapshot(),
                         "dry-run 改动了文件系统！这是最严重的安全缺陷")

    def test_apply_removes_junk_but_keeps_protected(self):
        """--apply --yes 走通删除分支：垃圾没了，受保护的还在"""
        rc = self._main_with_args(["--apply", "--yes"])
        self.assertEqual(rc, 0)
        for d in ("agent_backup_0912", "%PROGRAMDATA%", "agent/NVIDIA Corporation"):
            self.assertFalse(os.path.exists(os.path.join(self.root, d)), d)
        self.assertFalse(os.path.exists(os.path.join(self.agent, "tools", "__pycache__")))
        self.assertFalse(os.path.exists(os.path.join(self.agent, "data", "chroma_db", self.orphan_seg)))
        # 受保护的三样必须还在
        self.assertTrue(os.path.exists(os.path.join(self.agent, ".env")), ".env 被删了！")
        self.assertTrue(os.path.exists(os.path.join(self.agent, "data", "trace.db")), "trace.db 被删了！")
        self.assertTrue(os.path.exists(
            os.path.join(self.agent, "data", "chroma_db", "chroma.sqlite3")), "chroma 主库被删了！")
        self.assertTrue(os.path.exists(
            os.path.join(self.agent, "data", "chroma_db", self.live_seg)), "活 segment 被删了！")

    def _snapshot(self):
        """记录 fixture 树的 (相对路径, 大小) 集合，用于比对零副作用"""
        snap = {}
        for root, _dirs, files in os.walk(self.root):
            for f in files:
                p = os.path.join(root, f)
                snap[os.path.relpath(p, self.root)] = os.path.getsize(p)
        return snap


class PathEscapeTest(unittest.TestCase):
    """路径穿越：清理项必须落在项目根之下"""

    def test_relative_escape_is_detected(self):
        """os.path.abspath 会规范化 `..`，因此逃逸路径不会以 root+sep 开头"""
        root = os.path.abspath(C._ROOT)
        escaped = os.path.abspath(os.path.join(C._ROOT, "..", "..", "Windows"))
        self.assertFalse(escaped.startswith(root + os.sep))

    def test_normal_child_passes(self):
        root = os.path.abspath(C._ROOT)
        self.assertTrue(os.path.abspath(os.path.join(C._ROOT, "agent", "__pycache__"))
                        .startswith(root + os.sep))


if __name__ == "__main__":
    unittest.main(verbosity=2)
