"""Capture integrity and geometry regressions. All images/points are synthetic fixtures."""
import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import cv2
import numpy as np
from perception.camera_devices import resolve_device
from perception.stereo_capture import validate_config,eligible_pair,configure_camera,StereoCapture
from perception.capture_store import CaptureStore,validate_session
from perception.stereo_calibration import validate_board,fit_points,calibrate_session
from scripts.validate_measurement_dataset import validate as validate_dataset,REQUIRED
from scripts import calibrate_beta


def config():return {'left_device_id':'left','right_device_id':'right','width':64,'height':48,'fps':30,'max_skew_ms':40,'max_age_ms':500,'settings':{}}
def pair():
    frame=np.zeros((48,64,3),np.uint8)
    return {'left':{'frame':frame,'sequence':1,'received_at':100,'received_monotonic':10},
            'right':{'frame':frame+1,'sequence':1,'received_at':100.01,'received_monotonic':10.01},'host_skew_ms':10.0}


class CaptureWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'session'
        self.cfg={'capture':config(),'devices':{}}

    def test_same_camera_cannot_masquerade_as_stereo(self):
        c=config();c['right_device_id']='left'
        with self.assertRaises(ValueError):validate_config(c)

    def test_missing_device_identity_cannot_fall_back_to_index(self):
        for key in ('left_device_id','right_device_id'):
            c=config();c[key]=None
            with self.assertRaises(ValueError):validate_config(c)
        with self.assertRaises(ValueError):resolve_device('missing',[{'device_id':'left','index':0}])

    def test_identity_is_resolved_again_after_reindex(self):
        self.assertEqual(resolve_device('left',[{'device_id':'right','index':0},{'device_id':'left','index':1}]),1)

    def test_bad_size_timing_and_nonfinite_settings_rejected(self):
        for key,value in (('width',True),('height',0),('fps',float('nan')),('max_age_ms',-1)):
            c=config();c[key]=value
            with self.assertRaises(ValueError):validate_config(c)
        c=config();c['settings']={'exposure':float('inf')}
        with self.assertRaises(ValueError):validate_config(c)

    def test_stale_repeated_and_skewed_pairs_rejected(self):
        p=pair();c=config()
        self.assertIsNone(eligible_pair(p['left'],p['right'],10.02,(0,0),c))
        self.assertEqual(eligible_pair(p['left'],p['right'],11,(0,0),c),'stale_frame')
        self.assertEqual(eligible_pair(p['left'],p['right'],10.02,(1,1),c),'repeated_frame')
        p['right']['received_monotonic']=10.1
        self.assertEqual(eligible_pair(p['left'],p['right'],10.11,(0,0),c),'host_time_mismatch')

    def test_future_frame_is_rejected(self):
        p=pair()
        self.assertEqual(eligible_pair(p['left'],p['right'],10.005,(0,0),config()),'stale_frame')

    def test_failed_control_is_reported_unverified(self):
        class Cap:
            def set(self,*args):return False
            def get(self,*args):return -1
            def getBackendName(self):return 'DSHOW'
        c=config();c['settings']={'exposure':-5}
        result=configure_camera(Cap(),c)
        self.assertIsNone(result['fps_reported'])
        self.assertEqual(result['settings']['exposure']['status'],'unverified')

    def test_missing_second_device_fails_before_opening_first(self):
        called=[]
        capture=StereoCapture(config(),inventory=lambda:[{'device_id':'left','index':0}],stream_factory=lambda *args:called.append(args))
        with self.assertRaises(ValueError):capture.start()
        self.assertEqual(called,[])

    def test_lossless_commit_and_replay(self):
        store=CaptureStore(self.root,self.cfg);event=store.save(pair(),purpose='diagnostic')
        report=validate_session(self.root)
        self.assertTrue(report['valid']);self.assertEqual(report['committed_events'],1)
        self.assertFalse(event['hardware_synchronized'])
        self.assertEqual(cv2.imread(str(self.root/'events'/event['event_id']/'right.png'))[0,0,0],1)

    def test_hash_change_invalidates_record(self):
        store=CaptureStore(self.root,self.cfg);event=store.save(pair(),purpose='diagnostic')
        (self.root/'events'/event['event_id']/'left.png').write_bytes(b'bad')
        self.assertFalse(validate_session(self.root)['valid'])

    def test_partial_event_is_visible_and_resume_quarantines_without_deletion(self):
        store=CaptureStore(self.root,self.cfg)
        partial=self.root/'events'/'.pending-interrupted';partial.mkdir();(partial/'left.png').write_bytes(b'partial')
        self.assertFalse(validate_session(self.root)['valid'])
        CaptureStore(self.root,self.cfg,resume=True)
        report=validate_session(self.root)
        self.assertTrue(report['valid']);self.assertEqual(len(report['quarantined_partial_events']),1)
        preserved=next((self.root/'quarantine').iterdir())/'left.png'
        self.assertEqual(preserved.read_bytes(),b'partial')

    def test_png_write_failure_cannot_publish_partial_event(self):
        store=CaptureStore(self.root,self.cfg)
        with patch('cv2.imencode',side_effect=OSError('disk_failure')):
            with self.assertRaises(OSError):store.save(pair(),purpose='diagnostic')
        report=validate_session(self.root)
        self.assertEqual(report['committed_events'],0);self.assertTrue(report['pending_directories'])

    def test_existing_session_cannot_be_overwritten(self):
        CaptureStore(self.root,self.cfg)
        with self.assertRaises(FileExistsError):CaptureStore(self.root,self.cfg)

    def test_resume_rejects_config_change_and_closed_session(self):
        store=CaptureStore(self.root,self.cfg);changed=copy.deepcopy(self.cfg);changed['capture']['width']=100
        with self.assertRaises(ValueError):CaptureStore(self.root,changed,resume=True)
        store.close({'completed':True})
        with self.assertRaises(ValueError):CaptureStore(self.root,self.cfg,resume=True)

    def test_replay_rejects_path_traversal(self):
        store=CaptureStore(self.root,self.cfg);event=store.save(pair(),purpose='diagnostic')
        f=self.root/'events'/event['event_id']/'event.json';doc=json.loads(f.read_text());doc['images']['left']['file']='../../outside.png';f.write_text(json.dumps(doc))
        self.assertFalse(validate_session(self.root)['valid'])

    def test_missing_board_scale_or_source_cannot_generate_mm(self):
        for board in ({'columns':11,'rows':8},{'columns':11,'rows':8,'square_size_mm':10},
                      {'columns':11,'rows':8,'square_size_mm':True,'size_source':'test'}):
            with self.assertRaises(ValueError):validate_board(board)

    def test_insufficient_real_images_cannot_create_calibration(self):
        CaptureStore(self.root,self.cfg)
        with self.assertRaises(ValueError):calibrate_session(self.root,{'columns':11,'rows':8,'square_size_mm':10,'size_source':'synthetic test'},self.root/'calibration.json')
        self.assertFalse((self.root/'calibration.json').exists())

    def test_legacy_beta_writeback_and_implicit_defaults_are_blocked(self):
        target=Path(self.tmp.name)/'config.yaml';target.write_text('beta_vis: 1\n')
        with self.assertRaises(RuntimeError):calibrate_beta.write_beta_to_yaml(2,str(target))
        for args in ([],['--demo'],['--research-only','--demo'],['--write','--yaml',str(target)]):
            with self.assertRaises(SystemExit):calibrate_beta.main(args)
        self.assertEqual(target.read_text(),'beta_vis: 1\n')

    def dataset(self,rows):
        path=Path(self.tmp.name)/'data.csv'
        with path.open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=REQUIRED);w.writeheader();w.writerows(rows)
        return path

    def row(self,**kw):return {'event_id':'event1','specimen_id':'part1','session_id':'session1','split':'development',
                             'measured_value_mm':10.01,'reference_value_mm':10,'u_ref_mm':0.001,'anomaly_score':-8,
                             'epipolar_p95_px':0.2,'roi_valid_fraction':0.95,**kw}

    def test_paired_data_residual_preserves_sign(self):
        result=validate_dataset(self.dataset([self.row(measured_value_mm=9.99)]))
        self.assertAlmostEqual(result['rows'][0]['signed_residual_mm'],-0.01)
        self.assertFalse(result['physical_mapping_validated'])

    def test_duplicate_events_or_specimen_leakage_rejected(self):
        for second in (self.row(),self.row(event_id='event2',split='test')):
            with self.assertRaises(ValueError):validate_dataset(self.dataset([self.row(),second]))

    def test_invalid_reference_uncertainty_and_nonfinite_values_rejected(self):
        for row in (self.row(u_ref_mm=-1),self.row(anomaly_score='NaN'),self.row(roi_valid_fraction=2)):
            with self.assertRaises(ValueError):validate_dataset(self.dataset([row]))

    def test_empty_template_is_not_a_dataset(self):
        with self.assertRaises(ValueError):validate_dataset(self.dataset([]))

    def test_static_tool_does_not_infer_stationarity_or_apply_offsets(self):
        from scripts.assess_sensor_static import assess
        path=Path(self.tmp.name)/'sensor.jsonl'
        packet={'v':2,'type':'imu','boot':'fixture','unit':'m/s2','gyro_unit':'deg/s',
                'samples':[[i*10000,0,0,9.81,1,2,3,True] for i in range(120)]}
        path.write_text(json.dumps({'accepted':True,'raw':json.dumps(packet)})+'\n')
        result=assess(path)
        self.assertIsNone(result['gyro_bias_candidate_deg_s']);self.assertFalse(result['correction_applied'])
        self.assertEqual(assess(path,True)['gyro_bias_candidate_deg_s'],[1,2,3])

    def test_static_tool_rejects_restart_mixed_with_one_recording(self):
        from scripts.assess_sensor_static import assess
        path=Path(self.tmp.name)/'sensor.jsonl';packets=[]
        for boot in ('A','B'):
            packet={'v':2,'type':'imu','boot':boot,'unit':'m/s2','gyro_unit':'deg/s',
                    'samples':[[i*10000,0,0,9.81,1,2,3,True] for i in range(120)]}
            packets.append({'accepted':True,'raw':json.dumps(packet)})
        path.write_text('\n'.join(json.dumps(r) for r in packets))
        with self.assertRaises(ValueError):assess(path)


class SyntheticGeometryTest(unittest.TestCase):
    def test_known_projected_rig_recovers_geometry_on_heldout_views(self):
        k=np.array([[900.,0,640],[0,900,360],[0,0,1]])
        d=np.zeros(5);grid=np.zeros((88,3),np.float32);grid[:,:2]=np.mgrid[0:11,0:8].T.reshape(-1,2)*10
        objects=[];left=[];right=[];validation=[]
        for i in range(22):
            rv=np.array([0.12*np.sin(i),0.18*np.cos(i*0.7),0.03*i]);tv=np.array([-50+4*np.sin(i),-30+5*np.cos(i),650+15*i],float)
            lp,_=cv2.projectPoints(grid,rv,tv,k,d);rp,_=cv2.projectPoints(grid,rv,tv+np.array([-120.,0,0]),k,d)
            if i<18:objects.append(grid.copy());left.append(lp);right.append(rp)
            else:validation.append((f'synthetic-{i}',lp,rp))
        matrices,diag=fit_points(objects,left,right,(1280,720),validation)
        self.assertAlmostEqual(diag['baseline_mm'],120,delta=0.5)
        self.assertLess(diag['validation_vertical_p95_px'],0.1)
        self.assertTrue(np.isfinite(matrices['Q']).all())


if __name__=='__main__':unittest.main()
