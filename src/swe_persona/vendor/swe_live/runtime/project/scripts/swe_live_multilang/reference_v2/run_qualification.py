"""Qualify one real episode per original persona before admitting a final study.

The ten public adapter seeds are fixed independently of agent outcomes. Valid
functional failures are retained. This collection is a protocol qualification,
not the final dataset and not the population used to calculate P20.
"""
import bootstrap
import argparse
import json
import os
from pathlib import Path
import shutil
import sys

from audit_sample import audit
from calibration import calibrate, calibrate_bound, gate_location
from execution_policy import BudgetClient, Deadline
from llm import CallLog, Llm
from personas import PERSONAS
from run_reference import gold_proof
from run_study import ensure_gold
from support import ROOT, REVISION, Manifest, digest, file_hash, read, save, source_hashes
from task_binding import load_binding, candidate_for
from whole_sample import existing_result, supervise_sample

SEEDS = {
    'first_time_pr_contributor': ('rollup__rollup-6051', None),
    'terse_lead_with_flaky_test': ('fluent__fluent-bit-10563', 'fluent_loki_v1'),
    'laptop_data_scientist': ('BYVoid__OpenCC-1289', 'opencc_resource_v1'),
    'student_needing_explanation': ('BYVoid__OpenCC-1289', 'opencc_student_v1'),
    'manager_no_code': ('docsifyjs__docsify-2595', None),
    'analyst_without_index_permission': ('duckdb__duckdb-24074', 'duckdb_query_v1'),
    'designer_after_hard_reset': ('docsifyjs__docsify-2595', 'docsify_recovery_v1'),
    'founder_leaked_production_key': ('NVIDIA__NemoClaw-330', 'nemoclaw_credential_v1'),
    'shop_owner_cannot_install': ('docsifyjs__docsify-2595', 'docsify_shop_v1'),
    'senior_race_review_no_tutorial': ('open-telemetry__opentelemetry-collector-contrib-40522', 'otel_review_v1'),
}
HERE = Path(__file__).parent


def freeze(study, source, binding_paths, model):
    study.mkdir(parents=True, exist_ok=True)
    path = study / 'qualification-plan.json'
    if path.exists():
        plan = read(path)
        if (plan['sources'] != source_hashes() or plan['model'] != model
                or plan.get('driver_sha256') != file_hash(__file__)):
            raise ValueError('Qualification source/model changed; preserve this collection and use a new directory')
        for task in plan['tasks']:
            if file_hash(study / task['path']) != task['sha256']:
                raise ValueError('Qualification task changed')
        for role in plan['roles']:
            if role.get('binding_path'):
                if file_hash(study / role['binding_path']) != role['binding_sha256']:
                    raise ValueError('Qualification binding changed')
                task = next(t for t in plan['tasks'] if t['instance_id'] == role['instance_id'])
                load_binding(study / role['binding_path'], Manifest(study / task['path']), role['persona'])
        return plan
    original = read(source / 'manifest.json')
    if original['dataset_revision'] != REVISION:
        raise ValueError('Wrong dataset revision for qualification')
    needed = {task for task, _ in SEEDS.values()}
    tasks = [t for t in original['tasks'] if t['instance_id'] in needed]
    if len(tasks) != len(needed):
        raise ValueError('Qualification seeds absent from the pinned inventory')
    for task in tasks:
        target = study / task['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / task['path'], target)
        if file_hash(target) != task['sha256']:
            raise ValueError('Pinned task changed while copying')
    roles = []
    for persona, (instance, kind) in SEEDS.items():
        task = next(t for t in tasks if t['instance_id'] == instance)
        manifest = Manifest(study / task['path'])
        role = {'persona': persona, 'instance_id': instance, 'task_key': task['key'], 'fixture_kind': kind,
                'public_fit': {'quote': manifest.instruction.splitlines()[0],
                               'selection': 'Preregistered public fixture seed, before agent execution'}}
        if kind:
            source_binding = Path(binding_paths[persona]).resolve()
            bound = load_binding(source_binding, manifest, persona)
            if bound['fixture_kind'] != kind:
                raise ValueError('Wrong registered seed fixture')
            target = study / 'bindings' / persona
            target.mkdir(parents=True, exist_ok=False)
            shutil.copyfile(source_binding, target / 'binding.json')
            for artifact in bound['evidence']:
                destination = target / artifact['path']
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_binding.parent / artifact['path'], destination)
            role.update(binding_path=(target / 'binding.json').relative_to(study).as_posix(),
                        binding_sha256=file_hash(target / 'binding.json'))
        roles.append(role)
    plan = {'purpose': 'Ten-persona adapter qualification; excluded from final task scores and P20 calibration',
            'dataset_revision': REVISION, 'model': model, 'sources': source_hashes(), 'tasks': tasks, 'roles': roles,
            'driver_sha256': file_hash(__file__),
            'sample_safety_seconds': 7200, 'max_turns': 8, 'max_steps': 14,
            'retry_policy': 'Never retry a valid functional failure; invalid attempts need explicit --retry-invalid'}
    save(path, plan)
    return plan


def ensure_gate(study, role, model):
    bound = read(study / role['binding_path']) if role.get('binding_path') else None
    location, cached = gate_location(ROOT, model, role['persona'], bound)
    if cached:
        return read(location / 'report.json')
    log = CallLog(path=study / ('calibration-' + role['persona'] + '.jsonl'))
    if bound and candidate_for(bound)[1].template['satisfaction']['judge'] == 'trusted_harness':
        from types import SimpleNamespace
        client = SimpleNamespace(model=model)
    else:
        client = BudgetClient(Llm(model=model, call_log=log), 90)
        client.deadline = Deadline(1800, 'qualification_checker_gate')
    result = (calibrate_bound(client, location, study / role['binding_path']) if bound else
              calibrate(client, location, role['persona']))
    save(study / ('gate-' + role['persona'] + '.json'), result)
    if not result['gate_passed']:
        raise RuntimeError('Checker controls failed for ' + role['persona'] + '; see ' + str(location))
    return result


def collect_report(study, plan):
    rows = []
    for role in plan['roles']:
        attempts = sorted((study / 'qualification' / role['persona']).glob('attempt-*'))
        row = dict(role, validated=False, attempts=[])
        for path in attempts:
            if not (path / 'result.json').exists():
                row['attempts'].append({'path': str(path), 'status': 'unfinished'})
                continue
            result = read(path / 'result.json')
            row['attempts'].append({'path': path.relative_to(study).as_posix(), **result})
            worker = path / 'worker'
            if result['status'] == 'completed' and audit(worker)['ready_for_review']:
                protocol = read(worker / 'protocol.json')
                if protocol['sources'] != plan['sources']:
                    continue
                row['validated'] = True
                receipt = {'persona': role['persona'], 'instance_id': role['instance_id'], 'sources': protocol['sources'],
                           'sample_path': worker.relative_to(study).as_posix(), 'episode_sha256': file_hash(worker / 'episode.json'),
                           'fixture_controls_passed': read(worker / 'checker_gate.json')['gate_passed'],
                           'base_task_success': result['base_task_success'], 'joint_success': result['joint_success'],
                           'qualification_only': True, 'selection_was_not_based_on_agent_success': True}
                save(path.parent / 'qualification.json', receipt)
        rows.append(row)
    report = {'validated_count': sum(r['validated'] for r in rows), 'required_count': 10, 'roles': rows,
              'publication_ready': False, 'qualification_complete': all(r['validated'] for r in rows)}
    save(study / 'qualification-report.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-inventory', type=Path, required=True)
    parser.add_argument('--bindings', type=Path, required=True, help='JSON persona-to-existing-binding mapping')
    parser.add_argument('--gold-sources', type=Path, help='JSON instance-to-existing-gold-directory mapping')
    parser.add_argument('--study-dir', type=Path, required=True)
    parser.add_argument('--model', default='gpt-6-astra')
    parser.add_argument('--phase', choices=('all', 'gates', 'episodes', 'report'), default='all')
    parser.add_argument('--persona', choices=tuple(SEEDS), action='append')
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--retry-invalid', action='store_true')
    args = parser.parse_args()
    study = args.study_dir.resolve()
    os.environ.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    plan = freeze(study, args.source_inventory.resolve(), read(args.bindings), args.model)
    roles = [r for r in plan['roles'] if not args.persona or r['persona'] in args.persona]
    if not args.run or args.phase == 'report':
        report = collect_report(study, plan)
        print(json.dumps({'validated': report['validated_count'], 'required': 10, 'run_enabled': False}))
        return 0
    lock = study / 'qualification.lock'
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    client = None
    try:
        with os.fdopen(descriptor, 'w') as handle:
            json.dump({'pid': os.getpid()}, handle)
        if args.phase in ('all', 'gates'):
            gate_errors = []
            for role in roles:
                if source_hashes() != plan['sources']:
                    raise RuntimeError('Sources changed during qualification gates')
                try:
                    ensure_gate(study, role, args.model)
                except Exception as error:
                    gate_errors.append({'persona': role['persona'], 'error_type': type(error).__name__, 'message': str(error)})
                    print('[checker gate blocked]', role['persona'], str(error), flush=True)
            save(study / 'gate-errors.json', gate_errors)
            if gate_errors:
                return 2
        if args.phase == 'gates':
            return 0
        import docker
        client = docker.from_env(timeout=60)
        reuse = read(args.gold_sources) if args.gold_sources else {}
        for role in roles:
            if source_hashes() != plan['sources']:
                raise RuntimeError('Sources changed during qualification')
            directory = study / 'qualification' / role['persona']
            attempts = sorted(directory.glob('attempt-*'))
            if attempts:
                result = existing_result(attempts[-1])
                if result['status'] == 'completed' or not args.retry_invalid:
                    print('[retained]', role['persona'], result['status'], flush=True)
                    continue
            task = next(t for t in plan['tasks'] if t['key'] == role['task_key'])
            task_manifest = Manifest(study / task['path'])
            image = client.images.get(task_manifest.docker_image)
            gold = study / 'gold' / task['key']
            if not gold.exists() and role['instance_id'] in reuse:
                source = Path(reuse[role['instance_id']]).resolve()
                gold_proof(source, task_manifest, image.id)
                shutil.copytree(source, gold)
            # A verified older gold cache can lack timing metadata. Validity is
            # established by its report and input/image identity, not a duration.
            try:
                gold_proof(gold, task_manifest, image.id)
            except (ValueError, FileNotFoundError):
                if ensure_gold(study, task, image.id, {'gold_seconds': 5400}, False) is None:
                    raise RuntimeError('Original gold verifier blocked for ' + role['instance_id'])
            ensure_gate(study, role, args.model)
            command = [sys.executable, '-u', str(HERE / 'run_reference.py'), '--run', '--task', str(study / task['path']),
                       '--gold-dir', str(gold), '--persona', role['persona'], '--agent-model', args.model,
                       '--arm', 'interactive', '--max-turns', str(plan['max_turns']), '--max-steps', str(plan['max_steps']),
                       '--interaction-seconds', '0', '--verification-seconds', '0']
            if role.get('binding_path'):
                command += ['--binding', str(study / role['binding_path'])]
            print('[qualification]', role['persona'], role['instance_id'], flush=True)
            result = supervise_sample(command, directory / f'attempt-{len(attempts)+1:03d}', plan['sample_safety_seconds'])
            print('[result]', role['persona'], result['status'], 'joint', result['joint_success'], flush=True)
            collect_report(study, plan)
        return 0 if collect_report(study, plan)['qualification_complete'] else 2
    finally:
        if client:
            client.close()
        lock.unlink(missing_ok=True)
        collect_report(study, plan)


if __name__ == '__main__':
    raise SystemExit(main())
