"""Validate grouped paired residual data, without fitting beta or writing deployment."""
import argparse
import csv
import json
import math
from pathlib import Path

REQUIRED=('event_id','specimen_id','session_id','split','measured_value_mm','reference_value_mm',
          'u_ref_mm','anomaly_score','epipolar_p95_px','roi_valid_fraction')


def validate(path):
    rows=[];seen=set();groups={}
    with Path(path).open(encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f)
        if not reader.fieldnames or set(REQUIRED)-set(reader.fieldnames):raise ValueError('缺少必填列: '+','.join(REQUIRED))
        for number,row in enumerate(reader,2):
            if any(not str(row.get(k) or '').strip() for k in REQUIRED):raise ValueError(f'第{number}行缺参')
            if row['event_id'] in seen:raise ValueError('重复事件')
            seen.add(row['event_id'])
            if row['split'] not in ('development','calibration','test'):raise ValueError('未知数据划分')
            previous=groups.setdefault(row['specimen_id'],row['split'])
            if previous!=row['split']:raise ValueError('同一工件跨数据划分，存在泄漏')
            for key in REQUIRED[4:]:
                value=float(row[key])
                if not math.isfinite(value):raise ValueError('非有限数值')
                row[key]=value
            if row['u_ref_mm']<0 or row['epipolar_p95_px']<0 or not 0<=row['roi_valid_fraction']<=1:raise ValueError('量纲字段越界')
            row['signed_residual_mm']=row['measured_value_mm']-row['reference_value_mm']
            rows.append(row)
    if not rows:raise ValueError('无实测样本；模板表头不能当作数据')
    return {'schema_valid':True,'events':len(rows),'specimens':len(groups),
            'split_counts':{k:sum(r['split']==k for r in rows) for k in ('development','calibration','test')},
            'physical_mapping_validated':False,'rows':rows}


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('csv',type=Path);ap.add_argument('--out',type=Path,required=True)
    args=ap.parse_args();result=validate(args.csv)
    with args.out.open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2,allow_nan=False)
    print(json.dumps({k:v for k,v in result.items() if k!='rows'},ensure_ascii=True))


if __name__=='__main__':main()
