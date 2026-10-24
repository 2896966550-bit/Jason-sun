"""Persistent software-paired UVC capture for STATIC targets; no exposure sync claim."""
import math
import threading
import time
import cv2
from perception.camera_devices import enumerate_devices,resolve_device

PROPERTIES={'auto_exposure':cv2.CAP_PROP_AUTO_EXPOSURE,'exposure':cv2.CAP_PROP_EXPOSURE,
            'auto_focus':cv2.CAP_PROP_AUTOFOCUS,'focus':cv2.CAP_PROP_FOCUS,
            'auto_white_balance':cv2.CAP_PROP_AUTO_WB,'white_balance':cv2.CAP_PROP_WB_TEMPERATURE,
            'gain':cv2.CAP_PROP_GAIN}


def validate_config(config):
    for key in ('left_device_id','right_device_id'):
        if not isinstance(config.get(key),str) or not config[key].strip():raise ValueError('必须明确左右设备身份: '+key)
    if config['left_device_id']==config['right_device_id']:raise ValueError('左右不能使用同一设备')
    for key in ('width','height'):
        if type(config.get(key)) is not int or not 16<=config[key]<=8192:raise ValueError('无效图像尺寸: '+key)
    for key,low,high in (('fps',1,120),('max_skew_ms',0.1,1000),('max_age_ms',1,2000)):
        value=config.get(key)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high:raise ValueError('无效采集参数: '+key)
    settings=config.get('settings',{})
    if not isinstance(settings,dict) or set(settings)-set(PROPERTIES):raise ValueError('未知相机设置')
    for value in settings.values():
        if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value)):raise ValueError('相机设置必须为有限数值或null')
    return config


def configure_camera(cap,config):
    codec_ack=bool(cap.set(cv2.CAP_PROP_FOURCC,cv2.VideoWriter_fourcc(*'MJPG')))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,config['width']);cap.set(cv2.CAP_PROP_FRAME_HEIGHT,config['height'])
    fps_ack=bool(cap.set(cv2.CAP_PROP_FPS,config['fps']))
    settings={}
    for key,prop in PROPERTIES.items():
        requested=config.get('settings',{}).get(key)
        ack=bool(cap.set(prop,requested)) if requested is not None else None
        readback=float(cap.get(prop))
        settings[key]={'requested':requested,'set_ack':ack,'readback':readback if math.isfinite(readback) else None,
                       'status':'unrequested' if requested is None else 'readback_matches_request' if ack and math.isclose(readback,requested,abs_tol=0.01) else 'unverified'}
    fps=cap.get(cv2.CAP_PROP_FPS)
    codec_value=cap.get(cv2.CAP_PROP_FOURCC)
    codec=int(codec_value) if math.isfinite(codec_value) and codec_value>=0 else None
    return {'settings':settings,'codec_request':'MJPG','codec_set_ack':codec_ack,
            'codec_reported':''.join(chr((codec>>8*i)&255) for i in range(4)) if codec is not None else None,
            'fps_request':config['fps'],'fps_set_ack':fps_ack,
            'fps_reported':float(fps) if math.isfinite(fps) and fps>0 else None,'backend':cap.getBackendName()}


class CameraStream:
    def __init__(self,index,config,factory=cv2.VideoCapture):
        self.index,self.config,self.factory=index,config,factory
        self.stop=threading.Event();self.ready=threading.Event();self.lock=threading.Lock()
        self.latest=None;self.error=None;self.metadata=None;self.count=0;self.first_mono=None
        self.thread=threading.Thread(target=self._run,name=f'equis-camera-{index}',daemon=True)

    def _run(self):
        cap=None
        try:
            cap=self.factory(self.index,cv2.CAP_DSHOW)
            if not cap.isOpened():raise RuntimeError('相机打开失败')
            self.metadata=configure_camera(cap,self.config)
            while not self.stop.is_set():
                ok,frame=cap.read()
                mono=time.monotonic();wall=time.time()
                if not ok or frame is None:raise RuntimeError('相机读取失败，请重新核对设备并启动新会话')
                if frame.shape!=(self.config['height'],self.config['width'],3):raise RuntimeError('相机实际图像尺寸与配置不符')
                with self.lock:
                    self.count+=1
                    if self.first_mono is None:self.first_mono=mono
                    self.latest={'frame':frame,'sequence':self.count,'received_at':wall,'received_monotonic':mono}
                self.ready.set()
        except Exception as exc:
            self.error=str(exc);self.ready.set()
        finally:
            if cap is not None:cap.release()

    def start(self):
        self.thread.start()
        if not self.ready.wait(8):raise TimeoutError('相机启动超时')
        if self.error:raise RuntimeError(self.error)

    def snapshot(self):
        if self.error:raise RuntimeError(self.error)
        with self.lock:return dict(self.latest) if self.latest else None

    def close(self):
        self.stop.set()
        if self.thread.ident is not None:self.thread.join(2)
        if self.thread.is_alive():raise RuntimeError('相机驱动读取未退出；结束此采集进程后再启动，禁止复用旧会话')

    def stats(self):
        with self.lock:
            span=self.latest['received_monotonic']-self.first_mono if self.latest and self.first_mono is not None else 0
            return {'read_frames':self.count,'observed_span_s':span,
                    'host_dequeue_hz':(self.count-1)/span if span>0 else None,'error':self.error}


def eligible_pair(left,right,now,last,config):
    if not left or not right:return 'missing_frame'
    if left['sequence']<=last[0] or right['sequence']<=last[1]:return 'repeated_frame'
    ages=[(now-item['received_monotonic'])*1000 for item in (left,right)]
    if min(ages)<0 or max(ages)>config['max_age_ms']:return 'stale_frame'
    if abs(left['received_monotonic']-right['received_monotonic'])*1000>config['max_skew_ms']:return 'host_time_mismatch'
    return None


class StereoCapture:
    def __init__(self,config,inventory=enumerate_devices,stream_factory=CameraStream):
        self.config=validate_config(config)
        self.inventory=inventory;self.stream_factory=stream_factory;self.streams=[];self.last=(0,0)

    def start(self):
        before=self.inventory()
        indices=[resolve_device(self.config[k],before) for k in ('left_device_id','right_device_id')]
        try:
            for index in indices:
                stream=self.stream_factory(index,self.config);self.streams.append(stream);stream.start()
            after=self.inventory()
            if [resolve_device(self.config[k],after) for k in ('left_device_id','right_device_id')]!=indices:raise RuntimeError('相机枚举顺序在打开期间变化，拒绝使用本次会话')
            time.sleep(0.5) # warm-up only; does not prove exposure/white-balance stability
            return self
        except Exception:
            self.close();raise

    @property
    def metadata(self):
        return {side:{'device_id':self.config[f'{side}_device_id'],**stream.metadata}
                for side,stream in zip(('left','right'),self.streams)}

    def pair(self,timeout=2):
        if len(self.streams)!=2:raise RuntimeError('双目采集器尚未启动')
        deadline=time.monotonic()+timeout;reason='missing_frame'
        while time.monotonic()<deadline:
            left,right=[s.snapshot() for s in self.streams]
            reason=eligible_pair(left,right,time.monotonic(),self.last,self.config)
            if reason is None:
                self.last=(left['sequence'],right['sequence'])
                return {'left':left,'right':right,'host_skew_ms':abs(left['received_monotonic']-right['received_monotonic'])*1000,
                        'hardware_synchronized':False,'exposure_times_known':False,'time_kind':'host_dequeue',
                        'scope':'static_targets_only'}
            time.sleep(0.005)
        raise TimeoutError('未获得合格图对: '+reason)

    def close(self):
        errors=[]
        for stream in self.streams:
            try:stream.close()
            except Exception as exc:errors.append(str(exc))
        if errors:raise RuntimeError('; '.join(errors))
