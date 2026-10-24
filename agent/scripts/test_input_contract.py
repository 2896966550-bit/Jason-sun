"""Regression tests for invalid inputs and assessment semantics; no devices/API."""
import json
import math
import unittest

from agent.omni_ad.uncertainty import compliance as C


class InputContractTest(unittest.TestCase):
    def make(self, **values):
        defaults = dict(measured_value=100.0, reference_value=100.0,
                        sensor_precision=0.01, env_temp=20.0)
        defaults.update(values)
        return C.ComplianceInput(**defaults)

    def test_nonfinite_inputs_never_produce_a_verdict(self):
        fields = ("measured_value", "reference_value", "sensor_precision", "env_temp",
                  "baseline_temp", "temp_coefficient", "visual_anomaly_score",
                  "sensor_anomaly_score", "visual_threshold", "sensor_threshold",
                  "beta_vis", "beta_sensor", "k", "guard_band_factor",
                  "critical_band_low", "critical_band_high", "ood_dominance_ratio")
        for field in fields:
            for value in (math.nan, math.inf, -math.inf):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    C.compute_compliance(self.make(**{field:value}))

    def test_negative_scales_and_invalid_policy_are_rejected(self):
        for field, value in (("sensor_precision", -0.01), ("beta_vis", -1),
                             ("beta_sensor", -1), ("temp_coefficient", -1),
                             ("k", 0), ("guard_band_factor", 0),
                             ("critical_band_low", -1), ("critical_band_high", 0),
                             ("ood_dominance_ratio", 0.5), ("ood_dominance_ratio", 10)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                C.compute_compliance(self.make(**{field:value}))

    def test_missing_measurement_has_structured_needs_input_status(self):
        with self.assertRaises(ValueError) as caught:
            C.compute_compliance(self.make(measured_value=None))
        payload = caught.exception.to_dict()
        self.assertEqual(payload["execution_status"], "needs_input")
        self.assertEqual(payload["error"]["field"], "measured_value")
        self.assertIsNone(payload["verdict"])
        self.assertIsNone(payload["in_spec"])
        json.dumps(payload, allow_nan=False)

    def test_missing_reference_is_not_a_comparison(self):
        with self.assertRaises(ValueError) as caught:
            C.compute_compliance(self.make(reference_value=None))
        self.assertEqual(caught.exception.to_dict()["execution_status"], "needs_input")

    def test_boolean_and_text_are_not_physical_numbers(self):
        for value in (True, False, "10 mm", "10.0"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                C.compute_compliance(self.make(measured_value=value))

    def test_zero_baseline_is_unavailable_instead_of_zero_U_pass(self):
        with self.assertRaises(ValueError) as caught:
            C.compute_compliance(self.make(sensor_precision=0, visual_anomaly_score=1))
        payload = caught.exception.to_dict()
        self.assertEqual(payload["execution_status"], "unavailable")
        self.assertEqual(payload["error"]["code"], "missing_uncertainty_baseline")
        self.assertIsNone(payload["verdict"])
        self.assertNotIn("U", payload)

    def test_direct_verdict_rejects_zero_and_nonfinite_baseline(self):
        for value in (0, -1, math.nan, math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                C.compute_verdict(deviation=0, u_c=0.1, u_c_base=value)

    def test_finite_inputs_that_overflow_cannot_escape_validation(self):
        with self.assertRaises(ValueError):
            C.compute_compliance(self.make(measured_value=1e308, env_temp=1e308))

    def test_anomaly_helpers_reject_nonfinite_values(self):
        with self.assertRaises(ValueError):
            C.map_anomaly(math.nan, -4)
        with self.assertRaises(ValueError):
            C.compute_u_ood(math.inf, 0, 0.001, 0.001)

    def test_valid_reference_comparison_preserves_numeric_baseline(self):
        result = C.compute_compliance(self.make())
        self.assertAlmostEqual(result.U, 2*0.01/math.sqrt(3), places=14)
        self.assertEqual(result.verdict, "pass")
        payload = C.result_to_dict(result)
        self.assertEqual(payload["execution_status"], "success")
        self.assertEqual(payload["assessment_type"], "reference_consistency")
        self.assertEqual(payload["confidence_kind"], "heuristic")
        self.assertFalse(payload["ood_mapping_validated"])
        json.dumps(payload, allow_nan=False)

    def test_uncertainty_only_does_not_fabricate_a_reference_verdict(self):
        result = C.compute_compliance(self.make(reference_value=None,
                                                assessment_type="uncertainty_only",
                                                include_u_ood=False))
        payload = C.result_to_dict(result)
        self.assertEqual(payload["execution_status"], "success")
        self.assertEqual(payload["assessment_type"], "uncertainty_only")
        self.assertIsNone(payload["deviation"])
        self.assertIsNone(payload["verdict"])
        self.assertIsNone(payload["in_spec"])
        self.assertIsNone(payload["confidence"])
        json.dumps(payload, allow_nan=False)

    def test_unimplemented_product_conformity_is_rejected(self):
        with self.assertRaises(ValueError):
            C.compute_compliance(self.make(assessment_type="product_conformity"))

    def test_uncertainty_text_calls_uncertainty_only_and_labels_B_correctly(self):
        from unittest.mock import patch
        from agent.tools.measurement_tools import estimate_uncertainty
        with patch("agent.omni_ad.uncertainty.compute_compliance", wraps=C.compute_compliance) as called:
            output = estimate_uncertainty(100, 0.01, env_temp=20)
        used = called.call_args.args[0]
        self.assertEqual(used.assessment_type, "uncertainty_only")
        self.assertIsNone(used.reference_value)
        self.assertNotIn("A类（传感器", output)
        self.assertIn("B类", output)

    def test_uncertainty_only_never_calls_verdict_function(self):
        from unittest.mock import patch
        with patch.object(C, "compute_verdict", side_effect=AssertionError("No artificial comparison")):
            C.compute_compliance(self.make(reference_value=None,
                                            assessment_type="uncertainty_only"))


if __name__ == "__main__":
    unittest.main()
