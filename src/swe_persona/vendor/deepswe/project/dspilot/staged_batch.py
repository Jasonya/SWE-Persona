"""Immutable staged cohorts: prepare all pairs separately, or legacy per-pair execution."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from .coding_backend import backend_metadata, backend_sources
from . import specs
from .api import config
from .planning_staged import digest, dump, source_manifest
from .report_policy import exclusion_reason

VERSION = 'staged-batch-v5-separate-preparation'


def now():
    return datetime.now(timezone.utc).isoformat()


def directory_for(batch_id):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,39}', batch_id):
        raise ValueError('batch-id must be 1..40 letters/digits/hyphens/underscores')
    path = specs.ROOT / 'runs/staged-batches' / batch_id
    if path.resolve() != specs.ROOT.resolve() / 'runs/staged-batches' / batch_id:
        raise ValueError('Batch paths cannot use symlinks')
    return path


def inventory():
    pairs = []
    for path in sorted((specs.ROOT / 'data/staged_bindings').glob('*.json')):
        raw = json.loads(path.read_text())
        pairs.append({'task_id': raw['task_id'], 'persona_id': raw['persona_id']})
    if not pairs or len({(p['task_id'], p['persona_id']) for p in pairs}) != len(pairs):
        raise ValueError('Empty or duplicate cohort')
    return pairs


def fingerprint(pairs):
    sources = {}
    for pair in pairs:
        sources.update(source_manifest(specs.compile_episode(**pair)))
    # Also seal every runtime module, including this scheduler and reporting policy.
    for path in sorted((specs.ROOT / 'dspilot').glob('*.py')) + backend_sources(specs.ROOT):
        sources[str(path.relative_to(specs.ROOT))] = digest(path)
    cfg = config()
    return {'sources': sources, 'coding_backend': backend_metadata(), 'models': {
        role: cfg.get(key) or cfg['AGENT_MODEL'] for role, key in (
            ('agent', 'AGENT_MODEL'), ('simulator', 'SIMULATOR_MODEL'), ('judge', 'JUDGE_MODEL'))}}


def read(path):
    return json.loads(path.read_text()) if path.is_file() else {}


def report(directory, manifest, attempts):
    rows = []
    for pair in manifest['pairs']:
        attempt = attempts.get(pair['pipeline_id'], {})
        pipeline_dir = specs.ROOT / 'runs/staged-pipelines' / pair['pipeline_id']
        state = read(pipeline_dir / 'state.json')
        summary = read(pipeline_dir / 'summary.json')
        reason = exclusion_reason(pair['task_id'])
        timing = summary.get('timings') or {}
        row = dict(pair, attempt_status=attempt.get('status', 'pending'),
                   pipeline_status=state.get('status'), reasons=state.get('reasons', []),
                   plan=state.get('plan'), missing_information=state.get('missing_information',[]),
                   last_design_feedback=state.get('feedback',[])[-1:],
                   review=state.get('review'),
                   model_calibrated=bool(state.get('model_calibrated')),
                   admission_policy=state.get('admission_policy'),
                   review_policy=state.get('review_policy'),
                   preparation_warnings=state.get('preparation_warnings',[]),
                   calibration_policy=state.get('calibration_policy'),
                   calibration_status=state.get('calibration_status', 'legacy_unrecorded'),
                   coding_backend=summary.get('coding_backend', backend_metadata('native_act')),
                   result=summary.get('result'), end_reason=summary.get('end_reason'),
                   evaluation_valid=summary.get('evaluation_valid'),
                   full_task_disclosed=summary.get('full_task_disclosed'),
                   final_delivery=summary.get('end_reason') == 'final_delivery',
                   task_disclosure_mode=summary.get('task_disclosure_mode') or pair.get('task_disclosure_mode'),
                   original_score_exclusion=reason,
                   original_metric_eligible=(summary.get('evaluation_valid') is True and
                                             not reason and summary.get('original_evaluation_valid') is True),
                   persona_metric_eligible=(summary.get('evaluation_valid') is True and
                                            summary.get('persona_evaluation_valid') is True and
                                            not summary.get('persona_evaluation_exclusion')),
                   persona_evaluation_exclusion=summary.get('persona_evaluation_exclusion'),
                   raw_base_pass=summary.get('base_pass'), raw_joint_pass=summary.get('joint_pass'),
                   persona_pass=summary.get('persona_pass'),
                   staged_execution_seconds=timing.get('staged_execution_seconds'),
                   stage_seconds=timing.get('stage_seconds'), preparation_time_included=False)
        if attempt.get('error_type'):
            row['error_type'] = attempt['error_type']
        rows.append(row)
    finished = sum(r['attempt_status'] in ('finished', 'interrupted') for r in rows)
    times = [r['staged_execution_seconds'] for r in rows if r['staged_execution_seconds'] is not None]
    valid_times = [r['staged_execution_seconds'] for r in rows
                   if r['evaluation_valid'] is True and r['staged_execution_seconds'] is not None]
    original = [r for r in rows if r['original_metric_eligible']]
    persona = [r for r in rows if r['persona_metric_eligible']]
    joint = [r for r in original if r['persona_metric_eligible'] and r['evaluation_valid'] is True]
    result = {'version': VERSION, 'batch_id': manifest['batch_id'], 'updated_at': now(),
              'status': 'stopped' if (directory / 'stop-request.json').is_file() else 'finished' if finished == len(rows) else 'in_progress',
              'stop_request': read(directory / 'stop-request.json') or None,
              'phase': manifest.get('phase','run'),
              'pair_count': len(rows), 'finished_attempts': finished,
              'completed_executions':sum(r['pipeline_status']=='complete' for r in rows),
              'failed_execution_attempts':sum(r['pipeline_status'] in ('execution_failed','blocked','basic_check_failed') or
                  attempts.get(r['pipeline_id'],{}).get('returncode',0)!=0 or r['attempt_status']=='interrupted' for r in rows),
              'prepared_pairs': sum(r['pipeline_status']=='prepared' for r in rows),
              'pipeline_status_counts': dict(Counter(r['pipeline_status'] or r['attempt_status'] for r in rows)),
              'final_deliveries': sum(r['final_delivery'] for r in rows),
              'formal_task': {'eligible': len(original), 'passed': sum(r['raw_base_pass'] is True for r in original)},
              'formal_persona': {'eligible': len(persona), 'passed': sum(r['persona_pass'] is True for r in persona)},
              'formal_joint': {'eligible': len(joint), 'passed': sum(r['raw_joint_pass'] is True for r in joint)},
              'timed_episodes': len(times), 'staged_execution_seconds_total': sum(times) if times else None,
              'valid_timed_episodes': len(valid_times),
              'valid_staged_execution_seconds_total': sum(valid_times) if valid_times else None,
              'timing_scope': 'Only delivered staged interactions; preparation, calibration, setup and final evaluation excluded. All-attempt totals retain interrupted execution cost; valid totals include only evaluation_valid=true episodes. Missing time is not zero.',
              'notice': 'Finished attempts include blocked/failed/interrupted pairs. Pipeline complete does not imply final delivery or task success.',
              'pairs': rows}
    return result


def invoke(pair, manifest, log):
    env = os.environ.copy()
    for key in ('DS_STAGED_PLAN_PATH', 'DS_TASK_ID', 'DS_PERSONA_ID', 'DS_PROTOCOL'):
        env.pop(key, None)
    with log.open('w') as output:
        return subprocess.run([
            sys.executable, '-u', '-m', 'dspilot.staged_pipeline', manifest.get('phase','run'),
            '--pipeline-id', pair['pipeline_id'], '--task-id', pair['task_id'],
            '--persona-id', pair['persona_id'], '--arm', manifest['arm'],
            '--max-revisions', str(manifest['max_revisions'])],
            cwd=specs.ROOT, env=env, stdout=output, stderr=subprocess.STDOUT).returncode


def execute(batch_id, *, command='run', arm='pivot', max_revisions=2, launcher=invoke):
    if command not in ('prepare','run','status'):raise ValueError('Unknown batch command')
    directory = directory_for(batch_id)
    if command == 'status' or (directory / 'stop-request.json').is_file():
        return report(directory, read(directory / 'manifest.json'), read(directory / 'attempts.json'))
    directory.parent.mkdir(parents=True, exist_ok=True)
    with (directory.parent / f'.{batch_id}.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pairs = inventory()
        seal = fingerprint(pairs)
        if directory.exists():
            manifest = read(directory / 'manifest.json')
            if (manifest.get('phase','run') != command or manifest.get('seal') != seal or manifest.get('arm') != arm or
                    manifest.get('max_revisions') != max_revisions or
                    [{k: p[k] for k in ('task_id', 'persona_id')} for p in manifest.get('pairs', [])] != pairs):
                raise ValueError('Batch sources, models, inventory or options changed; resume rejected')
        else:
            manifest = {'version': VERSION, 'batch_id': batch_id, 'created_at': now(),
                        'phase':command,'arm': arm, 'max_revisions': max_revisions, 'seal': seal,
                        'pairs': [dict(p, pipeline_id=f'{batch_id}-{i:02d}') for i, p in enumerate(pairs, 1)]}
            for pair in manifest['pairs']:
                if (specs.ROOT / 'runs/staged-pipelines' / pair['pipeline_id']).exists():
                    raise ValueError('Pipeline ID already exists; no existing attempts may be relabeled')
            dump(directory / 'manifest.json', manifest)
        attempts = read(directory / 'attempts.json')
        for pair in manifest['pairs']:
            if (directory/'stop-request.json').is_file():break
            key = pair['pipeline_id']
            if key in attempts:
                if attempts[key]['status'] == 'launching':
                    # Lost process ownership: retain all evidence and never repeat a paid attempt.
                    attempts[key].update(status='interrupted', ended_at=now())
                    dump(directory / 'attempts.json', attempts)
                continue
            if fingerprint(pairs) != seal:
                raise ValueError('Batch source/model drift detected; no further pair launched')
            attempts[key] = {'status': 'launching', 'started_at': now()}
            dump(directory / 'attempts.json', attempts)
            dump(directory / 'report.json', report(directory, manifest, attempts))
            print(f"START {key} {pair['task_id']} / {pair['persona_id']}", flush=True)
            try:
                code = launcher(pair, manifest, directory / f'{key}.log')
                attempts[key].update(status='finished', returncode=code, ended_at=now())
            except Exception as exc:
                attempts[key].update(status='interrupted', error_type=type(exc).__name__, ended_at=now())
            dump(directory / 'attempts.json', attempts)
            result = report(directory, manifest, attempts)
            dump(directory / 'report.json', result)
            state = read(specs.ROOT / 'runs/staged-pipelines' / key / 'state.json')
            print(f"END {key} {state.get('status', 'no_pipeline_state')} ({result['finished_attempts']}/{len(pairs)})", flush=True)
        result = report(directory, manifest, attempts)
        dump(directory / 'report.json', result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'run', 'status'))
    parser.add_argument('--batch-id', required=True)
    parser.add_argument('--arm', choices=('pivot', 'control', 'baseline'), default='pivot')
    parser.add_argument('--max-revisions', type=int, choices=range(4), default=2)
    args = parser.parse_args()
    if args.command=='run':
        parser.error('Combined planning/execution is retired. Use staged_release freeze, check, then run; existing batch status remains readable.')
    result = execute(args.batch_id, command=args.command, arm=args.arm, max_revisions=args.max_revisions)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
