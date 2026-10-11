"""Frozen full dataset x ten shared LLM personas, with online whole-sample P20."""
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import bootstrap
from bootstrap import ROOT
import argparse
from collections import Counter
import ctypes
import json
import os
from personas import POOL_PERSONAS
from roleplay import PROTOCOL
from support import Manifest, REVISION, read, file_hash, source_hashes, collection_source_hashes, digest
from run_fullset import all_rows, atomic_save, get_image, release_image
from run_study import ensure_gold
from run_reference import gold_proof
from whole_sample import supervise_sample, existing_result
from collection.online import PEER_INITIAL_SECONDS, PEER_SOURCE, budget_after, advance_budget, ordered


def prepare(study, config, rows=None):
    path = study / 'manifest.json'
    sources, collection = source_hashes(), collection_source_hashes()
    if path.exists():
        manifest = read(path)
        if (manifest['config'] != config or manifest['sources'] != sources
                or manifest['collection_sources'] != collection or manifest['protocol'] != PROTOCOL):
            raise ValueError('Frozen code/config changed. Use a new study directory; do not mix experiments.')
        for task in manifest['tasks']:
            if file_hash(study / task['path']) != task['sha256']:
                raise ValueError('Frozen task changed: ' + task['key'])
        return manifest
    tasks = []
    for language, row in all_rows() if rows is None else rows:
        key = f'{len(tasks)+1:04d}-{language}'
        target = study / 'tasks' / key / 'task.private.jsonl'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(row, ensure_ascii=False) + '\n', encoding='utf-8')
        Manifest(target)
        tasks.append({'key': key, 'instance_id': row['instance_id'], 'language': language,
                      'path': target.relative_to(study).as_posix(), 'sha256': file_hash(target)})
    if len(tasks) != config['expected_tasks'] or len({t['instance_id'] for t in tasks}) != len(tasks):
        raise ValueError('Dataset size/identity mismatch')
    if len(POOL_PERSONAS) != 10:
        raise ValueError('Expected the original ten-persona pool')
    manifest = {'protocol': PROTOCOL, 'dataset_revision': REVISION, 'config': config,
        'sources': sources, 'collection_sources': collection, 'tasks': tasks, 'personas': list(POOL_PERSONAS),
        'candidate_pairs': len(tasks)*len(POOL_PERSONAS), 'initial_budget_source': PEER_SOURCE,
        'selection': 'Every candidate attempted; online P20 estimate, not exact global fastest fifth',
        'measurement': 'Official original correctness; separate subjective dialogue ratings; no technical persona oracle'}
    atomic_save(path, manifest)
    return manifest


def summarize(records, total, state):
    counts = Counter(r['status'] for r in records)
    successes = sum(r.get('base_task_success') == 1 and r['status'] == 'completed' for r in records)
    evaluated = counts['completed'] + counts['timeout']
    return {'candidate_pairs': total, 'retained_pairs': len(records), 'remaining': total-len(records),
        'counts': dict(counts), 'original_task_successes': successes,
        'original_success_rate_including_timeouts': successes/evaluated if evaluated else None,
        'rate_denominator': evaluated, 'excluded_from_rate': 'not_applicable, infrastructure_invalid, evaluation_invalid, interrupted',
        'historical_rescored': counts['historical_rescored'],
        'historical_original_successes': sum(r['status'] == 'historical_rescored' and r.get('base_task_success') == 1 for r in records),
        'historical_note': 'Old interactions rescored uniformly; separate generation cohort, excluded from new-protocol rate and P20',
        'joint_success': None, 'next_cap_seconds': state['next_cap_seconds'],
        'publication_ready': False, 'protocol': PROTOCOL}


def gold_directory(study, task, image_id, cap):
    manifest = Manifest(study / task['path'])
    for directory in (study/'gold'/task['key'], ROOT/'.local/multilang-roleplay/fullset-v1/gold'/task['key'], ROOT/'.local/multilang-study/qualification-ten-v3/gold'/task['key']):
        try:
            gold_proof(directory, manifest, image_id)
            return directory
        except (ValueError, FileNotFoundError):
            pass
    if ensure_gold(study, task, image_id, {'gold_seconds': cap}, False) is None:
        raise ValueError('Official gold environment preflight failed or timed out; no model score')
    directory = study/'gold'/task['key']
    gold_proof(directory, manifest, image_id)
    return directory


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run', action='store_true')
    p.add_argument('--study-dir', type=Path, default=ROOT/'.local/multilang-roleplay/fullset-v2')
    p.add_argument('--previous-study', type=Path, default=ROOT/'.local/multilang-roleplay/fullset-v1')
    p.add_argument('--model', default='gpt-6-astra')
    args = p.parse_args(argv)
    previous = args.previous_study.resolve()
    study = args.study_dir.resolve(); study.mkdir(parents=True, exist_ok=True)
    config = {'expected_tasks': 1077, 'model': args.model, 'seed': 'silvia-shared-roleplay-v1',
              'initial_seconds': PEER_INITIAL_SECONDS, 'maximum_seconds': 3600, 'update_every': 20,
              'max_turns': 4, 'max_steps': 14, 'workers': 1}
    config.update(previous_study=str(previous), previous_manifest_sha256=file_hash(previous/'manifest.json'),
                  previous_results_sha256=file_hash(previous/'results.json'))
    # Kernel-held lock is released after crashes; no stale PID lock files.
    lock = (study/'collection.lock').open('a+b')
    if os.name == 'nt':
        import msvcrt
        lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    manifest = prepare(study, config)
    print(json.dumps({'tasks': len(manifest['tasks']), 'personas': len(POOL_PERSONAS),
        'candidate_pairs': manifest['candidate_pairs'], 'initial_seconds': PEER_INITIAL_SECONDS,
        'run': args.run, 'study': str(study)}), flush=True)
    if not args.run:
        return 0
    import docker
    client = docker.from_env(timeout=60)
    info = client.info()
    if info['OSType'] != 'linux' or info['MemTotal'] < 18*1024**3:
        raise ValueError('Linux Docker with at least 18 GiB required')
    if os.name == 'nt':
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
    state = budget_after([], config['initial_seconds'], config['maximum_seconds'], config['update_every'])
    history = []
    try:
        for task in ordered(manifest['tasks'], lambda t:t['instance_id'], config['seed']):
            image = None; pulled = False; gold = None; setup_error = None
            try:
                for persona in ordered(list(POOL_PERSONAS), lambda x:x, config['seed']+task['instance_id']):
                    pair_id = task['key']+'/'+persona
                    directory = study/'samples'/task['key']/persona
                    result = existing_result(directory)
                    if result is None:
                        capfile = study/'budgets'/task['key']/(persona+'.json')
                        if not capfile.exists():
                            atomic_save(capfile, {'cap_seconds': state['next_cap_seconds'],
                                'history_digest': digest(history), 'records_seen': len(history)})
                        cap = read(capfile)['cap_seconds']
                        print(f'[{len(history)+1}/{manifest["candidate_pairs"]}] {pair_id} cap={cap:.1f}s', flush=True)
                        if image is None and setup_error is None:
                            try:
                                image, pulled = get_image(client, Manifest(study/task['path']))
                                gold = gold_directory(study, task, image.id, cap)
                            except Exception as error:
                                setup_error = type(error).__name__ + ': ' + str(error)[:700]
                        if setup_error:
                            result = {'status': 'infrastructure_invalid', 'seconds': None,
                                      'cap_seconds': cap, 'base_task_success': None, 'error': setup_error}
                            atomic_save(directory/'result.json', result)
                        else:
                            old = previous/'samples'/task['key']/persona/'worker'
                            replay = all((old/name).exists() for name in ('predictions.json','transcript.json','protocol.json'))
                            if replay:
                                command = [sys.executable, '-u', str(HERE/'replay_roleplay.py'), '--task', str(study/task['path']),
                                    '--persona', persona, '--replay-worker', str(old)]
                            else:
                                command = [sys.executable, '-u', str(HERE/'roleplay.py'), '--task', str(study/task['path']),
                                    '--persona', persona, '--gold-dir', str(gold), '--model', args.model,
                                    '--max-turns', str(config['max_turns']), '--max-steps', str(config['max_steps'])]
                            result = supervise_sample(command, directory, cap)
                            if replay:
                                result.update(cohort='historical_generation', source_worker=str(old),
                                    status='historical_rescored' if result['status']=='completed' else 'historical_'+result['status'])
                                atomic_save(directory/'result.json', result)
                    record = {key: result.get(key) for key in ('status','seconds','cap_seconds','base_task_success')}
                    record.update(pair_id=pair_id, result_sha256=file_hash(directory/'result.json'))
                    history.append(record)
                    state = advance_budget(state, history, config['maximum_seconds'])
                    atomic_save(study/'online-budget.json', state)
                    atomic_save(study/'results.json', history)
                    report = summarize(history, manifest['candidate_pairs'], state)
                    atomic_save(study/'summary.json', report)
                    print(f'  {record["status"]}; original={record["base_task_success"]}; next cap={state["next_cap_seconds"]:.1f}s', flush=True)
            finally:
                if image is not None:
                    release_image(client, image, pulled, False)
    finally:
        client.close()
        if os.name == 'nt':
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        lock.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
