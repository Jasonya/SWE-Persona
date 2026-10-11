"""Collect real execution sidecars into one result delivery, never synthesize them."""
import json
import re
from pathlib import Path


SENSITIVE = re.compile(r'(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|client[_-]?secret)')


def redact_text(text):
    # Known structured assignments/headers only. This is not a guarantee that an
    # arbitrary conversation contains no personal or sensitive information.
    text = re.sub(r'(?i)\bBearer\s+[^\s"\',;]+', 'Bearer [REDACTED]', text)
    return re.sub(r'(?i)(\b(?:[a-z_]*api[_-]?key|access[_-]?token|refresh[_-]?token|password|client[_-]?secret)\s*[:=]\s*)([^\s,;]+)',
                  r'\1[REDACTED]', text)


def redact(value):
    if isinstance(value, dict):
        return {k: '[REDACTED]' if SENSITIVE.fullmatch(k) or k.upper().endswith('_API_KEY') else redact(v)
                for k, v in value.items()}
    if isinstance(value, list): return [redact(v) for v in value]
    if isinstance(value, str): return redact_text(value)
    return value


def collect(work, pkg, jobs):
    from .export_tb3_results import Package, sha, dumps
    if work is None:
        return {'status': 'not_supplied', 'records': {}, 'missing': ['execution work directory'], 'source_files': {}}
    work = Path(work).absolute()
    if work.is_symlink() or (work / 'project').resolve() != pkg.root.resolve():
        raise ValueError('Execution sidecars must belong to this project work directory')
    source = Package(work, pkg.output); records = {}; categories = {}
    def copy(path, dest, category):
        if not path.exists(): return
        data = source.source_bytes(path)
        if path.suffix == '.json':
            value = json.loads(data); cleaned = redact(value)
            payload = data if cleaned == value else dumps(cleaned)
        elif path.suffix == '.jsonl':
            values = [json.loads(line) for line in data.decode().splitlines() if line.strip()]
            cleaned = [redact(v) for v in values]
            payload = data if cleaned == values else ('\n'.join(json.dumps(v, ensure_ascii=False) for v in cleaned)+'\n').encode()
        else:
            payload = redact_text(data.decode()).encode()
        target = pkg.output / dest; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(payload)
        record = {'path': str(dest), 'sha256': sha(payload), 'source_sha256': sha(data),
                  'bytes': len(payload), 'redacted': payload != data, 'paths_relative_to': 'result_package_root'}
        records[str(dest)] = record; categories.setdefault(category, []).append(record)
    for name in ('run-plan.json', 'status.json', 'dataset-identity.json', 'source-pins.json'):
        copy(work / name, Path('provenance/execution') / name, 'execution')
    ids = {j['id'] for j in jobs}; tasks = {j['task_id'] for j in jobs}
    copy(work / 'preflight.json', Path('preflight/source-preflight.json'), 'preflight')
    for name in sorted(ids | tasks):
        copy(work / 'preflight' / (name + '.json'), Path('preflight') / (name + '.json'), 'preflight')
    for name in sorted(ids):
        copy(work / 'task-logs' / (name + '.log'), Path('task-logs') / (name + '.log'), 'task_logs')
    calibration = work / 'calibration'
    if calibration.is_symlink(): raise ValueError('Symlink calibration directory rejected')
    if calibration.exists():
        for path in sorted(calibration.rglob('*')):
            if path.is_symlink(): raise ValueError('Symlink calibration record rejected')
            if path.is_file() and path.suffix in ('.json', '.jsonl'):
                copy(path, Path('calibration') / path.relative_to(calibration), 'calibration')
    source.verify_sources()
    missing = [name for name in ('execution', 'preflight', 'calibration', 'task_logs') if not categories.get(name)]
    receipt = {'status': 'captured', 'records': records, 'categories': categories, 'missing': missing,
        'privacy': {'known_credential_fields_redacted': True, 'arbitrary_content_review': 'not_certified'},
        'source_files': {'execution-work/' + rel: record for rel, record in source.sources.items()}}
    pkg.write('provenance/execution-records.json', {k: v for k, v in receipt.items() if k != 'source_files'})
    return receipt
