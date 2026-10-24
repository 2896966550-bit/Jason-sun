"""人脸脱敏合规控件测试（P4-42 同族，2026-10-17 补）

背景：默认环境 opencv-python 5.0.0 移除了 cv2.CascadeClassifier，
原实现抛 AttributeError 后被外层 except 吞掉，返回 (原图, 0)，
调用方看来是"处理成功、无人脸"——**合规控件失败开放（fail-open）**。
本测试锁定新契约：必须返回三态 status，且 "unavailable" 必须可被识别。

运行：<解释器> -m unittest scripts.test_redaction -v
"""
import os
import sys
import unittest
import tempfile
from pathlib import Path

_AGENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _AGENT)
sys.path.insert(0, os.path.dirname(_AGENT))

from compliance.gate import (
    face_redaction_status, redact_faces, compliance_report,
)


class RedactionContractTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Contract tests need a valid raster, not private production uploads.
        import cv2
        import numpy as np
        fixture = tempfile.TemporaryDirectory(prefix="equis_redaction_")
        cls.addClassCleanup(fixture.cleanup)
        path = Path(fixture.name)/"fixture.png"
        ok, encoded = cv2.imencode(".png", np.zeros((64,64,3),dtype=np.uint8))
        if not ok:
            raise RuntimeError("Failed to encode local image fixture")
        path.write_bytes(encoded.tobytes())
        cls.image = str(path)

    def test_returns_three_tuple(self):
        self.assertIsNotNone(self.image, "缺少测试图片")
        out = redact_faces(self.image)
        self.assertEqual(len(out), 3, "必须返回 (path, n_faces, status) 三元组")
        path, n, status = out
        self.assertIsInstance(path, str)
        self.assertIsInstance(n, int)
        self.assertIn(status, ("ok", "none", "unavailable", "error"))

    def test_status_consistent_with_capability(self):
        """能力不可用时，返回的 status 必须是 unavailable（不能谎报 none）"""
        cap = face_redaction_status()
        if self.image is None:
            self.skipTest("缺少测试图片")
        _p, _n, status = redact_faces(self.image)
        if not cap["available"]:
            self.assertEqual(status, "unavailable",
                             "检测器不可用时必须报 unavailable，而不是伪装成 none")
        else:
            self.assertIn(status, ("ok", "none"))

    def test_status_reports_reason_when_unavailable(self):
        cap = face_redaction_status()
        if cap["available"]:
            self.assertIn(cap["backend"], ("haar", "yunet"))
        else:
            self.assertTrue(cap["reason"], "不可用时必须给出可操作的原因")
            self.assertIn("cv2_version", cap)

    def test_compliance_report_is_honest(self):
        rep = compliance_report()
        fr = rep["face_redaction"]
        cap = face_redaction_status()
        # 报告里的 enabled 必须等于真实能力，不能写死 True
        self.assertEqual(fr["enabled"], cap["available"],
                         "合规报告不得谎报脱敏已启用")
        self.assertEqual(fr["available"], cap["available"])
        if not cap["available"]:
            self.assertTrue(fr["degraded_mode"])
            self.assertTrue(fr["degraded_note"])
            self.assertTrue(fr["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
