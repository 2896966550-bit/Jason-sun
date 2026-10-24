"""Class binding and single-event observation regressions; all inference is mocked."""
import copy
import json
import math
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
from perception import measurement_context as M
from agent.omni_ad import run_inference as R


class ClassBindingTest(unittest.TestCase):
    def setUp(self):
        self.frame = np.zeros((4,4,3),dtype=np.uint8)
        bank = SimpleNamespace(prototypes={"bottle":None,"screw":None},top_k=5,temperature=0.1)
        bank.has_class = lambda name:name in bank.prototypes
        self.predictor = SimpleNamespace(memory_bank=bank,predict_numpy=Mock(return_value={"image_score":-8.123456789}))

    def test_missing_class_never_initializes_model(self):
        with patch.object(R,"init_predictor",side_effect=AssertionError("Unexpected model load")):
            for name in (None,"","default"):
                out = R.detect_anomaly(self.frame,class_name=name)
                self.assertEqual(out["execution_status"],"needs_input")
                self.assertIsNone(out["anomaly_score"])
                self.assertIsNone(out["is_anomaly"])

    def test_unknown_class_never_falls_back_to_bottle(self):
        with patch.multiple(R,_predictor=self.predictor,_initialized=True):
            out = R.detect_anomaly(self.frame,class_name="gear")
        self.assertEqual(out["execution_status"],"unsupported")
        self.assertIsNone(out["class_used"])
        self.assertIsNone(out["anomaly_score"])
        self.predictor.predict_numpy.assert_not_called()

    def test_supported_class_and_raw_score_are_preserved(self):
        with patch.multiple(R,_predictor=self.predictor,_initialized=True),patch.object(R,"_get_threshold",return_value=-4):
            out = R.detect_anomaly(self.frame,class_name="screw")
        self.assertTrue(out["success"])
        self.assertEqual(out["class_used"],"screw")
        self.assertEqual(self.predictor.predict_numpy.call_args.kwargs["class_name"],"screw")
        self.assertEqual(out["anomaly_score"],-8.123456789)
        self.assertEqual(out["score_protocol"]["temperature"],0.1)
        self.assertNotIn("表面质量正常",out["verdict"])

    def test_unvalidated_cutoff_cannot_produce_binary_decision(self):
        for score in (-8.0,0.0):
            self.predictor.predict_numpy.return_value={"image_score":score}
            with patch.multiple(R,_predictor=self.predictor,_initialized=True),patch.object(R,"_get_threshold",return_value=-4):
                result=R.detect_anomaly(self.frame,"screw")
            self.assertTrue(result["success"])
            self.assertEqual(result["anomaly_score"],score)
            self.assertIsNone(result["is_anomaly"])
            self.assertIsNone(result["threshold"])
            self.assertEqual(result["decision_execution_status"],"needs_calibration")

    def test_invalid_images_do_not_initialize_model(self):
        with patch.object(R,"init_predictor",side_effect=AssertionError("Unexpected model load")):
            for image in (None,[],np.zeros((2,2)),np.zeros((2,2,3),dtype=np.float32)):
                self.assertEqual(R.detect_anomaly(image,"screw")["execution_status"],"invalid_input")

    def test_model_failure_never_means_normal_score(self):
        with patch.multiple(R,_initialized=False,_init_error="offline"),patch.object(R,"init_predictor",return_value=False):
            out = R.detect_anomaly(self.frame,"screw")
        self.assertFalse(out["success"])
        self.assertIsNone(out["anomaly_score"])
        self.assertIsNone(out["is_anomaly"])

    def test_nonfinite_model_scores_are_rejected(self):
        for value in (math.nan,math.inf,None,"bad"):
            with self.subTest(value=value),patch.multiple(R,_predictor=self.predictor,_initialized=True):
                self.predictor.predict_numpy.return_value={"image_score":value}
                out=R.detect_anomaly(self.frame,"screw")
                self.assertFalse(out["success"])
                self.assertIsNone(out["anomaly_score"])


class MeasurementContextTest(unittest.TestCase):
    def setUp(self):
        fixture = tempfile.TemporaryDirectory(prefix="equis_context_")
        self.addCleanup(fixture.cleanup)
        self.image = Path(fixture.name)/"fixture.png"
        import cv2
        ok, encoded = cv2.imencode(".png", np.zeros((8,8,3),dtype=np.uint8))
        self.assertTrue(ok)
        self.image.write_bytes(encoded.tobytes())
        self.ids={"class_id":"screw","specimen_id":"part-A","feature_id":"length"}
        self.reading={"t":22.3,"h":45.1,"ax":0.0,"ay":0.0,"az":9.81}

    def bound(self, *, image_type="camera", image_time=1000, sensor_time=1000, ids=None):
        context=M.new_context(self.ids if ids is None else ids,now=1000)
        ps=[{"type":image_type,"source":"upload" if image_type=="image" else "camera0",
             "ts":image_time,"payload":{"path":str(self.image)}},
            {"type":"sensor","source":"esp32","ts":sensor_time,
             "payload":{**self.reading,"unit":"m/s2","unit_source":"declared"}}]
        return M.bind_percepts(context,ps)

    def test_client_cannot_forge_bound_observations(self):
        with self.assertRaises(ValueError):
            M.new_context({"class_id":"screw","observations":{"sensor":self.reading}})

    def test_explicit_class_declaration_is_not_inferred_from_object_prose(self):
        self.assertIsNone(M.new_context(query="不要使用类别:bottle，检测这个螺丝")["class_id"])
        self.assertEqual(M.new_context(query="检测外观\n类别: screw")["class_id"],"screw")
        with self.assertRaises(ValueError):
            M.new_context({"class_id":"screw"},query="类别: bottle")

    def test_new_request_has_new_event_id(self):
        self.assertNotEqual(M.new_context()["event_id"],M.new_context()["event_id"])

    def test_live_host_alignment_does_not_claim_hardware_sync(self):
        ctx=self.bound()
        self.assertEqual(ctx["fusion_status"],"host_time_aligned")
        self.assertFalse(ctx["hardware_synchronized"])
        self.assertEqual(M.bound_sensor(ctx,require_alignment=True),self.reading)

    def test_uploaded_image_cannot_be_declared_synchronous_with_live_sensor(self):
        ctx=self.bound(image_type="image")
        self.assertEqual(ctx["fusion_status"],"unverified_upload_capture_time")
        self.assertIsNone(M.bound_sensor(ctx,require_alignment=True))

    def test_time_mismatch_stale_and_missing_identity_block_fusion(self):
        for ctx in (self.bound(sensor_time=1004),self.bound(image_time=900,sensor_time=900),self.bound(ids={"class_id":"screw"})):
            self.assertNotEqual(ctx["fusion_status"],"host_time_aligned")
            self.assertIsNone(M.bound_sensor(ctx,require_alignment=True))

    def test_inferred_sensor_units_do_not_authorize_physical_fusion(self):
        ctx=M.new_context(self.ids,now=1000)
        ps=[{"type":"camera","source":"camera0","ts":1000,"payload":{"path":str(self.image)}},
            {"type":"sensor","source":"esp32","ts":1000,
             "payload":{**self.reading,"unit":"m/s2","unit_source":"inferred"}}]
        ctx=M.bind_percepts(ctx,ps)
        self.assertEqual(ctx["fusion_status"],"unverified_sensor_units")
        self.assertIsNone(M.bound_sensor(ctx,require_alignment=True))

    def test_cross_event_observation_is_rejected(self):
        ctx=M.new_context(now=1000)
        with self.assertRaises(ValueError):
            M.bind_percepts(ctx,[{"type":"sensor","source":"esp32","ts":1000,
                                 "event_id":"another-event","payload":self.reading}])
        other=self.bound()
        other["observations"]["sensor"]["event_id"]="another-event"
        self.assertIsNone(M.bound_sensor(other))

    def test_missing_sensor_value_is_not_replaced_with_zero(self):
        ctx=self.bound()
        del ctx["observations"]["sensor"]["payload"]["ax"]
        self.assertIsNone(M.bound_sensor(ctx))

    def test_bound_image_hash_is_checked_before_inference(self):
        ctx=self.bound()
        ctx["observations"]["image"]["sha256"]="changed"
        with patch.object(R,"detect_anomaly",side_effect=AssertionError("Unexpected inference")):
            out=M.detect_bound_anomaly(ctx)
        self.assertEqual(out["error_code"],"image_changed")

    def test_bound_detection_uses_same_image_class_and_event(self):
        ctx=self.bound()
        with patch.object(R,"detect_anomaly",return_value={"success":True,"anomaly_score":-8}) as detect:
            out=M.detect_bound_anomaly(ctx)
        self.assertEqual(detect.call_args.kwargs["class_name"],"screw")
        self.assertEqual(out["event_id"],ctx["event_id"])
        self.assertEqual(out["image_sha256"],ctx["observations"]["image"]["sha256"])

    def test_coordinator_never_recaptures_camera_or_sensor(self):
        import coordinator
        ctx=self.bound(image_type="image")
        with patch("cv2.VideoCapture",side_effect=AssertionError("Camera reread")), \
             patch("tools.sensor_reader.read_sensors_raw",side_effect=AssertionError("Sensor reread")), \
             patch.object(coordinator,"_extract_params",return_value={"mv":100,"rv":100,"sp":0.01,"temp":20}):
            out=coordinator._run_duyuan("实测100mm，参考100mm，精度0.01mm，温度20℃，进行一致性判定",measurement_context=ctx)
        payload=json.loads(out.split("```compliance-card\n",1)[1].split("```",1)[0])
        self.assertFalse(payload["visual_used"])
        self.assertFalse(payload["sensor_used"])
        self.assertEqual(payload["event_id"],ctx["event_id"])
        self.assertEqual(payload["measurement_context"]["fusion_status"],"unverified_upload_capture_time")

    def test_planner_reads_bound_sensor_instead_of_defaults_or_device(self):
        from tools.task_planner import _execute_step
        ctx=self.bound()
        with patch("tools.sensor_reader.read_sensors_raw",side_effect=AssertionError("Sensor reread")):
            self.assertEqual(json.loads(_execute_step("read_sensors",{},ctx)),self.reading)
            self.assertIn("22.3",_execute_step("diag",{"t":999},ctx))
            self.assertIn("不可用",_execute_step("diag",{"t":25},None))

    def test_compute_node_passes_bound_context_to_executor(self):
        from graph import nodes
        ctx=self.bound()
        with patch("coordinator._run_duyuan",return_value="BOUND") as called,patch.object(nodes,"_trace",return_value=[]),patch.object(nodes,"_audit_branch"):
            out=nodes.node_compute({"plan":["compute"],"query":"计算","measurement_context":ctx})
        self.assertIs(called.call_args.kwargs["measurement_context"],ctx)
        self.assertEqual(out["compute_result"],"BOUND")

    def test_supervisor_does_not_probe_devices_after_collection(self):
        from graph import nodes
        decision={"intent":["vision"],"tool_calls":[],"need_camera":True,
                  "need_sensor":True,"direct_answer":False}
        with patch("agent_router.route",return_value=decision),patch("cv2.VideoCapture",side_effect=AssertionError("Camera reopened")), \
             patch("tools.sensor_reader._find_port",side_effect=AssertionError("Serial reprobed")),patch.object(nodes,"_trace",return_value=[]):
            out=nodes.node_supervisor({"query":"拍照","measurement_context":M.new_context(),"percepts":[]})
        self.assertIn("compute",out["plan"])

    def test_perception_keeps_processed_image_as_canonical(self):
        from graph import nodes
        from perception.bus import Percept
        ctx=M.new_context(self.ids,now=1000)
        p=Percept(type="image",source="upload",payload={"path":str(self.image)},ts=1000)
        with patch("perception.bus.collect",return_value=[p]),patch("compliance.gate.redact_faces",return_value=(str(self.image),0,"none")), \
             patch("tools.multimodal_tools.inspect_image",return_value="{}"),patch.object(nodes,"_trace",return_value=[]):
            out=nodes.node_perceive({"query":"分析图片","image_path":str(self.image),"measurement_context":ctx})
        self.assertEqual(out["image_path"],str(self.image))
        self.assertEqual(out["measurement_context"]["event_id"],ctx["event_id"])
        self.assertEqual(out["measurement_context"]["observations"]["image"]["payload"]["path"],str(self.image))

    def test_both_http_entries_reject_forged_context_before_execution(self):
        import server
        for endpoint in ("/api/chat","/api/chat/stream"):
            with self.subTest(endpoint=endpoint),patch.object(server,"local_chat") as called:
                response=server.app.test_client().post(endpoint,json={"message":"检测","measurement_context":{"observations":{}}})
                self.assertEqual(response.status_code,400)
                called.assert_not_called()

    def test_sync_http_passes_explicit_identity(self):
        import server
        with patch.object(server,"local_chat",return_value="OK") as called:
            response=server.app.test_client().post("/api/chat",json={"message":"检测","measurement_context":self.ids})
        self.assertEqual(response.status_code,200)
        self.assertEqual(called.call_args.kwargs["measurement_context"],self.ids)

    def test_stream_http_passes_explicit_identity_and_returns_event_metadata(self):
        import server
        def stream(*args,**kwargs):
            self.assertEqual(kwargs["measurement_context"],self.ids)
            yield {"final":"OK","run_id":123,"measurement_context":{"event_id":"bound-123"}}
        with patch("graph.runner.stream_run",side_effect=stream),patch("chat_history.get_recent_pairs",return_value=[]), \
             patch("chat_history.save_message"),patch.object(server,"audit_log"):
            response=server.app.test_client().post("/api/chat/stream",json={"message":"检测","measurement_context":self.ids})
        self.assertEqual(response.status_code,200)
        events=[json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines() if line.startswith("data: ")]
        self.assertTrue(any(e.get("run_meta",{}).get("measurement_context",{}).get("event_id")=="bound-123" for e in events))

    def test_sensor_unit_provenance_and_boolean_failure_are_preserved(self):
        from tools.sensor_reader import _normalize_units
        declared=_normalize_units({**self.reading,"unit":"g"})
        inferred=_normalize_units(self.reading)
        invalid=_normalize_units({**self.reading,"t":True,"unit":"m/s2"})
        self.assertEqual(declared["unit_source"],"declared")
        self.assertEqual(inferred["unit_source"],"inferred")
        self.assertIsNone(invalid["t"])


if __name__=="__main__":
    unittest.main()
