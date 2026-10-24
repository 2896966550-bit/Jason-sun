"""Verify committed event images and session consistency without touching hardware."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from perception.capture_store import validate_session,canonical


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('session',type=Path);ap.add_argument('--out',type=Path)
    args=ap.parse_args();result=validate_session(args.session)
    result.pop('events');result.pop('session')
    if args.out:
        with args.out.open('xb') as f:f.write(canonical(result))
    print(json.dumps(result,ensure_ascii=True,indent=2));return 0 if result['valid'] else 2


if __name__=='__main__':raise SystemExit(main())
