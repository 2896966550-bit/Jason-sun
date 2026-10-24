"""Describe recorded IMU stability; never writes offsets to firmware or config."""
import argparse
import json
import math
from pathlib import Path
import statistics


def assess(path,stationary_confirmed=False):
    rows=[];boots=set();invalid=0
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        record=json.loads(line)
        if not record.get('accepted'):continue
        packet=json.loads(record['raw'])
        if packet.get('v')!=2 or packet.get('type')!='imu':continue
        if packet.get('unit')!='m/s2' or packet.get('gyro_unit')!='deg/s':raise ValueError('单位未声明或不受支持')
        boots.add(packet['boot'])
        for sample in packet['samples']:
            if len(sample)!=8 or sample[-1] is not True or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in sample[:-1]):
                invalid+=1;continue
            if rows and sample[0]<=rows[-1][0]:raise ValueError('设备时间倒退或启动混合，请分段分析')
            rows.append(sample)
    if len(boots)!=1 or len(rows)<100:raise ValueError('需要同一次启动的至少100个有效观测')
    means=[statistics.fmean(s[i] for s in rows) for i in range(1,7)]
    std=[statistics.stdev(s[i] for s in rows) for i in range(1,7)]
    dt=(rows[-1][0]-rows[0][0])/1e6
    return {'valid_samples':len(rows),'invalid_samples':invalid,'duration_s':dt,
            'effective_hz':(len(rows)-1)/dt,'accel_mean_m_s2':means[:3],
            'accel_std_m_s2':std[:3],'gyro_mean_deg_s':means[3:],'gyro_std_deg_s':std[3:],
            'mean_accel_magnitude_m_s2':math.hypot(*means[:3]),
            'stationary_condition_user_confirmed':stationary_confirmed,
            'gyro_bias_candidate_deg_s':means[3:] if stationary_confirmed else None,
            'correction_applied':False,'metrological_calibration_validated':False,
            'scope':'recorded_samples_only; orientation/gravity and reference uncertainty unresolved'}


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('jsonl',type=Path);ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--stationary-confirmed',action='store_true',help='仅在操作者确认全程静止时使用')
    args=ap.parse_args();result=assess(args.jsonl,args.stationary_confirmed)
    with args.out.open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2,allow_nan=False)
    print(json.dumps(result,ensure_ascii=True))


if __name__=='__main__':main()
