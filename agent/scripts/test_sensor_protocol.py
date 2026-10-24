"""Sensor failures, freshness and batch association, without hardware or network."""
import copy
import json
import math
import unittest
from unittest.mock import patch

from tools.sensor_protocol import SensorState, normalize_units
from tools.sensor_reader import SensorReader, _find_port


def imu(seq=1, boot="A", us=1000000, rows=None):
    return {"v":2,"type":"imu","fw":"2.0.0","boot":boot,"seq":seq,
            "unit":"m/s2","gyro_unit":"deg/s",
            "samples":rows or [[us,0,0,9.807,0,0,0,True]]}


def env(seq=2, boot="A", us=1001000, valid=True):
    return {"v":2,"type":"env","fw":"2.0.0","boot":boot,"seq":seq,
            "t_us":us,"temperature_unit":"degC","humidity_unit":"percentRH",
            "t":24.2,"h":70.1,"valid":valid}


class SensorProtocolTest(unittest.TestCase):
    def setUp(self):
        self.state=SensorState()

    def both(self):
        self.state.ingest(imu(),100,10)
        self.state.ingest(env(),100.01,10.01)
        return self.state.snapshot(10.02)

    def test_si_and_original_observation_times_survive_join(self):
        out=self.both()
        self.assertTrue(out['fresh'])
        self.assertEqual(out['received_at'],100)
        self.assertEqual(out['env_received_at'],100.01)
        self.assertEqual(out['az'],9.807)
        self.assertFalse(out['hardware_synchronized'])

    def test_missing_environment_does_not_invent_temperature(self):
        self.state.ingest(imu(),100,10)
        out=self.state.snapshot(10)
        self.assertTrue(out['imu_valid'])
        self.assertFalse(out['fresh'])
        self.assertIsNone(out['t'])

    def test_failed_environment_replaces_previous_good_values(self):
        self.both()
        self.state.ingest(env(3,us=1010000,valid=False),100.1,10.1)
        out=self.state.snapshot(10.1)
        self.assertIsNone(out['t'])
        self.assertIsNone(out['h'])
        self.assertTrue(out['imu_valid'])

    def test_failed_imu_is_null_not_zero(self):
        self.both()
        self.state.ingest(imu(3,us=1020000,rows=[[1020000,None,None,None,None,None,None,False]]),100.1,10.1)
        out=self.state.snapshot(10.1)
        self.assertFalse(out['fresh'])
        self.assertIsNone(out['ax'])
        self.assertEqual(out['stats']['invalid_imu'],1)

    def test_monotonic_clock_expires_values_without_wall_clock_dependency(self):
        self.both()
        self.assertIsNone(self.state.snapshot(11)['ax'])
        self.assertIsNone(self.state.snapshot(16)['t'])

    def test_old_device_environment_is_not_relabelled_by_new_packets(self):
        self.both()
        self.state.ingest(imu(3,us=8000000),100.1,10.1)
        out=self.state.snapshot(10.1)
        self.assertTrue(out['imu_valid'])
        self.assertFalse(out['env_valid'])

    def test_new_boot_cannot_reuse_environment_from_old_boot(self):
        self.both()
        self.state.ingest(imu(0,boot='B',us=1000),100.1,10.1)
        out=self.state.snapshot(10.1)
        self.assertIsNone(out['t'])
        self.assertEqual(out['stats']['boot_changes'],1)

    def test_disconnect_reset_clears_cached_readings(self):
        self.both()
        self.state.reset()
        self.assertIsNone(self.state.snapshot(10.1))

    def test_packet_gaps_are_counted(self):
        self.both()
        self.state.ingest(imu(5,us=1020000),100.1,10.1)
        self.assertEqual(self.state.stats['sequence_gaps'],2)

    def test_sequence_replay_is_rejected_even_with_new_timestamp(self):
        self.both()
        with self.assertRaises(ValueError):
            self.state.ingest(imu(1,us=1030000),100.1,10.1)

    def test_backward_device_time_rejected_without_partial_batch_commit(self):
        self.both()
        with self.assertRaises(ValueError):
            self.state.ingest(imu(3,rows=[[1020000,0,0,9.8,0,0,0,True],[1010000,0,0,9.8,0,0,0,True]]),100.1,10.1)
        self.assertEqual(self.state.imu['us'],1000000)
        self.assertEqual(self.state.seq,2)

    def test_wrong_units_rejected_for_v2(self):
        packet=imu();packet['unit']='g'
        with self.assertRaises(ValueError):self.state.ingest(packet,100,10)

    def test_nonfinite_and_boolean_values_do_not_become_valid(self):
        for bad in (math.nan,math.inf,True,'bad'):
            packet=imu();packet['samples'][0][1]=bad
            self.state=SensorState();self.state.ingest(packet,100,10)
            self.assertIsNone(self.state.snapshot(10)['ax'])

    def test_out_of_range_temperature_and_humidity_are_null(self):
        for key,value in (('t',1000),('h',-1),('t',True)):
            packet=env();packet[key]=value
            self.state=SensorState();self.state.ingest(packet,100,10)
            self.assertIsNone(self.state.snapshot(10)[key])

    def test_out_of_range_imu_is_invalid(self):
        packet=imu();packet['samples'][0][1]=1000
        self.state.ingest(packet,100,10)
        self.assertIsNone(self.state.snapshot(10)['ax'])

    def test_static_gravity_does_not_create_ac_vibration(self):
        rows=[[1000000+i*10000,0,0,9.807,0,0,0,True] for i in range(20)]
        self.state.ingest(imu(rows=rows),100,10)
        out=self.state.snapshot(10)
        self.assertAlmostEqual(out['imu_window']['ac_rms_m_s2'],0,places=10)
        self.assertAlmostEqual(out['imu_window']['effective_hz'],100)
        self.assertEqual(out['imu_window']['max_gap_ms'],10)

    def test_rms_for_alternating_signal_is_computed_in_si_units(self):
        rows=[[1000000+i*10000,(-1)**i,0,9.807,0,0,0,True] for i in range(20)]
        self.state.ingest(imu(rows=rows),100,10)
        self.assertAlmostEqual(self.state.snapshot(10)['imu_window']['ac_rms_m_s2'],1)

    def test_legacy_unit_provenance_and_unknown_unit_are_not_hidden(self):
        values={'t':24,'h':60,'ax':0,'ay':0,'az':1}
        self.assertAlmostEqual(normalize_units({**values,'unit':'g'})['az'],9.80665)
        self.assertEqual(normalize_units(values)['unit_source'],'inferred')
        self.assertEqual(normalize_units({**values,'unit':'weird'})['unit'],'unknown')

    def test_legacy_cannot_override_verified_v2_stream(self):
        self.both()
        with self.assertRaises(ValueError):
            self.state.ingest({'t':0,'h':0,'ax':0,'ay':0,'az':0},101,11)

    def test_malformed_objects_and_versions_are_rejected(self):
        for packet in ([],{}, {'v':True}, {'v':3}, {'v':2,'boot':'A','seq':True}):
            with self.assertRaises(ValueError):self.state.ingest(packet,100,10)

    def test_serial_recorder_keeps_invalid_raw_json_and_cursor_loss(self):
        reader=SensorReader()
        with reader.condition:
            reader._record('{"t":NaN}',100,10)
            reader._record(json.dumps(imu()),100.1,10.1)
        packets,lost=reader.packets_since(0)
        self.assertEqual(lost,0)
        self.assertFalse(packets[0]['accepted'])
        self.assertTrue(packets[1]['accepted'])
        self.assertEqual(reader.state.stats['invalid'],1)

    def test_multiple_serial_adapters_require_explicit_selection(self):
        from types import SimpleNamespace
        ports=[SimpleNamespace(description='CH340',manufacturer=None,device=p) for p in ('COM1','COM2')]
        with patch.dict('os.environ',{'EQUIS_SENSOR_PORT':''}),patch('serial.tools.list_ports.comports',return_value=ports):
            with self.assertRaises(ValueError):_find_port()

    def test_explicit_port_takes_precedence(self):
        with patch.dict('os.environ',{'EQUIS_SENSOR_PORT':'COM10'}):self.assertEqual(_find_port(),'COM10')

    def test_bus_preserves_sensor_time_and_rejects_stale_snapshot(self):
        from perception.bus import from_sensor
        out=self.both()
        with patch('tools.sensor_reader.read_sensors_raw',return_value=out):
            percept=from_sensor()
        self.assertEqual(percept.ts,100)
        self.assertEqual(percept.payload['boot_id'],'A')
        self.assertEqual(percept.payload['env_received_at'],100.01)
        out['fresh']=False
        with patch('tools.sensor_reader.read_sensors_raw',return_value=out):self.assertIsNone(from_sensor())


if __name__ == '__main__':unittest.main()
