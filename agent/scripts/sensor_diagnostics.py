"""Local-only bounded serial recorder. Never calls models or loads .env."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',help='Explicit port, e.g. COM10')
    parser.add_argument('--seconds',type=float,default=30)
    parser.add_argument('--out',type=Path,required=True,help='New JSONL file; never overwrites')
    args=parser.parse_args()
    if not 1<=args.seconds<=3600:parser.error('seconds must be in [1,3600]')
    if args.port:os.environ['EQUIS_SENSOR_PORT']=args.port
    from tools.sensor_reader import _reader
    args.out.parent.mkdir(parents=True,exist_ok=True)
    summary_path=args.out.with_suffix('.summary.json')
    if args.out.exists() or summary_path.exists():parser.error('output already exists')
    started=time.time();deadline=time.monotonic()+args.seconds
    cursor=0;lost=0;total=0;accepted=0;last_good=None
    try:
        with args.out.open('x',encoding='utf-8') as handle:
            _reader.start()
            while time.monotonic()<deadline:
                rows,drop=_reader.packets_since(cursor)
                lost+=drop
                for row in rows:
                    handle.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
                    cursor=row['cursor'];total+=1;accepted+=bool(row['accepted'])
                handle.flush()
                status=_reader.status()
                if (status.get('snapshot') or {}).get('fresh'):last_good=status['snapshot']
                time.sleep(0.05)
            # Drain the recorder queue before closing the handle.
            rows,drop=_reader.packets_since(cursor);lost+=drop
            for row in rows:
                handle.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
                total+=1;accepted+=bool(row['accepted'])
            status=_reader.status()
    finally:
        _reader.close()
    summary={'started_at':started,'duration_s':time.time()-started,'packet_count':total,
             'accepted_packets':accepted,'recorder_dropped_packets':lost,'final_status':status,
             'last_fresh_snapshot':last_good,'hardware_synchronized':False,
             'metrological_calibration_validated':False}
    with summary_path.open('x',encoding='utf-8') as handle:
        json.dump(summary,handle,ensure_ascii=False,indent=2,allow_nan=False)
    print(json.dumps({'packets':total,'accepted':accepted,'recorder_dropped':lost,
                      'fresh':bool((status.get('snapshot') or {}).get('fresh')),
                      'summary':str(summary_path)},ensure_ascii=True))
    return 0 if last_good and lost==0 else 2


if __name__=='__main__':raise SystemExit(main())
