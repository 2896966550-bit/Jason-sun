"""Regression tests for grounded routing, units, statuses, merging and trace binding."""
import json
import math
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.measurement_executor import execute_request, execute_tool, extract_grounded
from tools.cards import extract_cards, attach_run_metadata


def payload(text):
    cards,_=extract_cards(text)
    return cards[0]["data"]


class MeasurementExecutionTest(unittest.TestCase):
    def test_numeric_verdict_never_calls_camera_or_parameter_LLM(self):
        import coordinator
        with patch("cv2.VideoCapture",side_effect=AssertionError("Camera called")), \
             patch.object(coordinator,"_extract_params",side_effect=AssertionError("LLM extraction")):
            data=payload(coordinator._run_duyuan("实测10.014mm，参考10mm，精度0.01mm，温度20℃，判合格吗？"))
        self.assertEqual(data["assessment_type"],"reference_consistency")
        self.assertEqual(data["verdict"],"fail")
        self.assertEqual(data["measured_value"],10.014)

    def test_missing_input_does_not_create_default_measurement(self):
        data=payload(execute_request("进行合规判定"))
        self.assertEqual(data["execution_status"],"needs_input")
        self.assertIsNone(data["verdict"])
        self.assertNotIn("measured_value",data)
        self.assertNotIn("U",data)

    def test_temperature_is_required_when_no_bound_snapshot_exists(self):
        data=payload(execute_request("实测100mm 参考100mm 精度0.01mm 判定"))
        self.assertEqual(data["execution_status"],"needs_input")
        self.assertIn("环境温度",data["error"]["message"])

    def test_critical_verdict_survives_legacy_true_in_spec(self):
        data=payload(execute_request("实测10.010mm，参考10mm，精度0.01mm，温度20℃，判定"))
        self.assertEqual(data["verdict"],"indeterminate")
        self.assertTrue(data["in_spec"])

    def test_units_are_converted_and_source_is_recorded(self):
        data=payload(execute_request("实测1000微米，参考1mm，精度10um，温度68°F，判定"))
        self.assertEqual(data["execution_status"],"success")
        self.assertEqual(data["measured_value"],1.0)
        self.assertAlmostEqual(data["u_a"],0.01/math.sqrt(3))
        self.assertEqual(data["input_provenance"]["mv"]["unit"],"mm")
        self.assertEqual(data["input_provenance"]["temp"]["source"],"user_text")

    def test_invalid_units_and_conflicts_cannot_be_ignored(self):
        queries=["实测10公里，精度0.01mm，温度20℃，算不确定度",
                 "实测10mm/s，精度0.01mm，温度20℃，算不确定度",
                 "实测10m²，精度0.01mm，温度20℃，算不确定度",
                 "实测10mm，精度0.01mm，温度20°K，算不确定度",
                 "实测10mm，实测11mm，精度0.01mm，温度20℃，算不确定度"]
        for query in queries:
            with self.subTest(query=query):
                self.assertEqual(payload(execute_request(query))["execution_status"],"invalid_input")

    def test_repeated_measurements_are_not_silently_truncated(self):
        data=payload(execute_request("实测10mm、10.01mm、10.02mm，精度0.01mm，温度20℃，算不确定度"))
        self.assertEqual(data["execution_status"],"unsupported")

    def test_nonfinite_and_zero_baseline_return_structured_failures(self):
        for query,status in (("实测NaNmm，参考10mm，精度0.01mm，温度20℃，判定","invalid_input"),
                             ("实测10mm，参考10mm，精度0mm，温度20℃，判定","unavailable")):
            data=payload(execute_request(query))
            self.assertEqual(data["execution_status"],status)
            self.assertIsNone(data["verdict"])

    def test_router_arguments_are_consumed_after_grounding(self):
        decision={"tool_calls":[{"name":"calc_uncertainty","args":{"mv":10,"sp":0.01,"temp":20}}]}
        data=payload(execute_request("实测10mm，精度0.01mm，温度20℃，算不确定度",router_decision=decision))
        self.assertEqual(data["execution_status"],"success")
        self.assertTrue(data["input_provenance"]["mv"]["route_argument_verified"])

    def test_hallucinated_router_value_is_rejected(self):
        decision={"tool_calls":[{"name":"calc_uncertainty","args":{"mv":100}}]}
        data=payload(execute_request("实测10mm，精度0.01mm，温度20℃，算不确定度",router_decision=decision))
        self.assertEqual(data["execution_status"],"invalid_input")
        self.assertIn("原文不一致",data["error"]["message"])

    def test_uncertainty_only_never_returns_a_pass_verdict(self):
        data=payload(execute_request("实测10mm，精度0.01mm，温度20℃，算不确定度"))
        self.assertIsNone(data["verdict"])
        self.assertIsNone(data["reference_value"])
        self.assertEqual(data["ood_policy"],"disabled_unvalidated_mapping")
        self.assertEqual(data["u_ood"],0)

    def test_product_conformity_is_not_replaced_by_consistency(self):
        data=payload(execute_request("实测10mm，参考10mm，精度0.01mm，温度20℃，公差0.1mm，判合格"))
        self.assertEqual(data["execution_status"],"unsupported")
        self.assertIsNone(data["verdict"])

    def test_sensor_diagnosis_does_not_invent_missing_accelerations(self):
        data=payload(execute_request("诊断传感器：温度25℃ 湿度50%"))
        self.assertEqual(data["execution_status"],"needs_input")
        self.assertNotIn("az",data)

    def test_complete_user_sensor_vector_keeps_user_provenance(self):
        text=execute_request("诊断传感器：温度25℃ 湿度50% 加速度0.3 -0.1 9.8m/s2")
        data=payload(text)
        self.assertEqual(data["source"],"user_text")
        self.assertEqual(data["execution_status"],"success")

    def test_calc_does_not_execute_incidental_sensor_tool(self):
        decision={"tool_calls":[{"name":"read_sensors","args":{}},{"name":"calc_uncertainty","args":{}}]}
        text=execute_request("实测10mm，精度0.01mm，温度20℃，算不确定度",router_decision=decision)
        cards,_=extract_cards(text)
        self.assertEqual(len(cards),1)
        self.assertEqual(cards[0]["data"]["execution_status"],"success")

    def test_tiny_uncertainty_is_not_serialized_as_zero(self):
        data=payload(execute_request("实测10mm，精度0.00000001mm，温度20℃，算不确定度"))
        self.assertGreater(data["U"],0)
        self.assertLess(data["U"],1e-6)

    def test_explicit_material_is_applied_instead_of_steel_default(self):
        data=payload(execute_request("材料：铝，实测100mm，精度0.01mm，温度25℃，算不确定度"))
        self.assertAlmostEqual(data["temp_coefficient"],23e-6)

    def test_three_dimensional_task_requires_actual_geometry_inputs(self):
        missing=payload(execute_request("视差45px，基线150mm，算深度"))
        self.assertEqual(missing["execution_status"],"needs_input")
        full=execute_request("视差45px，基线150mm，焦距500px，算深度")
        self.assertIn("1666.67",full)

    def test_direct_sensor_tool_rejects_nan_instead_of_saying_normal(self):
        from tools.measurement_tools import sensor_data_diagnose
        with self.assertRaises(ValueError):
            sensor_data_diagnose(math.nan,50,0,0,9.8)

    def test_planner_numeric_step_cannot_invent_measurement_values(self):
        from tools.task_planner import _execute_step
        output=_execute_step("calc_uncertainty",{"mv":100,"sp":0.01,"temp":25},query="帮我测量这个工件")
        self.assertEqual(payload(output)["execution_status"],"invalid_input")
        valid=_execute_step("calc_uncertainty",{"mv":10,"sp":0.01,"temp":20},query="实测10mm，精度0.01mm，温度20℃，算不确定度")
        self.assertEqual(payload(valid)["execution_status"],"success")

    def test_rule_fallback_never_uses_positional_numbers_or_fake_temperature(self):
        from agent_core import _rule_based_route
        text=_rule_based_route("算误差10 0.01")
        self.assertEqual(payload(text)["execution_status"],"needs_input")
        data=payload(_rule_based_route("实测10mm，精度0.01mm，算不确定度"))
        self.assertEqual(data["execution_status"],"needs_input")

    def test_plan_keeps_complete_structured_results(self):
        import tools.task_planner as planner
        query="实测10mm，精度0.01mm，温度20℃，算不确定度"
        plan=[{"action":"calc_uncertainty","params":{},"reason":"计算"}]
        with patch.object(planner,"_llm_plan",return_value=plan):
            output=planner.plan_measurement(query)
        self.assertEqual(payload(output)["execution_status"],"success")

    def test_router_history_cache_distinguishes_equal_length_context(self):
        import agent_router as router
        router.clear_cache()
        with patch.object(router,"ROUTER_ENABLED",True),patch.object(router,"LLM_API_KEY","test"),patch.object(router,"_audit_route"),patch.object(router,"_get_client") as client:
            def create(**kw):
                standard="JJG 21" if "JJG 21" in kw["messages"][-1]["content"] else "JJG 99"
                result={"intent":["kb"],"rewritten_query":standard,"tool_calls":[{"name":"kb_search","args":{"query":standard}}]}
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(result)))],usage=None)
            client.return_value.chat.completions.create.side_effect=create
            first=router.route("它适用于什么范围？",history=[{"role":"user","content":"JJG 21"}])
            second=router.route("它适用于什么范围？",history=[{"role":"user","content":"JJG 99"}])
        self.assertNotEqual(first["rewritten_query"],second["rewritten_query"])
        self.assertEqual(client.return_value.chat.completions.create.call_count,2)
        router.clear_cache()

    def test_parallel_vision_compute_plan_and_kb_are_all_preserved(self):
        import coordinator
        from graph import nodes
        def merge(query,results,agents,**kwargs):
            return "|".join(results.values())
        with patch.object(coordinator,"_llm_merge",side_effect=merge),patch.object(nodes,"_trace",return_value=[]):
            out=nodes.node_merge({"query":"并行任务","plan_result":"PLAN","vision_result":"VISION","compute_result":"COMPUTE","kb_result":"KB"})
        for part in ("PLAN","VISION","COMPUTE","KB"):
            self.assertIn(part,out["final"])

    def test_calculation_cards_are_not_reinterpreted_by_merge_llm(self):
        import coordinator
        from graph import nodes
        card=execute_request("实测10mm，参考10mm，精度0.01mm，温度20℃，判定")
        with patch.object(coordinator,"_llm_merge",side_effect=AssertionError("LLM rewrote verdict")),patch.object(nodes,"_trace",return_value=[]):
            out=nodes.node_merge({"query":"测量","compute_result":card,"kb_result":"标准摘要"})
        self.assertEqual(payload(out["final"])["verdict"],"pass")
        self.assertIn("标准摘要",out["final"])

    def test_card_run_metadata_is_bound_and_cross_event_result_is_invalidated(self):
        card=execute_request("实测10mm，参考10mm，精度0.01mm，温度20℃，判定")
        data=payload(attach_run_metadata(card,123,"event-A"))
        self.assertEqual(data["run_id"],123)
        self.assertEqual(data["event_id"],"event-A")
        wrong=payload(attach_run_metadata(attach_run_metadata(card,123,"event-A"),124,"event-B"))
        self.assertEqual(wrong["execution_status"],"invalid_input")
        self.assertIsNone(wrong["verdict"])

    def test_full_graph_numeric_request_overrides_wrong_visual_route(self):
        import coordinator
        import graph.build as build
        from graph.runner import run
        from perception.bus import from_text
        query="实测10.010mm，参考10mm，精度0.01mm，温度20℃，判合格吗？"
        decision={"intent":["vision"],"tool_calls":[{"name":"anomaly_detect","args":{}}],
                  "need_camera":True,"need_sensor":False,"direct_answer":False}
        with patch("agent_router.route",return_value=decision),patch("perception.bus.collect",return_value=[from_text(query)]), \
             patch.object(coordinator,"_llm_chat",side_effect=AssertionError("LLM rewrote calculation")), \
             patch("cv2.VideoCapture",side_effect=AssertionError("Unexpected camera")):
            build._graph=None
            output=run(query,history=[])
        data=payload(output)
        self.assertEqual(data["verdict"],"indeterminate")
        self.assertIsInstance(data["run_id"],int)
        self.assertTrue(data["event_id"])


if __name__=="__main__":
    unittest.main()
