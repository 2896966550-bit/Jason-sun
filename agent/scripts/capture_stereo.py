"""Local static stereo capture, explicit device identities and immutable PNG events."""
import argparse
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from perception.camera_devices import enumerate_devices,resolve_device
from perception.stereo_capture import StereoCapture,CameraStream
from perception.capture_store import CaptureStore,canonical


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    mode=ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--list-devices',action='store_true')
    mode.add_argument('--probe-device',help='Exact device_id from --list-devices')
    mode.add_argument('--config',type=Path)
    ap.add_argument('--out',type=Path)
    ap.add_argument('--seconds',type=float,default=10)
    ap.add_argument('--width',type=int,default=1280);ap.add_argument('--height',type=int,default=720)
    ap.add_argument('--count',type=int,default=10)
    ap.add_argument('--interval-seconds',type=float,default=1)
    ap.add_argument('--purpose',choices=('diagnostic','calibration','validation','measurement'),default='diagnostic')
    ap.add_argument('--sensor-port')
    ap.add_argument('--resume',action='store_true')
    ap.add_argument('--interactive',action='store_true',help='每次摆稳后按回车采图')
    ap.add_argument('--validation-every',type=int,default=0,help='标定采集中每N个事件预留一个独立验证位姿；建议4')
    args=ap.parse_args(argv)
    if args.list_devices:
        print(json.dumps(enumerate_devices(),ensure_ascii=True,indent=2));return 0
    if not 1<=args.seconds<=3600 or not 1<=args.count<=5000 or not 0.01<=args.interval_seconds<=60:ap.error('采样时长、次数或间隔越界')
    if args.validation_every and (args.validation_every<2 or args.purpose!='calibration'):ap.error('独立验证预留仅用于标定采集，N必须至少为2')
    if args.probe_device:
        if not 16<=args.width<=8192 or not 16<=args.height<=8192:ap.error('无效图像尺寸')
        config={'width':args.width,'height':args.height,'fps':30,'settings':{}}
        stream=CameraStream(resolve_device(args.probe_device,enumerate_devices()),config)
        try:
            stream.start();first=stream.snapshot();time.sleep(args.seconds);last=stream.snapshot()
            span=last['received_monotonic']-first['received_monotonic']
            report={'device_id':args.probe_device,'actual_shape':list(last['frame'].shape),
                    'metadata':stream.metadata,'observed_frames':last['sequence']-first['sequence'],
                    'observed_span_s':span,'host_dequeue_hz':(last['sequence']-first['sequence'])/span if span>0 else None,
                    'hardware_synchronized':False,'exposure_times_known':False}
            if args.out:
                with args.out.open('xb') as f:f.write(canonical(report))
            print(json.dumps(report,ensure_ascii=True,indent=2));return 0
        finally:stream.close()
    if args.out is None:ap.error('采集必须明确 --out 会话目录')
    config=json.loads(args.config.read_text(encoding='utf-8-sig'))
    capture=StereoCapture(config);store=None;sensor_reader=None
    summary={'attempts':0,'accepted':0,'rejected':0,'error':None}
    try:
        capture.start()
        store=CaptureStore(args.out,{'capture':config,'devices':capture.metadata},resume=args.resume)
        if args.sensor_port:
            import os
            os.environ['EQUIS_SENSOR_PORT']=args.sensor_port
            from tools import sensor_reader
            sensor_reader.read_sensors_raw(timeout=4)
        cursor=0
        while summary['accepted']<args.count and summary['attempts']<args.count*3:
            summary['attempts']+=1
            purpose='validation' if args.validation_every and (summary['accepted']+1)%args.validation_every==0 else args.purpose
            if args.interactive:
                input(f"摆稳第{summary['accepted']+1}个独立位姿（{purpose}），按回车采集: ")
                time.sleep(0.5)
            try:pair=capture.pair()
            except TimeoutError as exc:
                store.attempt('rejected',str(exc));summary['rejected']+=1;continue
            sensor=None
            if sensor_reader:
                snapshot=sensor_reader.read_sensors_raw(timeout=0)
                packets,lost=sensor_reader._reader.packets_since(cursor)
                if packets:cursor=packets[-1]['cursor']
                sensor={'snapshot':snapshot,'raw_packets':packets,'recorder_dropped':lost,
                        'association':'host_recording_only','hardware_synchronized':False}
            store.save(pair,purpose=purpose,identity=config.get('identity'),sensor=sensor)
            summary['accepted']+=1
            time.sleep(args.interval_seconds)
        summary['completed']=summary['accepted']==args.count
        print(json.dumps(summary,ensure_ascii=True));return 0 if summary['completed'] else 2
    except Exception as exc:
        summary['error']=str(exc)
        raise
    finally:
        summary['devices_observed']={side:stream.stats() for side,stream in zip(('left','right'),capture.streams)}
        try:capture.close()
        finally:
            if sensor_reader:sensor_reader.close_serial()
            if store:store.close(summary)


if __name__=='__main__':raise SystemExit(main())
