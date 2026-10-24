"""Crash-visible capture bundles. Only committed event directories are data records."""
from datetime import datetime,timezone
import hashlib
import json
import math
from pathlib import Path
import uuid
import cv2


def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(',',':')).encode('utf-8')
def sha(data):return hashlib.sha256(data).hexdigest()


class CaptureStore:
    def __init__(self,root,config,*,resume=False):
        self.root=Path(root)
        if resume:
            existing=json.loads((self.root/'session.json').read_text(encoding='utf-8'))
            if existing['config_sha256']!=sha(canonical(config)):raise ValueError('恢复会话的设备或配置与原会话不一致')
            if (self.root/'closed.json').exists():raise ValueError('会话已关闭，请创建新的会话目录')
            self.session=existing
            recovered=[]
            for partial in (self.root/'events').glob('.pending-*'):
                if partial.is_symlink() or not partial.resolve().is_relative_to(self.root.resolve()):raise ValueError('待恢复目录越界')
                quarantine=self.root/'quarantine';quarantine.mkdir(exist_ok=True)
                name=partial.name+'-'+uuid.uuid4().hex
                partial.rename(quarantine/name);recovered.append(name)
            if recovered:
                (self.root/('recovery-'+uuid.uuid4().hex+'.json')).write_bytes(canonical({'quarantined_partial_events':recovered}))
        else:
            self.root.mkdir(parents=True,exist_ok=False)
            self.session={'schema_version':'stereo-session-v1','session_id':uuid.uuid4().hex,
                          'created_at_utc':datetime.now(timezone.utc).isoformat(),'config':config,'config_sha256':sha(canonical(config))}
            (self.root/'session.json').write_bytes(canonical(self.session))
            (self.root/'events').mkdir()

    def attempt(self,status,reason=None):
        with (self.root/'attempts.jsonl').open('ab') as f:
            f.write(canonical({'time_utc':datetime.now(timezone.utc).isoformat(),'status':status,'reason':reason})+b'\n')
            f.flush()

    def save(self,pair,*,purpose,identity=None,sensor=None):
        if purpose not in ('calibration','validation','diagnostic','measurement'):raise ValueError('未知数据用途')
        eid=uuid.uuid4().hex
        pending=self.root/'events'/('.pending-'+eid);pending.mkdir()
        event={'schema_version':'stereo-event-v1','event_id':eid,'session_id':self.session['session_id'],
               'config_sha256':self.session['config_sha256'],'purpose':purpose,'identity':identity or {},
               'hardware_synchronized':False,'exposure_times_known':False,'time_kind':'host_dequeue',
               'host_skew_ms':pair['host_skew_ms'],'sensor':sensor,'images':{}}
        try:
            for side in ('left','right'):
                item=pair[side]
                frame=item['frame']
                ok,encoded=cv2.imencode('.png',frame)
                if not ok:raise OSError('PNG编码失败')
                raw=encoded.tobytes();filename=side+'.png'
                (pending/filename).write_bytes(raw)
                event['images'][side]={'file':filename,'sha256':sha(raw),'shape':list(frame.shape),
                                      'sequence':item['sequence'],'received_at':item['received_at'],
                                      'received_monotonic':item['received_monotonic']}
            (pending/'event.json').write_bytes(canonical(event))
            pending.rename(self.root/'events'/eid)
            self.attempt('accepted',eid)
            return event
        except Exception as exc:
            self.attempt('storage_failed',str(exc))
            raise # pending directory intentionally remains visible for inspection

    def close(self,summary):
        path=self.root/'closed.json'
        with path.open('xb') as f:f.write(canonical(summary))


def validate_session(root):
    root=Path(root);session=json.loads((root/'session.json').read_text(encoding='utf-8'))
    if session['config_sha256']!=sha(canonical(session['config'])):raise ValueError('会话配置哈希不一致')
    errors=[];events=[];pending=[];seen=set()
    for directory in sorted((root/'events').iterdir()):
        if directory.name.startswith('.pending-'):
            pending.append(directory.name);continue
        if not directory.is_dir():errors.append({'event':directory.name,'error':'unexpected_file'});continue
        try:
            if directory.is_symlink() or not directory.resolve().is_relative_to(root.resolve()):raise ValueError('事件路径越界')
            event=json.loads((directory/'event.json').read_text(encoding='utf-8'))
            if event['event_id']!=directory.name or event['event_id'] in seen:raise ValueError('事件身份重复或目录不符')
            seen.add(event['event_id'])
            if event['session_id']!=session['session_id'] or event['config_sha256']!=session['config_sha256']:raise ValueError('跨会话或配置不一致')
            if event.get('hardware_synchronized') is not False or event.get('exposure_times_known') is not False:raise ValueError('不可声明硬件同步')
            images=event['images']
            times=[]
            for side in ('left','right'):
                item=images[side]
                if item['file']!=side+'.png':raise ValueError('图像路径不符合固定命名')
                if (directory/item['file']).is_symlink():raise ValueError('图像不能使用符号链接')
                raw=(directory/item['file']).read_bytes()
                if sha(raw)!=item['sha256']:raise ValueError('图像哈希不一致')
                import numpy as np
                frame=cv2.imdecode(np.frombuffer(raw,dtype=np.uint8),cv2.IMREAD_COLOR)
                if frame is None or list(frame.shape)!=item['shape']:raise ValueError('图像损坏或尺寸变化')
                config=session['config']['capture']
                if frame.shape!=(config['height'],config['width'],3):raise ValueError('图像不符合会话尺寸')
                value=item['received_monotonic']
                if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):raise ValueError('无效时间')
                times.append(value)
            skew=abs(times[0]-times[1])*1000
            if skew>session['config']['capture']['max_skew_ms'] or not math.isclose(skew,event['host_skew_ms'],abs_tol=1e-6):raise ValueError('配对时差超限或被修改')
            events.append(event)
        except (ValueError,KeyError,TypeError,OSError) as exc:errors.append({'event':directory.name,'error':str(exc)})
    return {'valid':not errors and not pending,'committed_events':len(events),'errors':errors,
            'quarantined_partial_events':[p.name for p in (root/'quarantine').iterdir()] if (root/'quarantine').is_dir() else [],
            'pending_directories':pending,'events':events,'session':session}
