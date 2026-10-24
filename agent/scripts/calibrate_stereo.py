"""Offline stereo calibration from committed, independently reserved image pairs."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from perception.stereo_calibration import calibrate_session


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--session',type=Path,required=True);ap.add_argument('--board',type=Path,required=True);ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--exclusions',type=Path,help='已有事件ID到排除理由的JSON；不会删除原图')
    args=ap.parse_args();board=json.loads(args.board.read_text(encoding='utf-8-sig'))
    exclusions=json.loads(args.exclusions.read_text(encoding='utf-8-sig')) if args.exclusions else None
    result=calibrate_session(args.session,board,args.out,exclusions)
    print(json.dumps({'calibration_id':result['calibration_id'],'status':result['status'],'diagnostics':result['diagnostics']},ensure_ascii=True,indent=2))


if __name__=='__main__':main()
