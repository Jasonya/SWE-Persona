"""Prospective delivery provenance. No historical receipts are inferred.

Task blur preparation runs in a fresh, task-only process before persona binding.
The full first user message is frozen immediately before delivery to the agent.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib

from . import delivery_runtime as rt

VERSION = 'deepswe-prospective-delivery-v1'
ROOT = Path(__file__).resolve().parents[1]


def stamp(): return datetime.now(timezone.utc).isoformat()


def text_sha(text): return hashlib.sha256(text.encode()).hexdigest()


def task_only_blur(root, task_id, output):
    # Deliberately do not import specs/task, read plans/bindings, or read personas.
    profile_path = rt.safe(root, 'data/tasks/' + task_id + '.json')
    profile = rt.read(profile_path)
    original = rt.safe(root, '.cache/tasks/' + profile.get('cache_dir', task_id))
    config = tomllib.loads((original / 'task.toml').read_text())
    title = config.get('metadata', {}).get('display_title') or config.get('metadata', {}).get('original_title') or task_id
    if not isinstance(title, str) or not title.strip(): raise ValueError('Task has no public title')
    # Fixed task-only title projection; no invented constraints or simulated review.
    opening = 'Please help with this repository task: ' + title.strip() + '.'
    # A conservative task-only schedule: expose the whole original task at S1.
    # Do not reverse-engineer a pre-persona schedule from a joint binding.
    instruction = (original / 'instruction.md').read_bytes().decode('utf-8')
    value = {'schema_version': VERSION, 'task_id': task_id, 'opening': opening,
             'stage_summary': {'status': 'frozen_before_persona_binding',
                 'method': 'task_only_full_disclosure_v1',
                 'scope': 'Task-only preparation schedule; the bound runtime schedule is recorded separately.',
                 'value': {'task_disclosure_mode': 'full_at_start',
                     'stages': [{'id': 'S1', 'task_units': ['T1']}],
                     'task_units': [{'id': 'T1', 'stage': 'S1', 'text': instruction}]}},
             'method': 'deterministic_public_title_projection_v1',
             'original_instruction_sha256': rt.sha(original / 'instruction.md'),
             'scope': 'Task-only vague request. Full original task obligations remain in the staged plan; this is not a semantic quality approval.'}
    output.mkdir(parents=True, exist_ok=False)
    rt.dump(output / 'task-blur.json', value)
    rt.dump(output / 'receipt.json', {'schema_version': VERSION, 'task_id': task_id,
        'created_at_utc': stamp(), 'task_only_process': True, 'persona_inputs': [],
        'persona_loaded_in_generator': False, 'model_calls': 0,
        'generator_sha256': rt.sha(Path(__file__)),
        'inputs': {str(p.relative_to(root)): rt.sha(p) for p in (profile_path, original / 'instruction.md', original / 'task.toml')},
        'output_sha256': rt.sha(output / 'task-blur.json')})


def prepare_blur(root, task_id):
    with tempfile.TemporaryDirectory(prefix='deepswe-task-only-blur-') as tmp:
        env = os.environ.copy()
        for key in list(env):
            if key.startswith('DS_'): env.pop(key)
        env.update(PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE='1')
        dest = Path(tmp) / 'blur'
        subprocess.run([sys.executable, '-B', '-m', 'dspilot.delivery_capture',
            '--task-id', task_id, '--output', str(dest)], cwd=root, env=env, check=True, capture_output=True)
        return {'blur': rt.read(dest / 'task-blur.json'), 'receipt': rt.read(dest / 'receipt.json')}


def verify_blur(bundle, task_id):
    blob = (json.dumps(bundle['blur'], ensure_ascii=False, indent=2) + '\n').encode()
    receipt = bundle['receipt']
    if (bundle['blur']['task_id'] != task_id or receipt['task_id'] != task_id
            or receipt['output_sha256'] != hashlib.sha256(blob).hexdigest()
            or receipt.get('task_only_process') is not True
            or receipt['persona_inputs'] != [] or receipt['persona_loaded_in_generator'] is not False):
        raise ValueError('Task-only blur provenance mismatch')
    summary = bundle['blur'].get('stage_summary')
    if summary is not None:
        value = summary.get('value') or {}
        units = value.get('task_units', [])
        if (summary.get('status') != 'frozen_before_persona_binding'
                or summary.get('method') != 'task_only_full_disclosure_v1'
                or value.get('task_disclosure_mode') != 'full_at_start'
                or value.get('stages') != [{'id': 'S1', 'task_units': ['T1']}]
                or len(units) != 1 or units[0].get('id') != 'T1' or units[0].get('stage') != 'S1'
                or not isinstance(units[0].get('text'), str)
                or text_sha(units[0]['text']) != bundle['blur']['original_instruction_sha256']):
            raise ValueError('Task-only stage summary differs from original instruction')


def from_plan(plan_path, task_id):
    if not plan_path: return None
    path = Path(plan_path)
    if not path.is_absolute(): path = ROOT / path
    source = path.parent / 'delivery-blur.json'
    if not source.exists(): return None
    value = rt.read(source); verify_blur(value, task_id)
    return value


def freeze_opening(out, message, bundle, *, task_id, persona_id, arm, plan, initial, models, reused_from=None):
    verify_blur(bundle, task_id)
    if not isinstance(message, str) or not message.strip(): raise ValueError('Empty complete opening')
    dest = out / 'delivery'; dest.mkdir(exist_ok=False)
    (dest / 'instruction.md').write_text(message)
    rt.dump(dest / 'task-blur.json', bundle['blur'])
    rt.dump(dest / 'blur-receipt.json', bundle['receipt'])
    receipt = {'schema_version': VERSION, 'task_id': task_id, 'persona_id': persona_id, 'arm': arm,
        'frozen_at_utc': stamp(), 'freeze_phase': 'before_first_user_delivery_and_agent_action',
        'agent_actions_before_freeze': 0, 'complete_first_user_message': True,
        'instruction_sha256': rt.sha(dest / 'instruction.md'),
        'blur_sha256': rt.sha(dest / 'task-blur.json'), 'blur_receipt_sha256': rt.sha(dest / 'blur-receipt.json'),
        'plan_sha256': text_sha(json.dumps(plan, sort_keys=True)),
        'initial_snapshot_sha256': text_sha(json.dumps(initial, sort_keys=True)),
        'models': models, 'historical_backfill': False}
    if reused_from is not None:
        if reused_from['instruction_sha256'] != receipt['instruction_sha256']:
            raise ValueError('Reused opening differs from the source receipt')
        receipt['reused_from'] = reused_from
    rt.dump(dest / 'opening-receipt.json', receipt)
    return receipt


OPENING_FILES = ('instruction.md', 'task-blur.json', 'blur-receipt.json', 'opening-receipt.json')


def opening_for_run(plan_path, *, task_id, persona_id, arm, plan, generate):
    """Bypass the simulator for a frozen Pivot opening; never silently fall back."""
    path = Path(plan_path) if plan_path else None
    if path is not None and not path.is_absolute(): path = ROOT / path
    source = path.parent / 'delivery-opening' if path is not None else None
    if arm != 'pivot' or source is None or not source.exists():
        return generate(), None
    receipt = verify_opening(source, task_id=task_id, persona_id=persona_id, arm=arm, plan=plan)
    return (source / 'instruction.md').read_text(), {
        'opening_receipt_sha256': rt.sha(source / 'opening-receipt.json'),
        'instruction_sha256': receipt['instruction_sha256'],
        'frozen_at_utc': receipt['frozen_at_utc'],
        'source': 'frozen_plan/delivery-opening', 'simulator_regenerated': False}


def verify_opening(directory, *, task_id, persona_id, arm, plan=None, delivered_message=None):
    receipt = rt.read(directory / 'opening-receipt.json')
    if any(receipt.get(k) != v for k, v in {'task_id': task_id, 'persona_id': persona_id, 'arm': arm,
            'freeze_phase': 'before_first_user_delivery_and_agent_action',
            'complete_first_user_message': True, 'agent_actions_before_freeze': 0, 'historical_backfill': False}.items()):
        raise ValueError('Opening receipt identity/freeze mismatch')
    for key, name in [('instruction_sha256', 'instruction.md'), ('blur_sha256', 'task-blur.json'), ('blur_receipt_sha256', 'blur-receipt.json')]:
        if receipt[key] != rt.sha(directory / name): raise ValueError('Opening artifact hash mismatch')
    verify_blur({'blur': rt.read(directory / 'task-blur.json'), 'receipt': rt.read(directory / 'blur-receipt.json')}, task_id)
    if plan is not None and receipt['plan_sha256'] != text_sha(json.dumps(plan, sort_keys=True)):
        raise ValueError('Opening belongs to a different plan')
    if delivered_message is not None and text_sha(delivered_message) != receipt['instruction_sha256']:
        raise ValueError('Delivered opening differs from frozen opening')
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task-id', required=True); parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    task_only_blur(ROOT, args.task_id, args.output)
