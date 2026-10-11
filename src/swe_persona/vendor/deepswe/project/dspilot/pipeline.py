"""Preview or execute a full task/persona cross product from CLI flags or JSON."""
import argparse
import json
from pathlib import Path
import re
import tempfile

from . import pairing, specs
from .pilot_2x2 import launch, prepare_task, published_by_candidate, source_fingerprint


def selection(values, label):
    if (not isinstance(values, list) or not values or
            any(not isinstance(value, str) or not value for value in values)):
        raise ValueError(f'{label} must be a nonempty list of IDs')
    if len(set(values)) != len(values):
        raise ValueError(f'Duplicate IDs in {label}')
    return sorted(values)


def plan(prefix, tasks, personas, repeats=1, skip_human_review=False):
    if not isinstance(prefix, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,69}', prefix):
        raise ValueError('Prefix must contain 1-70 letters, numbers, hyphens or underscores')
    tasks, personas = selection(tasks, 'tasks'), selection(personas, 'personas')
    if type(repeats) is not int or repeats < 1:
        raise ValueError('repeats must be a positive integer')
    if type(skip_human_review) is not bool:
        raise ValueError('skip_human_review must be true or false')
    for task in tasks:
        specs.load_task(task)
    unknown = set(personas) - set(specs.persona_catalog())
    if unknown:
        raise ValueError(f'Unknown personas: {sorted(unknown)}')
    pairs = [{'task_id': task, 'persona_id': persona,
              'candidate': f'{prefix}--{task}--{persona}'}
             for task in tasks for persona in personas]
    if any(len(item['candidate'].encode()) > 240 for item in pairs):
        raise ValueError('Candidate filename too long; use a shorter prefix')
    return {'version': 'task-persona-pipeline-v1', 'prefix': prefix,
            'tasks': tasks, 'personas': personas, 'repeats': repeats,
            'review_mode': 'skip_human_review' if skip_human_review else 'human_review',
            'expected_pairs': len(pairs),
            'expected_episodes': repeats * (len(tasks) + 2 * len(pairs)), 'pairs': pairs}


def model_roles():
    from .api import config
    cfg = config()
    return {role: cfg.get(role) or cfg['AGENT_MODEL'] for role in
            ('AGENT_MODEL', 'SIMULATOR_MODEL', 'JUDGE_MODEL')}


def review_is_current(candidate):
    base = pairing.folder(candidate)
    path = base / 'review.json'
    if not path.exists():
        return False
    review = specs._load(path)
    if review.get('decision') == 'reject':
        raise ValueError(f'Candidate explicitly rejected: {candidate}')
    return (review.get('decision') == 'approve' and review.get('inputs') == pairing.inputs(candidate)
            and review.get('check_sha256') == pairing.digest(base / 'check.json'))


def refresh_preparation_source(record_path, saved, identity):
    """Explicit, audited source upgrade BEFORE candidate checking or experiments."""
    old = {key: value for key, value in saved.items() if key not in ('existing_bindings', 'source_fingerprint')}
    new = {key: value for key, value in identity.items() if key != 'source_fingerprint'}
    if old != new:
        raise ValueError('Preparation refresh cannot change models, selection, prefix or review policy')
    before, after = saved['source_fingerprint'], identity['source_fingerprint']
    changed = {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
    if any(not key.startswith('dspilot/') or not key.endswith('.py') for key in changed):
        raise ValueError('Preparation refresh only allows harness Python source changes, not task/persona data')
    prefix = saved['prefix']
    if ((specs.ROOT / 'runs/matrices' / f'{prefix}.json').exists() or
            (specs.ROOT / 'runs/matrices' / f'{prefix}-report').exists() or
            any((specs.ROOT / 'runs').glob(f'{prefix}-r*'))):
        raise ValueError('Cannot refresh source after matrix work has started; use a new prefix')
    bindings = specs.binding_paths()
    for item in saved['pairs']:
        candidate = item['candidate']
        base = pairing.folder(candidate)
        path = bindings.get((item['task_id'], item['persona_id']))
        expected_hash = saved['existing_bindings'].get(candidate)
        if (path is not None and (expected_hash is None or pairing.digest(path) != expected_hash)) or (expected_hash is not None and path is None):
            raise ValueError('Published binding changed; cannot refresh preparation source')
        if any((specs.ROOT / directory / candidate).exists() for directory in ('runs/pair-checks', 'runs/pair-publications')):
            raise ValueError('Candidate calibration/publication already started; use a new prefix')
        if base.exists():
            _, meta = pairing.candidate_data(candidate)
            if (meta['task_id'], meta['persona_id'], meta.get('origin')) != (item['task_id'], item['persona_id'], 'model'):
                raise ValueError('Candidate identity/origin mismatch during preparation refresh')
            if (all((base / name).exists() for name in ('binding.json', 'applicability.json')) or
                    any((base / name).exists() for name in ('check.json', 'review.json', 'publication.json'))):
                raise ValueError('Only incomplete, unchecked candidates support preparation source refresh')
    parent = specs.ROOT / 'runs/pipeline-source-upgrades' / prefix
    parent.mkdir(parents=True, exist_ok=True)
    audit = Path(tempfile.mkdtemp(prefix='attempt-', dir=parent))
    updated = identity | {'existing_bindings': saved['existing_bindings']}
    pairing.dump(audit / 'before.json', saved)
    pairing.dump(audit / 'after.json', updated)
    pairing.dump(audit / 'upgrade.json', {'utc': pairing.now(), 'changed_files': sorted(changed),
                                        'reason': 'Explicit --refresh-preparation-source; no calibration or matrix has started'})
    pairing.dump(record_path, updated)
    print(json.dumps({'preparation_source_refreshed': prefix, 'audit': str(audit)}), flush=True)
    return updated


def execute(pipeline_plan, *, resume=False, refresh_source=False):
    if refresh_source and not resume:
        raise ValueError('--refresh-preparation-source requires --resume')
    prefix = pipeline_plan['prefix']
    identity = pipeline_plan | {'source_fingerprint': source_fingerprint(pipeline_plan['tasks']),
                                'model_roles': model_roles()}
    record_path = specs.ROOT / 'runs/pipelines' / f'{prefix}.json'
    bindings = specs.binding_paths()
    if record_path.exists():
        if not resume:
            raise ValueError('Pipeline prefix exists; use --resume or a new prefix')
        saved = specs._load(record_path)
        if {key: value for key, value in saved.items() if key != 'existing_bindings'} != identity:
            if refresh_source:
                saved = refresh_preparation_source(record_path, saved, identity)
            else:
                raise ValueError('Source/model/selection/review policy changed; cannot resume this prefix. '
                                 'For harness fixes before any candidate check or matrix work, explicitly use '
                                 '--refresh-preparation-source; otherwise start a new prefix.')
    elif resume:
        raise ValueError('Cannot resume: no saved pipeline plan')
    else:
        if any((specs.ROOT / directory / f'{prefix}.json').exists()
               for directory in ('runs/matrices', 'runs/pilots')):
            raise ValueError('Experiment prefix already used; choose a new prefix')
        existing = {}
        runnable = {(cell['task_id'], cell['persona_id']) for cell in specs.pairing_plan()
                    if cell['status'] == 'runnable'}
        for item in pipeline_plan['pairs']:
            key = (item['task_id'], item['persona_id'])
            if pairing.folder(item['candidate']).exists():
                raise ValueError(f"Candidate already exists: {item['candidate']}")
            if key in bindings:
                if key not in runnable:
                    raise ValueError(f'Existing binding is not runnable: {key}')
                specs.compile_episode(*key)
                existing[item['candidate']] = pairing.digest(bindings[key])
        saved = identity | {'existing_bindings': existing}
        pairing.dump(record_path, saved)

    # Check reused bindings before spending on downloads or model calls.
    for item in pipeline_plan['pairs']:
        expected_hash = saved['existing_bindings'].get(item['candidate'])
        if expected_hash is not None:
            path = bindings.get((item['task_id'], item['persona_id']))
            if path is None or pairing.digest(path) != expected_hash:
                raise ValueError('Previously registered binding changed; refusing reuse')

    for task in pipeline_plan['tasks']:
        prepare_task(task)
    pending = []
    skip = pipeline_plan['review_mode'] == 'skip_human_review'
    for item in pipeline_plan['pairs']:
        candidate = item['candidate']
        if candidate in saved['existing_bindings'] or published_by_candidate(item):
            continue
        base = pairing.folder(candidate)
        if not base.exists():
            launch('pair', 'propose', '--task-id', item['task_id'], '--persona-id', item['persona_id'],
                   '--candidate', candidate, '--generate')
        else:
            _, meta = pairing.candidate_data(candidate)
            if (meta['task_id'], meta['persona_id']) != (item['task_id'], item['persona_id']):
                raise ValueError('Existing candidate identity mismatch')
            if not all((base / name).is_file() for name in ('binding.json', 'applicability.json')):
                launch('pair', 'propose', '--task-id', item['task_id'], '--persona-id', item['persona_id'],
                       '--candidate', candidate, '--generate', '--retry-generation')
        review_path = base / 'review.json'
        if review_path.exists() and specs._load(review_path).get('decision') == 'reject':
            raise ValueError(f'Candidate explicitly rejected: {candidate}')
        try:
            pairing.require_checked(candidate)
            needs_check = False
        except (ValueError, OSError, KeyError):
            needs_check = True
        if needs_check:
            launch('pair', 'check', '--candidate', candidate, '--calibrate')
        if not skip and not review_is_current(candidate):
            pending.append(candidate)
            continue
        launch('pair', 'publish', '--candidate', candidate, *(['--skip-human-review'] if skip else []))
    if pending:
        print(json.dumps({'status': 'awaiting_human_review', 'candidates': pending,
                          'next': 'Review each candidate with launch pair review, then rerun the same pipeline command with --resume.'},
                         indent=2), flush=True)
        return 'awaiting_human_review'

    from .matrix import make_plan
    final = make_plan(prefix, pipeline_plan['repeats'], pipeline_plan['tasks'], pipeline_plan['personas'])
    expected = {(item['task_id'], item['persona_id']) for item in pipeline_plan['pairs']}
    actual = {(item['task_id'], item['persona_id']) for item in final['pairs']}
    if actual != expected or final['episode_count'] != pipeline_plan['expected_episodes']:
        raise ValueError('Incomplete cross product; refusing to run a reduced matrix')
    if source_fingerprint(pipeline_plan['tasks']) != saved['source_fingerprint'] or model_roles() != saved['model_roles']:
        raise ValueError('Source or models changed during processing')
    args = ['matrix', '--prefix', prefix, '--repeats', str(pipeline_plan['repeats']), '--execute', '--calibrate']
    for task in pipeline_plan['tasks']:
        args += ['--task-id', task]
    for persona in pipeline_plan['personas']:
        args += ['--persona-id', persona]
    if (specs.ROOT / 'runs/matrices' / f'{prefix}.json').exists():
        args.append('--resume')
    launch(*args)
    return 'complete'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, help='JSON file with prefix, tasks, personas, repeats and skip_human_review.')
    parser.add_argument('--prefix')
    parser.add_argument('--task-id', action='append', dest='tasks')
    parser.add_argument('--persona-id', action='append', dest='personas')
    parser.add_argument('--repeats', type=int)
    parser.add_argument('--skip-human-review', action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument('--execute', action='store_true', help='Download images, make paid model calls and publish bindings.')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--refresh-preparation-source', action='store_true',
                        help='Audit and accept harness-only source fixes on resume, before any candidate checks or matrix work.')
    args = parser.parse_args()
    try:
        settings = {}
        if args.config:
            if args.tasks is not None or args.personas is not None:
                raise ValueError('Use either config task/persona lists or CLI lists, not both')
            settings = json.loads(args.config.read_text())
            if not isinstance(settings, dict):
                raise ValueError('Config must be a JSON object')
            unknown = set(settings) - {'prefix', 'tasks', 'personas', 'repeats', 'skip_human_review'}
            if unknown:
                raise ValueError(f'Unknown config keys: {sorted(unknown)}')
        for key in ('prefix', 'tasks', 'personas', 'repeats', 'skip_human_review'):
            if getattr(args, key) is not None:
                settings[key] = getattr(args, key)
        if args.resume and not args.execute:
            raise ValueError('--resume requires --execute')
        if args.refresh_preparation_source and not args.resume:
            raise ValueError('--refresh-preparation-source requires --execute --resume')
        pipeline_plan = plan(settings.get('prefix'), settings.get('tasks'), settings.get('personas'),
                             settings.get('repeats', 1), settings.get('skip_human_review', False))
    except (ValueError, OSError, KeyError) as exc:
        parser.error(str(exc))
    print(json.dumps(pipeline_plan, indent=2), flush=True)
    if args.execute:
        execute(pipeline_plan, resume=args.resume, refresh_source=args.refresh_preparation_source)


if __name__ == '__main__':
    main()
