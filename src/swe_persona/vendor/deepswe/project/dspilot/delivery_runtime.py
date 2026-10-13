"""Portable DeepSWE entrypoints. Standard library only until a runtime is selected.

The immutable dataset is copied to an external work directory for fresh freezes.
Existing attempts are never replayed automatically, including failed attempts.
"""
import argparse
from collections import Counter
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import tomllib

DEFAULT_VERIFIER_SECONDS = 900


def verifier_seconds(plan, original_seconds):
    return plan.get('pier_verifier_timeout_sec', min(original_seconds, DEFAULT_VERIFIER_SECONDS))


def read(path):
    return json.loads(Path(path).read_text())


def dump(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temp.replace(path)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''): h.update(block)
    return h.hexdigest()


def safe(root, rel):
    p = Path(rel)
    if p.is_absolute() or '..' in p.parts:
        raise ValueError('Unsafe relative path')
    out = root / p
    if not out.resolve().is_relative_to(root.resolve()) or any(x.is_symlink() for x in (out, *out.parents)):
        raise ValueError('Symlink/outside path is not supported')
    return out


def task_path(dataset, pair):
    return safe(dataset, 'tasks/' + pair['delivery_id'])


def pairs(dataset, selected=None):
    rows = read(dataset / 'inventory.json')['pairs']
    if selected:
        ids = set(selected)
        rows = [p for p in rows if p['delivery_id'] in ids]
        if {p['delivery_id'] for p in rows} != ids:
            raise ValueError('Unknown delivery pair ID')
    return rows


def verify_delivery_artifacts(dataset, folder, pair, plan):
    """Validate optional prospective receipts without upgrading historical data."""
    ext = folder / 'tests/persona'
    manifest_path = ext / 'closed_loop/manifest.json'
    if manifest_path.exists():
        manifest = read(manifest_path)
        for name, digest in manifest.get('files', {}).items():
            if sha(safe(ext / 'closed_loop', name)) != digest:
                raise ValueError('Rule manifest checksum mismatch: ' + name)
        if 'source_binding_sha256' in manifest:
            rubric = read(ext / 'closed_loop/rubric.json')
            if (manifest['source_binding_sha256'] != sha(ext / 'binding.json')
                    or rubric.get('source_binding_sha256') != manifest['source_binding_sha256']
                    or manifest.get('protocol_version') != plan['protocol']):
                raise ValueError('Rule source binding/protocol mismatch')
            for name in ('experience_rubric.json', 'experience_prompt.md', 'experience_schema.json'):
                if name not in manifest['files']: raise ValueError('Missing UX rule: ' + name)
    blur_dir = ext / 'receipts/blur'
    if blur_dir.exists():
        receipt = read(blur_dir / 'receipt.json'); blur = read(blur_dir / 'task-blur.json')
        if (receipt.get('task_id') != pair['task_id'] or blur.get('task_id') != pair['task_id']
                or receipt.get('output_sha256') != sha(blur_dir / 'task-blur.json')
                or receipt.get('task_only_process') is not True
                or receipt.get('persona_inputs') != [] or receipt.get('persona_loaded_in_generator') is not False):
            raise ValueError('Task-only blur receipt mismatch')
        for name, digest in receipt['inputs'].items():
            if sha(safe(dataset / 'runtime/project', name)) != digest:
                raise ValueError('Task-only blur source mismatch: ' + name)
        exported = read(ext / 'blurred_task.json') if (ext / 'blurred_task.json').exists() else {}
        if 'stage_summary' in blur and exported.get('stage_summary') != blur['stage_summary']:
            raise ValueError('Task stage summary differs from frozen task-only receipt')
    opening_path = ext / 'receipts/opening.json'
    interaction_path = ext / 'interaction.json'
    interaction = read(interaction_path) if interaction_path.exists() else {}
    if opening_path.exists():
        receipt = read(opening_path)
        expected = {'task_id': pair['task_id'], 'persona_id': pair['persona_id'], 'arm': 'pivot',
                    'complete_first_user_message': True, 'agent_actions_before_freeze': 0,
                    'freeze_phase': 'before_first_user_delivery_and_agent_action', 'historical_backfill': False,
                    'instruction_sha256': sha(folder / 'instruction.md'),
                    'blur_sha256': sha(blur_dir / 'task-blur.json'),
                    'blur_receipt_sha256': sha(blur_dir / 'receipt.json'),
                    'plan_sha256': hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()}
        if any(receipt.get(k) != v for k, v in expected.items()):
            raise ValueError('Frozen opening receipt mismatch')
        if interaction.get('opening') != (folder / 'instruction.md').read_text() or interaction.get('complete_opening_frozen') is not True:
            raise ValueError('Interaction differs from frozen complete opening')
    elif interaction.get('complete_opening_frozen'):
        raise ValueError('Complete opening claimed without a receipt')


def verify_dataset(dataset):
    dataset = Path(dataset).resolve()
    lock = read(dataset / 'release-lock.json')['files']
    actual = {p.relative_to(dataset).as_posix() for p in dataset.rglob('*') if p.is_file()}
    if actual != set(lock) | {'release-lock.json', 'SHA256SUMS'}:
        raise ValueError('Dataset file inventory differs from release lock')
    for rel, digest in lock.items():
        if sha(safe(dataset, rel)) != digest:
            raise ValueError('Dataset checksum mismatch: ' + rel)
    rows = pairs(dataset)
    if len({p['delivery_id'] for p in rows}) != len(rows):
        raise ValueError('Duplicate pair ID')
    for pair in rows:
        folder = task_path(dataset, pair); ext = folder / 'tests/persona'
        protocol = read(ext / 'protocol.json')
        plan = read(ext / 'legacy-plan.json')
        policy = read(ext / 'closed_loop/policy.json')
        config = tomllib.loads((folder / 'task.toml').read_text())
        original = tomllib.loads((ext / 'original/task.toml').read_text())
        if (ext / 'runtime-settings.json').exists():
            settings = read(ext / 'runtime-settings.json')
            expected = {'original_agent_timeout_sec': original['agent']['timeout_sec'],
                'pier_agent_timeout_sec': plan.get('pier_agent_timeout_sec', original['agent']['timeout_sec']),
                'pier_agent_timeout_overridden': 'pier_agent_timeout_sec' in plan,
                'interaction_seconds': plan['budgets']['interaction_seconds'],
                'final_review_seconds': plan['budgets']['final_review_seconds'],
                'verifier_timeout_sec': original['verifier']['timeout_sec']}
            if settings.get('schema_version') in ('deepswe-time-limits-v2','deepswe-time-limits-v3'):
                expected.update(original_verifier_timeout_sec=original['verifier']['timeout_sec'],
                    verifier_timeout_sec=verifier_seconds(plan, original['verifier']['timeout_sec']),
                    verifier_max_attempts=1)
            if settings.get('schema_version') == 'deepswe-time-limits-v3':
                expected['coding_seconds'] = plan['budgets'].get('coding_seconds',3600)
            if any(settings.get(k) != v for k, v in expected.items()):
                raise ValueError('Runtime time limits differ from the frozen plan/original verifier')
        added = config.get('metadata', {}).pop('deepswe_persona', None)
        if added and added.get('harbor_task_name'):
            if config.get('task', {}).get('name') != added['harbor_task_name']:
                raise ValueError('Harbor task name differs from frozen pair identity')
            if 'name' in original.get('task', {}): config['task']['name'] = original['task']['name']
            else:
                config['task'].pop('name', None)
                if not config['task'] and 'task' not in original: config.pop('task')
        if not original.get('metadata') and not config.get('metadata'): config.pop('metadata', None)
        if config != original or not added or added['pair_id'] != pair['delivery_id']:
            raise ValueError('Original task settings changed or persona entrypoint mismatched')
        if protocol['runner']['pair_id'] != pair['delivery_id'] or protocol['source_protocol'] != plan['protocol']:
            raise ValueError('Protocol/plan mismatch')
        if any(policy[k] != plan[k] for k in ('stages', 'requirements', 'budgets')) or protocol['budgets'] != plan['budgets']:
            raise ValueError('Executable policy differs from the frozen plan')
        rules = read(ext / 'closed_loop/rubric.json')['requirements']
        if {r['requirement_id'] for r in rules} != set(plan['requirements']):
            raise ValueError('Rubric requirement coverage mismatch')
        for rule in rules:
            if rule['lifecycle'] != plan['requirements'][rule['requirement_id']]:
                raise ValueError('Rubric lifecycle differs from frozen plan')
        for name in ('checker.py', 'closed_loop/response_schema.json', 'closed_loop/judge_prompt.md',
                     'closed_loop/simulator_prompt.md', 'closed_loop/rubric.json'):
            if not (ext / name).is_file(): raise ValueError('Missing task rule: ' + name)
        verify_delivery_artifacts(dataset, folder, pair, plan)
    manifest_path = dataset / 'release-manifest.json'
    manifest = read(manifest_path) if manifest_path.exists() else {}
    if manifest.get('metadata_version') == 'deepswe-delivery-metadata-v1':
        entries = manifest['task_entries']
        if len(entries) != len(rows) or {e['pair_id'] for e in entries} != {r['delivery_id'] for r in rows}:
            raise ValueError('Release manifest pair coverage mismatch')
        if sha(safe(dataset, manifest['source']['path'])) != manifest['source']['sha256']:
            raise ValueError('Release manifest source index mismatch')
        for entry in entries:
            folder = safe(dataset, entry['task_directory'])
            for name, digest in entry['files'].items():
                if sha(safe(folder, name)) != digest: raise ValueError('Release task entry checksum mismatch')
    return {'passed': True, 'pairs': len(rows), 'files_verified': len(lock),
            'scope': 'DeepSWE staged package integrity; no live model or Docker validation',
            'model_calls': 0, 'docker_calls': 0}


def work_root(dataset, output):
    output = Path(output).absolute()
    if '..' in output.parts or any(p.is_symlink() for p in (output, *output.parents)):
        raise ValueError('Unsafe work directory')
    if output.is_relative_to(dataset) or dataset.is_relative_to(output):
        raise ValueError('Work directory must be outside the immutable dataset')
    return output


def worker_env(project, pair, runtime, env_file=None):
    env = os.environ.copy()
    for key in ('DS_TASK_ID', 'DS_PERSONA_ID', 'DS_PROTOCOL', 'DS_STAGED_PLAN_PATH', 'DS_RUNTIME', 'DS_CODING_BACKEND', 'PYTHONPATH'):
        env.pop(key, None)
    # Keep the exact host package that launched this worker. A private wheel
    # installation may not be on site-packages; discarding its root silently
    # imports a different installed release in the child. Do not inherit
    # arbitrary ambient import paths into the portable project.
    paths = [str(project)]
    package = sys.modules.get('swe_persona')
    package_file = getattr(package, '__file__', None)
    if package_file:
        paths.append(str(Path(package_file).resolve().parent.parent))
    env.update(PYTHONPATH=os.pathsep.join(paths), PYTHONDONTWRITEBYTECODE='1', DS_TASK_ID=pair['task_id'],
               DS_PERSONA_ID=pair['persona_id'], DS_PROTOCOL='staged', DS_CODING_BACKEND='json_act' if runtime=='harbor' else 'native_act', DS_RUNTIME=runtime)
    if env_file: env['DS_PERSONA_ENV_FILE'] = str(Path(env_file).resolve())
    return env


def command(project, pair, runtime, operation, *, env_file=None, arguments=(), log=None):
    argv = [sys.executable, '-B', '-m', 'dspilot.delivery_worker', operation, *arguments]
    kwargs = dict(cwd=project, env=worker_env(project, pair, runtime, env_file), text=True)
    if log:
        with Path(log).open('w') as out:
            return subprocess.run(argv, stdout=out, stderr=subprocess.STDOUT, **kwargs).returncode
    result = subprocess.run(argv, capture_output=True, **kwargs)
    if result.returncode:
        # No raw credential/config exceptions are exposed by this wrapper.
        raise ValueError(f'{operation} failed: ' + result.stderr[-1800:])
    return json.loads(result.stdout)


def initialize(dataset, output):
    identity = sha(dataset / 'release-lock.json')
    receipt = output / 'dataset-identity.json'
    if receipt.exists():
        if read(receipt)['sha256'] != identity:
            raise ValueError('Work directory belongs to a different dataset release')
    else:
        if any(output.iterdir()): raise ValueError('Use an empty/new work directory')
        shutil.copytree(dataset / 'runtime/project', output / 'project')
        dump(receipt, {'sha256': identity, 'dataset_name': read(dataset / 'inventory.json')['dataset_name']})
    return output / 'project'


def run_pair(dataset, output, pair, runtime='legacy', arm='pivot', env_file=None, attempt='01', preflight=False):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,30}', attempt): raise ValueError('Invalid attempt ID')
    plan_file = task_path(dataset, pair) / 'tests/persona/legacy-plan.json'
    if plan_file.exists() and any(k in read(plan_file) for k in ('pier_agent_timeout_sec', 'pier_verifier_timeout_sec')) and runtime not in ('pier_native', 'harbor'):
        raise ValueError('Explicit time limits require --runtime pier_native or harbor')
    project = output / 'project'
    token = hashlib.sha256((pair['delivery_id'] + ':' + arm + ':' + attempt).encode()).hexdigest()[:20]
    pipeline_id = 'delivery-' + token
    statefile = output / 'attempts' / (pipeline_id + '.json')
    if statefile.exists():
        previous = read(statefile)
        if previous['runtime'] != runtime: raise ValueError('Existing attempt uses a different runtime')
        return {**previous, 'skipped_existing_attempt': True}
    row = {'pair_id': pair['delivery_id'], 'task_id': pair['task_id'], 'persona_id': pair['persona_id'],
           'arm': arm, 'runtime': runtime, 'attempt': attempt, 'pipeline_id': pipeline_id,
           'status': 'starting', 'started_at_unix': time.time()}
    dump(statefile, row)
    try:
        common = ('--pipeline-id', pipeline_id, '--arm', arm)
        if arm == 'pivot':
            folder = task_path(dataset, pair); ext = folder / 'tests/persona'
            if (ext / 'receipts/opening.json').exists():
                verify_delivery_artifacts(dataset, folder, pair, read(ext / 'legacy-plan.json'))
                destination = project / 'runs/delivery-inputs' / pipeline_id
                destination.mkdir(parents=True, exist_ok=False)
                for source, name in [(folder/'instruction.md', 'instruction.md'),
                        (ext/'receipts/opening.json', 'opening-receipt.json'),
                        (ext/'receipts/blur/task-blur.json', 'task-blur.json'),
                        (ext/'receipts/blur/receipt.json', 'blur-receipt.json')]:
                    shutil.copy2(source, destination/name)
                common += ('--opening-directory', str(destination))
        result = command(project, pair, runtime, 'preflight', env_file=env_file, arguments=common)
        dump(output / 'preflight' / (pipeline_id + '.json'), result)
        if preflight:
            row['status'] = 'preflight_passed'
        else:
            log = output / 'task-logs' / (pipeline_id + '.log'); log.parent.mkdir(exist_ok=True)
            row['status'] = 'running'; dump(statefile, row)
            code = command(project, pair, runtime, 'run', env_file=env_file, arguments=common, log=log)
            state = read(project / 'runs/staged-pipelines' / pipeline_id / 'state.json')
            row.update(status=state['status'], returncode=code, log=str(log.relative_to(output)),
                       run_id=state.get('run_id'), result=state.get('result'))
            if row['result']:
                row['result'] = 'project/' + row['result']
    except BaseException as exc:
        row.update(status='interrupted' if isinstance(exc, (KeyboardInterrupt, SystemExit)) else 'infrastructure_error',
                   error_type=type(exc).__name__, error=str(exc))
        if isinstance(exc, (KeyboardInterrupt, SystemExit)): raise
    finally:
        row['finished_at_unix'] = time.time(); dump(statefile, row)
    return row


def execute(dataset, output, selected, *, runtime='legacy', arm='pivot', env_file=None,
            attempt='01', resume=False, preflight=False, image_map=None, calibrate=False, experience=False):
    verify_dataset(dataset)
    if runtime not in ('pier_native', 'harbor') and any(any(k in
            read(task_path(dataset, pair) / 'tests/persona/legacy-plan.json')
            for k in ('pier_agent_timeout_sec', 'pier_verifier_timeout_sec')) for pair in selected):
        raise ValueError('Explicit time limits require --runtime pier_native or harbor')
    output = work_root(dataset, output)
    output.mkdir(parents=True, exist_ok=True)
    # Lock outside work contents so an empty directory can be initialized atomically.
    with output.with_name(output.name + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (output / 'dataset-identity.json').exists() and not resume:
            raise ValueError('Work directory exists; use --resume or a fresh directory')
        project = initialize(dataset, output)
        selection = [p['delivery_id'] for p in selected]
        plan_path = output / 'run-plan.json'
        plan = {'dataset_sha256': sha(dataset / 'release-lock.json'), 'pairs': selection,
                'runtime': runtime, 'arm': arm, 'attempt': attempt, 'preflight_only': preflight,
                'image_overrides': image_map or {}, 'minimal_judge_calibration': calibrate,
                'experience_scoring': experience, 'model_identity': model_identity(env_file)}
        if plan_path.exists() and read(plan_path) != plan:
            raise ValueError('Resume requires the same selection, arm, runtime, attempt and mode')
        dump(plan_path, plan)
        for task_id, image in (image_map or {}).items():
            if task_id not in {p['task_id'] for p in selected} or not isinstance(image, str) or not image:
                raise ValueError('Image map must name selected tasks and nonempty image identities')
            path = project / 'data/tasks' / (task_id + '.json')
            task = read(path); task['image'] = image; dump(path, task)
        sources = {}
        for source in [project/'data/personas.v3.json', *[project/'data/tasks'/ (p['task_id']+'.json') for p in selected],
                       *[project/'data/bindings'/ (p['task_id']+'--'+p['persona_id']+'.json') for p in selected]]:
            if source.exists(): sources[source.relative_to(project).as_posix()] = sha(source)
        pins = output / 'source-pins.json'
        if pins.exists() and read(pins) != sources:
            raise ValueError('Batch sources changed since the initial freeze')
        dump(pins, sources)
        if calibrate and not preflight:
            try:
                # Resolve host/container prerequisites before spending calibration calls.
                for pair in {p['task_id']: p for p in selected}.values():
                    environment = command(project, pair, runtime, 'environment', env_file=env_file)
                    dump(output / 'preflight' / (pair['task_id'] + '.json'), environment)
                diagnostic = command(project, selected[0], runtime, 'calibrate', env_file=env_file,
                    arguments=('--dataset', str(dataset), '--output', str(output / 'calibration'), '--calibration-reuse'))
                if not diagnostic['passed']: raise ValueError('Minimal judge calibration failed; no pair was started')
            except Exception:
                dump(output / 'status.json', {'status': 'calibration_or_preflight_blocked',
                     'total': len(selected), 'no_new_pairs_started': True,
                     'experiment_started': any((output / 'attempts').glob('*.json'))})
                raise
        rows = []
        for pair in selected:
            previous_experience = os.environ.get('DS_DELIVERY_EXPERIENCE')
            os.environ['DS_DELIVERY_EXPERIENCE'] = '1' if experience else '0'
            try:
                rows.append(run_pair(dataset, output, pair, runtime, arm, env_file, attempt, preflight))
            finally:
                if previous_experience is None: os.environ.pop('DS_DELIVERY_EXPERIENCE', None)
                else: os.environ['DS_DELIVERY_EXPERIENCE'] = previous_experience
            dump(output / 'status.json', {'total': len(selected), 'recorded': len(rows),
                                         'counts': dict(Counter(r['status'] for r in rows))})
            dump(output / 'results.json', {'entries': rows, 'scope': 'DeepSWE staged outcomes, not TB3 outcomes'})
        # Export all actual attempts using the same historical-layout adapter; no grade is changed.
        jobs = [{'id': r['pipeline_id'], 'run_id': r.get('run_id') or 'pipeline-' + r['pipeline_id'] + '-' + arm,
                 'task_id': r['task_id'], 'persona_id': r['persona_id'], 'arm': arm} for r in rows if not preflight]
        if jobs and not (output / 'delivery-results').exists():
            batch = project / 'runs/delivery-batch'; dump(batch / 'manifest.json', {'batch_id': 'delivery-batch', 'jobs': jobs, 'sources': sources,
                'models': plan['model_identity'].get('models'),
                'backend': {'runtime': runtime, 'coding_backend': 'json_act' if runtime=='harbor' else 'native_act'},
                'delivery_protocol': 'deepswe-prospective-delivery-v1'})
            for row in rows:
                status = row['status']
                mapped = 'execution_failed' if status in ('infrastructure_error', 'blocked') else 'finished' if status == 'complete' else status
                dump(batch / 'attempts' / (row['pipeline_id'] + '.json'),
                     {**row, 'status': mapped, 'delivery_attempt_status': status})
            command(project, selected[0], runtime, 'export-results', arguments=('--output', str(output / 'delivery-results')))
        return {'entries': rows, 'output': str(output), 'experiment_started': not preflight}


def model_identity(env_file):
    """Freeze effective role routes using the same resolver as the API client."""
    from .api_config import config, public_routes
    host_keys = ('DS_PERSONA_ENV_FILE', 'API_PROVIDER', 'API_KEY', 'OPENAI_API_KEY', 'AZURE_OPENAI_API_KEY',
                 'AGENT_MODEL', 'SIMULATOR_MODEL', 'JUDGE_MODEL', 'PCB_MODEL')
    if env_file is None and not any(os.environ.get(k) for k in host_keys):
        return {'status': 'not_supplied', 'models': None, 'endpoint_sha256': None}
    values = config(env_file)
    routes = public_routes(values)
    return {'status': 'recorded', 'models': {k: values.get(k) or values['AGENT_MODEL'] for k in
        ('AGENT_MODEL', 'SIMULATOR_MODEL', 'JUDGE_MODEL')},
        'endpoint_sha256': routes['agent']['endpoint_sha256'], 'api_routes': routes}


def main(dataset=None, batch=False):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pair', action='append' if batch else 'store', required=not batch)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--runtime', choices=('legacy', 'pier_native', 'harbor'), default='harbor')
    parser.add_argument('--arm', choices=('pivot', 'baseline', 'control'), default='pivot')
    parser.add_argument('--env-file', type=Path, help='Host credential file; never copied into the package/container')
    parser.add_argument('--attempt', default='01')
    parser.add_argument('--image-map', type=Path, help='Optional task_id -> rebuilt image identity map; recorded before fresh freeze')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--dry-run', action='store_true', help='Check package and selected plans; no Docker/models/work directory')
    parser.add_argument('--preflight-only', action='store_true', help='Fresh freeze and Docker preflight; no model calls. Use a separate output for execution.')
    parser.add_argument('--skip-calibration', action='store_true', help='Explicitly skip the default batch judge gate; recorded in run-plan.json')
    parser.add_argument('--calibrate', action='store_true', help='Also require minimal judge calibration for a single run')
    parser.add_argument('--experience', action='store_true', help='Optional post-episode visible-dialogue UX scoring; does not change benchmark acceptance')
    args = parser.parse_args()
    dataset = Path(dataset).resolve()
    selected = pairs(dataset, args.pair if batch else [args.pair])
    if args.dry_run:
        value = verify_dataset(dataset)
        project = dataset / 'runtime/project'
        checked = command(project, selected[0], 'legacy', 'validate', arguments=('--pairs-json', json.dumps(selected)))
        value.update(selected_pairs=len(selected), plans=checked, requested_runtime=args.runtime,
                     runtime_dependencies_checked=False, experiment_started=False,
                     online_calibration_required=(batch or args.calibrate) and not args.skip_calibration)
    else:
        if not args.output: parser.error('--output is required for execution/preflight')
        if not args.env_file:
            from .api_config import config, ROOT as config_root
            # Pass a discovered file through to the isolated worker as well;
            # otherwise the wrapper and worker could resolve different defaults.
            selected_file = os.environ.get('DS_PERSONA_ENV_FILE')
            if selected_file or (config_root / '.env').is_file():
                args.env_file = Path(selected_file or config_root / '.env').resolve()
            config(args.env_file)  # Validate before creating an execution.
        value = execute(dataset, args.output, selected, runtime=args.runtime, arm=args.arm, env_file=args.env_file,
                        attempt=args.attempt, resume=args.resume, preflight=args.preflight_only,
                        image_map=read(args.image_map) if args.image_map else None,
                        calibrate=(batch or args.calibrate) and not args.skip_calibration, experience=args.experience)
    print(json.dumps(value, ensure_ascii=False, indent=2))
    if any(r['status'] not in ('complete', 'preflight_passed') for r in value.get('entries', [])):
        raise SystemExit(1)
