"""P4-42 验收测试：数据不出境 Gate。

覆盖：白名单内放行 / 后缀伪装拒绝 / http 与 https 都处理 /
非 URL 输入报错清晰 / 出站台账 / 审计留痕。

运行：<解释器> -m unittest scripts.test_data_gate -v

维护（2026-10-17）：**测试不得污染生产审计库**。
原实现直接用 compliance.gate.AUDIT_DB（即 agent/data/audit.db）跑审计断言，
每跑一次测试就往生产库里写若干条 event='outbound' 的记录。
实测后果：audit.db 里的 119 条 outbound **全部来自测试运行**
（时间戳对得上两次测试），而生产调用点当时一条都没有 ——
导致"出站域名清单"这一合规指标被测试数据污染、失去意义。
现改为整个模块使用临时库，结束时恢复并清理。
"""
import os
import sys
import tempfile
import unittest

_AGENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _AGENT)
sys.path.insert(0, os.path.dirname(_AGENT))

from compliance.gate import (
    ALLOWED_HOSTS, ComplianceError, assert_host_allowed, guard_request,
    host_of, is_host_allowed, outbound_summary, record_outbound, reset_outbound,
)

#: 模块级临时审计库（setUpModule 填充）
_TMP_AUDIT_DB = None
_ORIG_AUDIT_DB = None


def setUpModule():
    """把审计库指向临时文件，避免污染生产 audit.db"""
    global _TMP_AUDIT_DB, _ORIG_AUDIT_DB
    from compliance import gate as G
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)                    # 让 sqlite 自己创建
    _TMP_AUDIT_DB = path
    _ORIG_AUDIT_DB = G.AUDIT_DB
    G.AUDIT_DB = path
    # 丢弃线程本地连接缓存（否则仍连着旧库）
    G._tls.__dict__.clear()


def tearDownModule():
    global _TMP_AUDIT_DB, _ORIG_AUDIT_DB
    from compliance import gate as G
    try:
        G._tls.__dict__.clear()
    except Exception:
        pass
    if _ORIG_AUDIT_DB is not None:
        G.AUDIT_DB = _ORIG_AUDIT_DB
    for p in (_TMP_AUDIT_DB, _TMP_AUDIT_DB + "-wal", _TMP_AUDIT_DB + "-shm"):
        try:
            if p and os.path.exists(p):
                os.unlink(p)
        except OSError:
            pass


class HostParseTest(unittest.TestCase):
    def test_https_url(self):
        self.assertEqual(host_of("https://dashscope.aliyuncs.com/compatible-mode/v1"),
                         "dashscope.aliyuncs.com")

    def test_http_url(self):
        self.assertEqual(host_of("http://dashscope.aliyuncs.com/x"), "dashscope.aliyuncs.com")

    def test_port_stripped(self):
        self.assertEqual(host_of("https://dashscope.aliyuncs.com:443/x"),
                         "dashscope.aliyuncs.com")

    def test_reject_non_http_scheme(self):
        for bad in ("ftp://dashscope.aliyuncs.com/x", "file:///etc/passwd",
                    "javascript:alert(1)"):
            with self.assertRaises(ComplianceError):
                host_of(bad)

    def test_reject_empty_and_none(self):
        for bad in ("", None, "   "):
            with self.assertRaises(ComplianceError):
                host_of(bad)


class WhitelistTest(unittest.TestCase):
    def test_allowed_exact(self):
        self.assertTrue(is_host_allowed("https://dashscope.aliyuncs.com/x"))

    def test_allowed_subdomain(self):
        self.assertTrue(is_host_allowed("https://api.dashscope.aliyuncs.com/x"))

    def test_reject_suffix_disguise(self):
        """最关键的安全用例：后缀伪装必须被拒"""
        for bad in (
            "https://dashscope.aliyuncs.com.evil.com/x",
            "https://evildashscope.aliyuncs.com/x",
            "https://dashscope.aliyuncs.com.attacker.cn/x",
            "https://notdashscope.aliyuncs.com/x",
            "https://xaliyuncs.com/x",
        ):
            self.assertFalse(is_host_allowed(bad), "应拒绝: %s" % bad)
            with self.assertRaises(ComplianceError, msg="应抛异常: %s" % bad):
                assert_host_allowed(bad)

    def test_reject_foreign_hosts(self):
        for bad in ("https://api.openai.com/v1", "https://cdn.jsdelivr.net/x",
                    "https://generativelanguage.googleapis.com/x",
                    "https://example.com/x"):
            self.assertFalse(is_host_allowed(bad))
            with self.assertRaises(ComplianceError):
                assert_host_allowed(bad)

    def test_assert_returns_host(self):
        self.assertEqual(assert_host_allowed("https://dashscope.aliyuncs.com/a/b"),
                         "dashscope.aliyuncs.com")

    def test_error_message_is_actionable(self):
        try:
            assert_host_allowed("https://api.openai.com/v1")
            self.fail("应抛 ComplianceError")
        except ComplianceError as e:
            msg = str(e)
            self.assertIn("api.openai.com", msg)
            self.assertIn("ALLOWED_HOSTS", msg)
            self.assertIn("数据不出境", msg)


class OutboundLedgerTest(unittest.TestCase):
    def setUp(self):
        reset_outbound()

    def tearDown(self):
        reset_outbound()

    def test_record_and_summary(self):
        record_outbound("https://dashscope.aliyuncs.com/a")
        record_outbound("https://dashscope.aliyuncs.com/b")
        s = outbound_summary()
        self.assertEqual(s["domains"].get("dashscope.aliyuncs.com"), 2)
        self.assertTrue(s["all_allowed"])
        self.assertEqual(s["violations"], [])

    def test_summary_flags_violation(self):
        record_outbound("https://api.openai.com/v1")
        s = outbound_summary()
        self.assertFalse(s["all_allowed"])
        self.assertIn("api.openai.com", s["violations"])

    def test_check_data_residency_exposes_outbound(self):
        record_outbound("https://dashscope.aliyuncs.com/x")
        from compliance.gate import check_data_residency
        r = check_data_residency()
        self.assertIn("outbound_domains", r)
        self.assertIn("dashscope.aliyuncs.com", r["outbound_domains"])

    def test_compliance_report_has_outbound(self):
        from compliance.gate import compliance_report
        rec = compliance_report()
        self.assertIn("outbound_domains", rec["data_residency"])


class GuardRequestTest(unittest.TestCase):
    def setUp(self):
        reset_outbound()

    def tearDown(self):
        reset_outbound()

    def test_blocks_before_any_network_call(self):
        """Gate 必须在发请求前拦截 —— 用不存在的域名验证『根本没连网』"""
        with self.assertRaises(ComplianceError):
            guard_request("POST", "https://nonexistent.invalid/x", json={})
        # 被拦截的调用不产生出站记录（因为没出去）
        self.assertEqual(outbound_summary()["domains"], {})

    def test_allowed_request_proceeds_and_is_recorded(self):
        """用本地 httpbin 风格不存在时也应走到网络层并记录出站"""
        try:
            guard_request("GET", "https://dashscope.aliyuncs.com/definitely-404", timeout=5)
        except Exception:
            pass   # 网络错误/404 都可接受，重点是 Gate 放行且记了台账
        s = outbound_summary()
        self.assertIn("dashscope.aliyuncs.com", s["domains"])


class AuditIntegrationTest(unittest.TestCase):
    def test_outbound_event_type_registered(self):
        from compliance.gate import EVENT_TYPES
        self.assertIn("outbound", EVENT_TYPES)

    def test_outbound_writes_audit_row(self):
        import sqlite3
        from compliance.gate import AUDIT_DB, audit_stats
        before = audit_stats()["by_event"].get("outbound", 0)
        record_outbound("https://dashscope.aliyuncs.com/audit-probe")
        after = audit_stats()["by_event"].get("outbound", 0)
        self.assertGreater(after, before, "record_outbound 应写入审计")

    def test_blocked_call_is_audited_as_blocked(self):
        import sqlite3
        from compliance.gate import AUDIT_DB
        record_outbound("https://api.openai.com/v1")
        c = sqlite3.connect(AUDIT_DB)
        rows = c.execute(
            "SELECT detail FROM audit WHERE event='outbound' ORDER BY id DESC LIMIT 5").fetchall()
        c.close()
        self.assertTrue(any("BLOCKED" in (r[0] or "") for r in rows),
                        "被拒域名应记为 BLOCKED，实际: %s" % rows)


if __name__ == "__main__":
    unittest.main(verbosity=2)
