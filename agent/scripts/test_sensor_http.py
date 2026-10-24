"""Sensor monitor API must preserve invalid/stale values rather than fabricate zero."""
import math
import unittest
from unittest.mock import patch


class SensorHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        cls.client=server.app.test_client()

    def response(self,reading):
        with patch('tools.sensor_reader.read_sensors_raw',return_value=reading):
            return self.client.get('/api/sensors').json

    def valid(self):
        return {'t':25.1,'h':60.0,'ax':0.0,'ay':0.0,'az':9.80665,'fresh':True,
                'unit':'m/s2','unit_source':'declared','protocol_version':2,'firmware_version':'2.0.3'}

    def test_healthy_reading_retains_existing_ui_fields(self):
        result=self.response(self.valid())
        self.assertTrue(result['ok']);self.assertEqual(result['temp'],25.1)
        self.assertEqual(result['ax'],0.0);self.assertEqual(result['az'],9.807)
        self.assertEqual(result['firmware_version'],'2.0.3');self.assertFalse(result['hardware_synchronized'])

    def test_missing_values_are_null_and_not_success(self):
        result=self.response({'t':25.1,'fresh':True})
        self.assertFalse(result['ok']);self.assertIsNone(result['hum']);self.assertIsNone(result['ax'])

    def test_null_values_never_raise_round_type_error(self):
        result=self.response({**self.valid(),'t':None,'ax':None})
        self.assertFalse(result['ok']);self.assertIsNone(result['temp']);self.assertIsNone(result['ax'])

    def test_stale_cache_cannot_report_success(self):
        self.assertFalse(self.response({**self.valid(),'fresh':False})['ok'])

    def test_unknown_freshness_cannot_report_success(self):
        reading=self.valid();reading.pop('fresh')
        self.assertFalse(self.response(reading)['ok'])

    def test_nonfinite_and_boolean_values_are_not_serialized_as_measurements(self):
        for value in (math.nan,math.inf,True):
            result=self.response({**self.valid(),'t':value})
            self.assertFalse(result['ok']);self.assertIsNone(result['temp'])

    def test_disconnected_sensor_is_unavailable(self):
        result=self.response(None)
        self.assertFalse(result['ok']);self.assertEqual(result['execution_status'],'unavailable')


if __name__=='__main__':unittest.main()
