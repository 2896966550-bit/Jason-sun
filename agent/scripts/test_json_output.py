"""Model-output shape and tolerant parsing contracts; no services/devices."""
import unittest
from tools.json_output import parse_json_object


class JsonOutputTest(unittest.TestCase):
    def test_plain_and_fenced_objects(self):
        for text in ('{"a":1}', '```json\n{"a":1}\n```', '```JSON\n{"a":1}\n```'):
            with self.subTest(text=text):
                self.assertEqual(parse_json_object(text), {"a": 1})

    def test_prose_and_multiple_objects_keep_first_complete_object(self):
        self.assertEqual(parse_json_object('说明 {"a":"} inside { a string"} 结束 {"b":2}'),
                         {"a": "} inside { a string"})

    def test_nested_object_is_preserved(self):
        self.assertEqual(parse_json_object('结果 {"a":{"b":[1,2]}}。'), {"a":{"b":[1,2]}})

    def test_non_objects_and_invalid_types_are_rejected(self):
        for text in (None, {}, [], 3, '', 'not JSON', '[]', '[{"a":1}]', 'true', '42', '"text"', 'null'):
            with self.subTest(text=text):
                self.assertEqual(parse_json_object(text), {})

    def test_three_consumers_use_same_parser(self):
        from agent_router import _parse_json_loose as router
        from tools.multimodal_tools import _parse_json_loose as vision
        from tools.task_planner import _parse_json_loose as planner
        self.assertIs(router, parse_json_object)
        self.assertIs(vision, parse_json_object)
        self.assertIs(planner, parse_json_object)
