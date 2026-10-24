"""Check shipped JavaScript syntax and local interaction regression fixtures."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    node = os.environ.get('EQUIS_NODE') or shutil.which('node')
    if not node:
        print('Node.js is required for frontend checks; install it or set EQUIS_NODE.')
        return 1
    commands = [[node, '--check', str(p)] for p in sorted((ROOT/'static/js').glob('*.js'))]
    commands += [[node, str(p)] for p in sorted((ROOT/'scripts/frontend').glob('test_*.cjs'))]
    failed = 0
    for command in commands:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace')
        if result.returncode:
            failed += 1
            print(result.stdout + result.stderr)
    print(f'JavaScript checks: {len(commands)-failed}/{len(commands)} passed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
