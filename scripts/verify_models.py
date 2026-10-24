"""Check locally supplied model files against the published SHA256 manifest."""
from pathlib import Path
import hashlib,json
ROOT=Path(__file__).resolve().parents[1]


def main():
    manifest=json.loads((ROOT/'docs/model-manifest.json').read_text(encoding='utf-8'))
    results=[]
    for row in manifest['files']:
        path=ROOT/row['target']
        if not path.resolve().is_relative_to(ROOT.resolve()):raise ValueError('Model target outside repository')
        if not path.is_file():
            results.append({'file':row['file'],'status':'missing'});continue
        h=hashlib.sha256()
        with path.open('rb') as f:
            for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
        results.append({'file':row['file'],'status':'match' if h.hexdigest()==row['sha256'] and path.stat().st_size==row['bytes'] else 'mismatch'})
    print(json.dumps({'models':results,'scope':'file_identity_not_model_or_physical_validation'},indent=2))
    return 0 if all(r['status']=='match' for r in results) else 1


if __name__=='__main__':raise SystemExit(main())
