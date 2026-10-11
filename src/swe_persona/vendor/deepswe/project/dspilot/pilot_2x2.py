"""Opt-in end-to-end pilot: two new tasks, two personas, no human review.

Default mode only previews. --execute prepares tasks/images, generates/checks/
publishes all four candidates, then runs exactly ten scored episodes. A failed
candidate stops the pipeline; it never shrinks the requested matrix silently.
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys

from . import pairing, specs

TASKS = ('httpx-streaming-json-iteration', 'cattrs-partial-structuring-recovery')
PERSONAS = ('first_time_pr_contributor', 'student_needing_explanation')


def plan(prefix):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,69}', prefix):
        raise ValueError('Prefix must contain 1-70 letters, numbers, hyphens or underscores')
    for task in TASKS:
        specs.load_task(task)
    if not set(PERSONAS) <= set(specs.persona_catalog()):
        raise ValueError('Pilot personas are missing from the catalog')
    return {'version': 'new-task-2x2-v1', 'prefix': prefix,
            'tasks': list(TASKS), 'personas': list(PERSONAS),
            'expected_pairs': 4, 'expected_episodes': 10, 'repeats': 1,
            'review_mode': 'skip_human_review',
            'pairs': [{'task_id': task, 'persona_id': persona,
                       'candidate': f'{prefix}--{task}--{persona}'}
                      for task in TASKS for persona in PERSONAS]}


def source_fingerprint(task_ids=TASKS):
    paths = list((specs.ROOT / 'dspilot').glob('*.py'))
    paths += [specs.ROOT / 'data/personas.v3.json', specs.ROOT / 'sources.lock.json']
    for task in task_ids:
        task_spec = specs.load_task(task)
        paths += [task_spec.path, task_spec.contract_path]
    return {str(path.relative_to(specs.ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(set(paths))}


def launch(*args):
    command = [sys.executable, '-m', 'dspilot.launch', *args]
    print(json.dumps({'launch': command}), flush=True)
    subprocess.run(command, cwd=specs.ROOT, check=True)


def prepare_task(task_id):
    from .prepare import verify_cache
    try:
        verify_cache(task_id)
        print(json.dumps({'task_cache': task_id, 'status': 'verified'}), flush=True)
    except (ValueError, OSError):
        command = [sys.executable, '-m', 'dspilot.prepare', '--task-id', task_id]
        local = specs.ROOT / '.cache/deepswe-source'
        if local.is_dir():
            command += ['--from-local', str(local)]
        subprocess.run(command, cwd=specs.ROOT, check=True)
    image = specs.load_task(task_id).image
    available = subprocess.run(['docker', 'image', 'inspect', image],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if available.returncode:
        print(json.dumps({'pull_image': image}), flush=True)
        subprocess.run(['docker', 'pull', '--platform', 'linux/amd64', image], check=True)


def published_by_candidate(item):
    path = specs.binding_paths().get((item['task_id'], item['persona_id']))
    if path is None:
        return False
    base = pairing.folder(item['candidate'])
    receipt = base / 'publication.json'
    if not receipt.is_file():
        raise ValueError(f'Pair already exists without this pilot publication: {path}. Use launch matrix to rerun it.')
    saved = specs._load(receipt)
    if (saved.get('candidate_id') != item['candidate'] or
            saved.get('binding_path') != str(path.relative_to(specs.ROOT)) or
            saved.get('binding_sha256') != pairing.digest(path) or
            saved.get('binding_sha256') != pairing.digest(base / 'binding.json')):
        raise ValueError('Published pair differs from the pilot candidate; refusing reuse')
    return True


def execute(pilot_plan, *, resume=False):
    from .api import config
    cfg = config()  # Fail before any generation if credentials are unavailable.
    model_roles = {role: cfg.get(role) or cfg['AGENT_MODEL'] for role in
                   ('AGENT_MODEL', 'SIMULATOR_MODEL', 'JUDGE_MODEL')}
    prefix = pilot_plan['prefix']
    record = pilot_plan | {'source_fingerprint': source_fingerprint(), 'model_roles': model_roles}
    path = specs.ROOT / 'runs/pilots' / f'{prefix}.json'
    if path.exists():
        if not resume:
            raise ValueError('Pilot prefix already exists; use --resume or choose a new prefix')
        if specs._load(path) != record:
            raise ValueError('Pilot source/model/selection changed; do not mix experiments under one prefix')
    elif resume:
        raise ValueError('Cannot resume: no saved pilot plan')
    else:
        # Avoid spending on preparation/generation before detecting name collisions.
        for item in pilot_plan['pairs']:
            if pairing.folder(item['candidate']).exists() or (item['task_id'], item['persona_id']) in specs.binding_paths():
                raise ValueError('Candidate or pair already exists. Use --resume for this pilot, or launch matrix for existing pairs.')
        if (specs.ROOT / 'runs/matrices' / f'{prefix}.json').exists():
            raise ValueError('Matrix prefix already exists; choose a new pilot prefix')
        pairing.dump(path, record)

    for task in TASKS:
        prepare_task(task)
    for item in pilot_plan['pairs']:
        if published_by_candidate(item):
            print(json.dumps({'candidate': item['candidate'], 'status': 'already_published'}), flush=True)
            continue
        base = pairing.folder(item['candidate'])
        if not base.exists():
            launch('pair', 'propose', '--task-id', item['task_id'], '--persona-id', item['persona_id'],
                   '--candidate', item['candidate'], '--generate')
        else:
            _, meta = pairing.candidate_data(item['candidate'])
            if (meta['task_id'], meta['persona_id']) != (item['task_id'], item['persona_id']):
                raise ValueError('Existing candidate identity mismatch')
            if not (base / 'binding.json').is_file() or not (base / 'applicability.json').is_file():
                raise ValueError(f'Incomplete proposal in {base}; preserve the evidence and repair the draft before resuming')
        launch('pair', 'check', '--candidate', item['candidate'], '--calibrate')
        launch('pair', 'publish', '--candidate', item['candidate'], '--skip-human-review')

    from .matrix import make_plan
    final_plan = make_plan(prefix, 1, list(TASKS), list(PERSONAS))
    expected = {(task, persona) for task in TASKS for persona in PERSONAS}
    actual = {(item['task_id'], item['persona_id']) for item in final_plan['pairs']}
    if actual != expected or final_plan['episode_count'] != 10:
        raise ValueError('Expected all four pairs and ten episodes; refusing a reduced matrix')
    if source_fingerprint() != record['source_fingerprint']:
        raise ValueError('Source changed during candidate processing; refusing to start episodes')
    current_cfg = config()
    current_roles = {role: current_cfg.get(role) or current_cfg['AGENT_MODEL'] for role in model_roles}
    if current_roles != model_roles:
        raise ValueError('Model configuration changed during candidate processing')
    args = ['matrix', '--prefix', prefix, '--repeats', '1', '--execute', '--calibrate']
    for task in TASKS:
        args += ['--task-id', task]
    for persona in PERSONAS:
        args += ['--persona-id', persona]
    if (specs.ROOT / 'runs/matrices' / f'{prefix}.json').exists():
        args.append('--resume')
    launch(*args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', required=True)
    parser.add_argument('--execute', action='store_true', help='Opt in to image downloads and paid model calls.')
    parser.add_argument('--resume', action='store_true', help='Continue the same pilot after a failure; never overwrite episodes.')
    args = parser.parse_args()
    if args.resume and not args.execute:
        parser.error('--resume requires --execute')
    pilot_plan = plan(args.prefix)
    print(json.dumps(pilot_plan, indent=2), flush=True)
    if args.execute:
        execute(pilot_plan, resume=args.resume)


if __name__ == '__main__':
    main()
