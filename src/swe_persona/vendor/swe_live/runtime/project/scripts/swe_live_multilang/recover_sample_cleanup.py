"""Recover only Docker cleanup bookkeeping; never retry or rescore a sample.

This launcher-side maintenance step keeps the frozen scoring runtime unchanged.
The original receipt and the recovery proof are preserved next to each result.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

LABEL = 'personacode.sample'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    temporary = path.with_name(path.name + '.recovery-tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    os.replace(temporary, path)


def cleanup_verified(client, owner, attempts=4, sleep=time.sleep):
    import docker
    removed, errors = [], []
    def labels(container):
        # sparse=True returns /containers/json records with top-level Labels;
        # Container.labels expects inspected Config.Labels in some SDK versions.
        attrs = container.attrs
        value = attrs.get('Config', {}).get('Labels') or attrs.get('Labels')
        if not isinstance(value, dict):
            raise ValueError('Cannot verify container ownership labels')
        return value
    for attempt in range(attempts):
        try:
            containers = client.containers.list(all=True, filters={'label': LABEL+'='+owner}, sparse=True)
            for container in sorted(containers, key=lambda c:not bool(labels(c).get('personacode.fixture'))):
                if labels(container).get(LABEL) != owner:
                    raise ValueError('Container owner does not match; refusing removal')
                try:
                    container.remove(force=True)
                    removed.append(container.id)
                except docker.errors.NotFound:
                    pass  # Another cleanup request already removed this container.
            remaining = client.containers.list(all=True, filters={'label': LABEL+'='+owner}, sparse=True)
            if not remaining:
                return {'verified_no_owned_containers': True, 'removed_containers': removed,
                        'cleanup_attempts': attempt+1, 'transient_errors': errors}
        except docker.errors.DockerException as error:
            errors.append({'type': type(error).__name__, 'message': str(error)[:700]})
        if attempt+1 < attempts:
            sleep(min(4, attempt+1))
    raise RuntimeError('Owned containers could not be confirmed absent: '+json.dumps(errors))


def recover_receipt(path, client, cleanup=cleanup_verified):
    result = read(path)
    if str(result.get('error_type', '')).startswith('process_cleanup:'):
        raise ValueError('Process cleanup failure requires separate inspection; refusing to clear it')
    if not result.get('cleanup_error'):
        return None
    owner = result.get('owner', '')
    if not re.fullmatch(r'[0-9a-f]{32}', owner) or read(path.parent/'started.json').get('owner') != owner:
        raise ValueError('Missing or inconsistent sample ownership')
    proof = cleanup(client, owner)
    if proof.get('verified_no_owned_containers') is not True:
        raise ValueError('No verified cleanup proof')
    original = path.read_bytes()
    backup = path.with_name('result.before-cleanup-recovery-'+uuid.uuid4().hex[:12]+'.json')
    backup.write_bytes(original)
    recovery = {'recovered_utc': datetime.now(timezone.utc).isoformat(),
        'previous_cleanup_error': result['cleanup_error'], 'original_receipt': backup.name,
        'original_sha256': hashlib.sha256(original).hexdigest(), **proof,
        'outcome_policy': 'Original status, timing, budget and scores unchanged; no sample retry'}
    result['cleanup_error'] = None
    result['cleanup_recovery'] = recovery
    write(path.parent/'cleanup-recovery.json', recovery)
    write(path, result)
    return recovery


def refresh_progress(study):
    """Include receipts saved immediately before a cleanup exception stopped collection."""
    sys.path.insert(0, str(Path(__file__).parent/'reference_v2'))
    import bootstrap
    from support import file_hash, source_hashes, collection_source_hashes
    from collection.online import budget_after, advance_budget, ordered
    from collection.run_once import report
    m = read(study/'manifest.json'); cfg = m['config']
    if cfg.get('collection_policy') != 'one-pass-all-outcomes':
        return
    if m['sources'] != source_hashes() or m['collection_sources'] != collection_source_hashes():
        raise ValueError('Frozen source changed; refusing to rebuild progress')
    records = read(study/'carried-forward.json'); seen = {r['pair_id'] for r in records}
    observations = []; state = budget_after([], cfg['initial_seconds'], cfg['maximum_seconds'], cfg['update_every'])
    for task in ordered(m['tasks'], lambda t:t['instance_id'], cfg['seed']):
        for persona in ordered(m['personas'], lambda p:p, cfg['seed']+task['instance_id']):
            pair = task['key']+'/'+persona; path = study/'samples'/task['key']/persona/'result.json'
            if pair in seen or not path.exists():
                continue
            result = read(path)
            if result.get('cleanup_error') or str(result.get('error_type', '')).startswith('process_cleanup:'):
                raise ValueError('Unresolved cleanup receipt remains')
            row = {k: result.get(k) for k in ('status','base_task_success','seconds','cap_seconds')}
            row.update(pair_id=pair, source_result=str(path.resolve()), result_sha256=file_hash(path),
                source_protocol=m['protocol'], source_study=study.name, carried_forward=False, task_sha256=task['sha256'])
            records.append(row); seen.add(pair); observations.append(row)
            state = advance_budget(state, observations, cfg['maximum_seconds'])
    write(study/'online-budget.json', state)
    report(study, records, m['candidate_pairs'], state['next_cap_seconds'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study-dir', type=Path, required=True)
    args = parser.parse_args(argv); study = args.study_dir.resolve()
    if not (study/'manifest.json').exists():
        print(json.dumps({'recovered':0,'unresolved':0})); return 0
    # Holding the same byte lock excludes the collector during repair.
    with (study/'collection.lock').open('a+b') as lock:
        lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        pending = []
        for path in sorted((study/'samples').glob('*/*/result.json')):
            result = read(path)
            if result.get('cleanup_error') or str(result.get('error_type', '')).startswith('process_cleanup:'):
                pending.append(path)
        if not pending:
            print(json.dumps({'recovered':0,'unresolved':0})); return 0
        import docker
        client = docker.from_env(timeout=10); recovered = []; errors = []
        try:
            for path in pending:
                try:
                    proof = recover_receipt(path, client)
                    if proof: recovered.append(path.parent.relative_to(study/'samples').as_posix())
                except Exception as error:
                    errors.append({'sample':str(path.parent), 'error':str(error)[:1000]})
        finally:
            client.close()
        if recovered and not errors:
            refresh_progress(study)
        print(json.dumps({'recovered':len(recovered),'pairs':recovered,'unresolved':len(errors),'errors':errors}))
        return 2 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
