"""Per-pair time limits: derive a new input release without editing a sealed one."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import tempfile
import tomllib

from . import delivery_runtime as rt
from .coding_budget import coding_seconds, SCOPE

DEFAULT_VERIFIER_SECONDS = rt.DEFAULT_VERIFIER_SECONDS
verifier_seconds = rt.verifier_seconds


def positive_seconds(value):
    if type(value) is not int or value <= 0:
        raise ValueError('Time limits must be positive integer seconds')
    return value


def update_plan(plan, *, pier_agent_seconds=None, coding_seconds=None, interaction_seconds=None, final_review_seconds=None, verifier_seconds=None):
    result = copy.deepcopy(plan)
    for key, value in (('coding_seconds', coding_seconds), ('interaction_seconds', interaction_seconds), ('final_review_seconds', final_review_seconds)):
        if value is not None:
            result['budgets'][key] = positive_seconds(value)
    if pier_agent_seconds is not None:
        result['pier_agent_timeout_sec'] = positive_seconds(pier_agent_seconds)
    if verifier_seconds is not None:
        result['pier_verifier_timeout_sec'] = positive_seconds(verifier_seconds)
    return result


def settings(folder, plan):
    original = tomllib.loads((folder / 'tests/persona/original/task.toml').read_text())
    return {'schema_version': 'deepswe-time-limits-v3',
            'coding_seconds': coding_seconds(plan),
            'coding_timeout_scope': SCOPE,
            'original_agent_timeout_sec': original['agent']['timeout_sec'],
            'pier_agent_timeout_sec': plan.get('pier_agent_timeout_sec', original['agent']['timeout_sec']),
            'pier_agent_timeout_overridden': 'pier_agent_timeout_sec' in plan,
            'pier_agent_timeout_scope': 'per coding segment, not whole episode',
            'interaction_seconds': plan['budgets']['interaction_seconds'],
            'final_review_seconds': plan['budgets']['final_review_seconds'],
            'original_verifier_timeout_sec': original['verifier']['timeout_sec'],
            'verifier_timeout_sec': verifier_seconds(plan, original['verifier']['timeout_sec']),
            'verifier_max_attempts': 1,
            'verifier_timeout_scope': 'entire verification attempt including setup; cancellation/cleanup may take additional time; no timeout retry',
            'configure_entrypoint': 'tests/persona/configure_runtime.py',
            'mutation_policy': 'new input release; original task config and prior runs remain unchanged'}


def configure(dataset, pair_id, output, **changes):
    from .export_deepswe_release import package
    dataset, output = Path(dataset).resolve(), Path(output).absolute()
    rt.verify_dataset(dataset)
    chosen = rt.pairs(dataset, [pair_id])[0]
    rt.work_root(dataset, output)
    if output.exists():
        raise ValueError('Use a new output directory')
    if not any(value is not None for value in changes.values()):
        raise ValueError('Specify at least one time limit')
    # Build from a temporary source copy: no sealed files or historical evidence change.
    with tempfile.TemporaryDirectory(prefix='deepswe-time-limits-') as tmp:
        project = Path(tmp) / 'project'
        shutil.copytree(dataset / 'runtime/project', project)
        rows = rt.pairs(dataset)
        for row in rows:
            plan = rt.read(rt.task_path(dataset, row) / 'tests/persona/legacy-plan.json')
            if row['delivery_id'] == chosen['delivery_id']:
                plan = update_plan(plan, **changes)
            rt.dump(project / 'data/staged_bindings' / f"{row['task_id']}--{row['persona_id']}.json", plan)
        selection = project / 'selection-timeouts.json'
        rt.dump(selection, {'pairs': [{'task_id': r['task_id'], 'persona_id': r['persona_id']} for r in rows]})
        return package(project, None, None, output, selection=selection)


def main(dataset, pair_id=None):
    parser = argparse.ArgumentParser(description=__doc__)
    if pair_id is None:
        parser.add_argument('--pair', required=True)
    parser.add_argument('--show', action='store_true', help='Read current time limits without model or Docker calls')
    parser.add_argument('--pier-agent-seconds', type=int)
    parser.add_argument('--coding-seconds', type=int, help='Cumulative active coding seconds; default 3600; excludes simulator/judges/final grading')
    parser.add_argument('--interaction-seconds', type=int)
    parser.add_argument('--final-review-seconds', type=int)
    parser.add_argument('--verifier-seconds', type=int, help='Total Pier-native verification budget; one attempt, no timeout retry')
    parser.add_argument('--output', type=Path, help='Fresh release directory outside this dataset')
    args = parser.parse_args()
    pair_id = pair_id or args.pair
    if args.show:
        rt.verify_dataset(dataset)
        pair = rt.pairs(dataset, [pair_id])[0]
        folder = rt.task_path(dataset, pair)
        value = settings(folder, rt.read(folder / 'tests/persona/legacy-plan.json'))
    else:
        if not args.output:
            parser.error('--output is required to generate a new release')
        value = configure(dataset, pair_id, args.output, pier_agent_seconds=args.pier_agent_seconds, coding_seconds=args.coding_seconds,
                          interaction_seconds=args.interaction_seconds, final_review_seconds=args.final_review_seconds,
                          verifier_seconds=args.verifier_seconds)
    print(json.dumps(value, ensure_ascii=False, indent=2))
