"""Check tracked/staged source for publish mistakes without echoing secret values."""
from pathlib import Path
import hashlib
import json
import re
import subprocess

ROOT=Path(__file__).resolve().parents[1]
PATTERNS=[re.compile(p) for p in (r'sk-[A-Za-z0-9_-]{20,}',r'gh[pousr]_[A-Za-z0-9_]{20,}',
          r'github_pat_[A-Za-z0-9_]{20,}',r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')]


def main():
    result=subprocess.run(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT,check=True,capture_output=True)
    files=sorted(set(p for p in result.stdout.decode('utf-8').split('\0') if p))
    issues=[]
    for rel in files:
        path=ROOT/rel
        if not path.is_file():continue
        parts=Path(rel).parts
        if path.name=='.env' or path.suffix.lower() in ('.db','.sqlite','.sqlite3','.pth','.pkl','.bin','.log','.tmp') or '.pio' in parts or rel.startswith('agent/data/'):
            issues.append({'path':rel,'reason':'private_or_runtime_file'})
        if path.stat().st_size>10*1024*1024:issues.append({'path':rel,'reason':'unexpected_large_file'})
        if path.suffix.lower() in ('.png','.jpg','.jpeg','.woff','.woff2','.ttf','.ico'):continue
        text=path.read_text(encoding='utf-8',errors='replace')
        for line_number,line in enumerate(text.splitlines(),1):
            if any(p.search(line) for p in PATTERNS):issues.append({'path':rel,'line':line_number,'reason':'possible_secret'})
    required=['agent/server.py','agent/equis.html','agent/static/marked.min.js','agent/static/mermaid.min.js',
              'agent/static/katex/katex.min.js','agent/static/katex/katex.min.css','esp32_sensors/src/main.cpp',
              'third_party/licenses/katex-LICENSE','third_party/licenses/marked-LICENSE','third_party/licenses/mermaid-LICENSE']
    for rel in required:
        if rel not in files or not (ROOT/rel).is_file():issues.append({'path':rel,'reason':'required_runtime_file_missing'})
    manifest=ROOT/'docs/model-manifest.json'
    if not manifest.is_file():issues.append({'path':'docs/model-manifest.json','reason':'model_setup_missing'})
    vendor_manifest=ROOT/'third_party/manifest.json'
    if vendor_manifest.is_file():
        for library in json.loads(vendor_manifest.read_text(encoding='utf-8')):
            for item in library.get('files',[]):
                resource=ROOT/item['path']
                if not resource.is_file() or hashlib.sha256(resource.read_bytes()).hexdigest()!=item['sha256']:
                    issues.append({'path':item['path'],'reason':'vendor_checksum_mismatch'})
    print(json.dumps({'files_checked':len(files),'issues':issues,'scope':'heuristic_source_check_not_security_certification'},ensure_ascii=True,indent=2))
    return 1 if issues else 0


if __name__=='__main__':raise SystemExit(main())
