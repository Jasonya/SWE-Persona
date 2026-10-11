"""One pass over all pairs: every retained outcome counts; never replay/retry."""
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import bootstrap
from bootstrap import ROOT
import argparse
from collections import Counter
import ctypes
import os
from personas import POOL_PERSONAS
from support import Manifest, read, file_hash, digest
from run_fullset import atomic_save, get_image, release_image
from collection.run_roleplay import prepare, gold_directory
from collection.online import budget_after, advance_budget, ordered
from whole_sample import existing_result, supervise_sample

DEFAULT = ROOT / '.local/multilang-roleplay/fullset-once-v3'
HISTORY = [ROOT / '.local/multilang-roleplay' / name for name in ('fullset-v1', 'fullset-v2')]


def collect_history(studies):
    """Later chronological cohorts win, never pick the most successful attempt."""
    pairs = {}
    for study in studies:
        manifest = read(study/'manifest.json')
        tasks = {t['key']: t for t in manifest['tasks']}
        for path in sorted((study/'samples').glob('*/*/result.json')):
            key, persona = path.parent.relative_to(study/'samples').parts
            task = tasks[key]
            if persona not in manifest['personas']:
                raise ValueError('Unknown historical persona')
            if file_hash(study/task['path']) != task['sha256']:
                raise ValueError('Historical task changed')
            receipt = read(path)
            pairs[key+'/'+persona] = {
                'pair_id': key+'/'+persona, 'status': receipt['status'],
                'base_task_success': receipt.get('base_task_success'),
                'seconds': receipt.get('seconds'), 'cap_seconds': receipt.get('cap_seconds'),
                'source_result': str(path.resolve()), 'result_sha256': file_hash(path),
                'task_sha256': task['sha256'], 'source_protocol': manifest['protocol'],
                'source_study': study.name, 'carried_forward': True}
    return [pairs[key] for key in sorted(pairs)]


def report(study, records, total, cap):
    ids = [r['pair_id'] for r in records]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate pair in one-pass progress')
    successes = [r for r in records if r['status'] in ('completed', 'historical_rescored') and r.get('base_task_success') == 1]
    summary = {'candidate_pairs': total, 'processed_pairs': len(records), 'retained_pairs': len(records),
        'remaining': total-len(records), 'coverage_percent': 100*len(records)/total,
        'counts': dict(Counter(r['status'] for r in records)), 'successful_pairs': len(successes),
        'carried_forward_pairs': sum(bool(r.get('carried_forward')) for r in records),
        'next_cap_seconds': cap, 'policy': 'All outcomes count as processed; no retry, replay, or regrading',
        'success_case_policy': 'Original task passed; persona dialogue ratings are separate. Preserve source protocol tags.',
        'publication_ready': False}
    atomic_save(study/'results.json', records)
    atomic_save(study/'summary.json', summary)
    atomic_save(study/'successful-cases.json', successes)
    return summary


def collect(study, manifest, carried, client):
    cfg = manifest['config']
    records = list(carried); seen = {r['pair_id'] for r in records}
    state = budget_after([], cfg['initial_seconds'], cfg['maximum_seconds'], cfg['update_every'])
    observations = []
    report(study, records, manifest['candidate_pairs'], state['next_cap_seconds'])
    for task in ordered(manifest['tasks'], lambda t:t['instance_id'], cfg['seed']):
        image = None; pulled = False; gold = None; setup_error = None
        try:
            for persona in ordered(manifest['personas'], lambda p:p, cfg['seed']+task['instance_id']):
                pair_id = task['key']+'/'+persona
                if pair_id in seen:
                    continue
                directory = study/'samples'/task['key']/persona
                result = existing_result(directory)
                if result is None:
                    capfile = study/'budgets'/task['key']/(persona+'.json')
                    if not capfile.exists():
                        atomic_save(capfile, {'cap_seconds': state['next_cap_seconds'], 'history_digest': digest(records)})
                    cap = read(capfile)['cap_seconds']
                    print(f'[{len(records)+1}/{manifest["candidate_pairs"]}] {pair_id}; whole sample cap={cap:.2f}s', flush=True)
                    if image is None and setup_error is None:
                        try:
                            image, pulled = get_image(client, Manifest(study/task['path']))
                            gold = gold_directory(study, task, image.id, cap)
                        except Exception as error:
                            setup_error = type(error).__name__+': '+str(error)[:700]
                    if setup_error:
                        result = {'status':'infrastructure_invalid','base_task_success':None,'seconds':None,'cap_seconds':cap,'error':setup_error}
                        atomic_save(directory/'result.json', result)
                    else:
                        command = [sys.executable, '-u', str(HERE/'roleplay.py'), '--task', str(study/task['path']),
                            '--persona', persona, '--gold-dir', str(gold), '--model', cfg['model'],
                            '--max-turns', str(cfg['max_turns']), '--max-steps', str(cfg['max_steps'])]
                        result = supervise_sample(command, directory, cap)
                row = {k:result.get(k) for k in ('status','base_task_success','seconds','cap_seconds')}
                row.update(pair_id=pair_id, source_result=str((directory/'result.json').resolve()),
                    result_sha256=file_hash(directory/'result.json'), source_protocol=manifest['protocol'],
                    source_study=study.name, carried_forward=False, task_sha256=task['sha256'])
                records.append(row); seen.add(pair_id); observations.append(row)
                state = advance_budget(state, observations, cfg['maximum_seconds'])
                atomic_save(study/'online-budget.json', state)
                report(study, records, manifest['candidate_pairs'], state['next_cap_seconds'])
                print(f'  {result["status"]}; processed={len(records)}; no retry', flush=True)
        finally:
            if image is not None:
                release_image(client, image, pulled, False)
    return records


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',action='store_true')
    p.add_argument('--study-dir',type=Path,default=DEFAULT)
    args=p.parse_args(argv)
    study=args.study_dir.resolve(); study.mkdir(parents=True,exist_ok=True)
    lock=(study/'collection.lock').open('a+b')
    if os.name=='nt':
        import msvcrt
        lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
        msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    else:
        import fcntl
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    if (study/'manifest.json').exists():
        cfg=read(study/'manifest.json')['config']; carried=read(study/'carried-forward.json')
        if digest(carried)!=cfg['carried_digest']:
            raise ValueError('Frozen carried progress changed')
    else:
        carried=collect_history(HISTORY)
        latest=read(HISTORY[-1]/'online-budget.json')
        cfg={'expected_tasks':1077,'model':'gpt-6-astra','seed':'silvia-shared-roleplay-v1',
            'initial_seconds':latest['next_cap_seconds'],'maximum_seconds':3600,'update_every':20,
            'max_turns':4,'max_steps':14,'workers':1,'collection_policy':'one-pass-all-outcomes',
            'carried_digest':digest(carried),'initial_budget_source':'Continue last v2 cap; only new samples update P20',
            'history_sources':{str(s):file_hash(s/'manifest.json') for s in HISTORY}}
        atomic_save(study/'carried-forward.json',carried)
    manifest=prepare(study,cfg)
    tasks={t['key']:t for t in manifest['tasks']}
    for row in carried:
        key,persona=row['pair_id'].split('/')
        if row['task_sha256']!=tasks[key]['sha256'] or persona not in manifest['personas']:
            raise ValueError('History does not match frozen full-set identity')
    if not (study/'results.json').exists():
        report(study,carried,manifest['candidate_pairs'],cfg['initial_seconds'])
    print({'candidate_pairs':manifest['candidate_pairs'],'already_processed':len(carried),
           'remaining':manifest['candidate_pairs']-len(carried),'run':args.run,'study':str(study)},flush=True)
    if not args.run:
        lock.close(); return 0
    import docker
    client=docker.from_env(timeout=60); info=client.info()
    if info['OSType']!='linux' or info['MemTotal']<18*1024**3:
        raise ValueError('Linux Docker with at least 18 GiB required')
    if os.name=='nt': ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
    try:
        collect(study,manifest,carried,client)
    finally:
        client.close(); lock.close()
        if os.name=='nt': ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
    return 0


if __name__=='__main__': raise SystemExit(main())
