"""Build unexecuted DeepSWE delivery inputs, and materialize real run receipts."""
import json
from pathlib import Path
import shutil
from . import delivery_runtime as rt, delivery_capture as capture
from .export_tb3_delivery import pair_id, seal
from .delivery_reader import VERSION


def selection(path):
    value = rt.read(path)
    rows = value if isinstance(value, list) else value.get('pairs', value.get('jobs', []))
    chosen = {}; plans = {}
    for row in rows:
        key = row['task_id'], row['persona_id']
        pair_id(*key)  # Validate both identifiers before file access.
        if key in chosen and plans[key] != row.get('plan'): raise ValueError('Divergent plans for selected pair')
        chosen[key] = row; plans[key] = row.get('plan')
    if not chosen: raise ValueError('Selection contains no pairs')
    return list(chosen.values())


def seed(root, selected_file, outer):
    selected = selection(selected_file)
    dataset = outer / 'dataset'
    # Every task-only blur is completed before the persona catalog is read here.
    blur = {task: capture.prepare_blur(root, task) for task in sorted({p['task_id'] for p in selected})}
    personas = {p['id']: p for p in rt.read(root / 'data/personas.v3.json')['personas']}
    rows = []; tasks = {}
    for row in selected:
        task_id, persona_id = row['task_id'], row['persona_id']; identity = pair_id(task_id, persona_id)
        profile = rt.read(root / 'data/tasks' / (task_id + '.json'))
        source = profile['source']; cache = rt.safe(root, '.cache/tasks/' + profile.get('cache_dir', task_id))
        original = rt.read(rt.safe(root, source['lock_file']))['task_files'] if source.get('lock_file') else source['files']
        folder = dataset / 'tasks' / identity; ext = folder / 'tests/persona'
        for name, digest in original.items():
            path = rt.safe(cache, name)
            if rt.sha(path) != digest: raise ValueError('Original task checksum mismatch: ' + task_id + '/' + name)
            dest = rt.safe(folder, name); dest.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(path, dest)
        (ext / 'original').mkdir(parents=True)
        for name in ('instruction.md', 'task.toml'): shutil.copy2(cache / name, ext / 'original' / name)
        shutil.copy2(cache / 'instruction.md', folder / 'README.md')
        plan_path = rt.safe(root, row.get('plan') or f'data/staged_bindings/{task_id}--{persona_id}.json')
        plan = rt.read(plan_path)
        if (plan['task_id'], plan['persona_id']) != (task_id, persona_id): raise ValueError('Selected plan identity mismatch')
        binding = rt.read(root / 'data/bindings' / f'{task_id}--{persona_id}.json')
        (folder / 'instruction.md').write_text(plan['opening'])
        for name, value in {'persona.json': personas[persona_id], 'binding.json': binding,
            'legacy-plan.json': plan, 'legacy-task-profile.json': profile,
            'blurred_task.json': {**blur[task_id]['blur'], 'frozen_before_persona_loading': True,
                'freeze_scope': 'Isolated task-only construction before persona binding; not a claim about historical runs'},
            'receipts/blur/task-blur.json': blur[task_id]['blur'],
            'receipts/blur/receipt.json': blur[task_id]['receipt'],
            'receipts/binding/source-identity.json': {'task_id': task_id, 'persona_id': persona_id},
            'receipts/source-protocol-deepswe.json': {'source_protocol': plan['protocol'], 'source_kind': 'unexecuted_registered_plan',
                'task_id': task_id, 'persona_id': persona_id, 'source_plan_sha256': rt.sha(plan_path)},
            'evolved_requirements.json': {'task_id': task_id, 'persona_id': persona_id, 'requirements': [
                {'requirement_id': key, 'disclosure': life['statement'], 'lifecycle': life}
                for key, life in plan['requirements'].items()]},
            'interaction.json': {'opening_seed': plan['opening'], 'opening_status': 'pending_runtime_freeze'},
            'provenance.json': {'original_files': original, 'source': source, 'plan_source': str(plan_path.relative_to(root))},
            'closed-loop-provenance.json': {'source_protocol': plan['protocol'], 'execution_status': 'not_run'},
            'closed_loop/manifest.json': {}, 'closed_loop/rubric.json': {}, 'closed_loop/policy.json': {},
        }.items(): rt.dump(ext / name, value)
        rows.append({'delivery_id': identity, 'task_id': task_id, 'persona_id': persona_id,
            'task_directory': 'dataset/tasks/' + identity, 'requirement_count': len(plan['requirements']), 'trials': []})
        tasks[task_id] = source
    rt.dump(dataset / 'inventory.json', {'schema_version': VERSION, 'dataset_name': f'deepswe-persona-{len(rows)}pairs', 'pairs': rows})
    rt.dump(dataset / 'registry.json', {'schema_version': VERSION, 'pairs': [{k:v for k,v in r.items() if k != 'trials'} for r in rows]})
    rt.dump(dataset / 'source-deepswe.json', {'dataset_family': 'deepswe', 'tasks': tasks, 'source_selection_sha256': rt.sha(selected_file)})
    rt.dump(dataset / 'release-manifest.json', {'schema_version': VERSION, 'pairs': len(rows), 'original_tasks': len(tasks),
        'trials': 0, 'dataset_family': 'deepswe', 'preparation_only': True,
        'results_reference': {'status': 'not_run', 'path_base': 'results_package'}})
    rt.dump(outer / 'results/results.json', {'entries': [], 'status': 'not_run'})
    rt.dump(outer / 'delivery-status.json', {'tb3_protocol_compliant': False, 'experiment_runnable': False})
    rt.dump(outer / 'schemas/field-map.json', {'mappings': {}})
    (outer / 'index.html').write_text('<!doctype html><meta charset="utf-8"><h1>DeepSWE prospective delivery</h1><h2>待补充内容</h2><h2>Pair 与结果</h2><p>Prepared inputs; no scored results exist yet.</p>')
    (dataset / 'runtime/source').mkdir(parents=True)
    rt.dump(dataset / 'runtime/source-index.json', {})
    rt.dump(dataset / 'provenance/source-files.json', {'scope': 'Prospective input preparation, not historical results'})
    rt.dump(dataset / 'provenance/source-selection.json', rt.read(selected_file))
    for src, dest in [(root.parent / 'LICENSE' if (root.parent/'LICENSE').exists() else root/'LICENSE', 'PersonaCodeBench-LICENSE'), (root / '.cache/deepswe-source/LICENSE', 'DeepSWE-LICENSE')]:
        shutil.copy2(src, dataset / 'provenance' / dest)
    for dest in (outer / 'read_delivery.py', dataset / 'runtime/read_delivery.py'):
        shutil.copy2(root / 'dspilot/delivery_reader.py', dest)
    (dataset / 'dataset.toml').write_text('')
    seal(dataset); seal(outer)


def materialize_opening(root, folder, row):
    """Return status; never promote an old seed to a frozen message."""
    ext = folder / 'tests/persona'; candidates = []
    for trial in row['trials']:
        if trial['arm'] != 'pivot': continue
        run = rt.safe(root, 'runs/' + trial['run_id']); delivery = run / 'delivery'
        if not (delivery / 'opening-receipt.json').exists(): continue
        turns = rt.read(run / 'turns.json')
        first = next((t['content'] for t in turns if t['speaker'] == 'user'), None)
        if first is None: raise ValueError('Frozen opening has no recorded delivery')
        receipt = capture.verify_opening(delivery, task_id=row['task_id'], persona_id=row['persona_id'], arm='pivot',
            plan=rt.read(ext / 'legacy-plan.json'), delivered_message=first)
        candidates.append((delivery, receipt))
    if not candidates:
        return 'pending_runtime_freeze' if not row['trials'] else 'historical_opening_seed_only'
    if len({r['instruction_sha256'] for _, r in candidates}) != 1:
        raise ValueError('Different frozen first messages for this pair; export distinct attempts separately')
    source, receipt = candidates[0]
    shutil.copy2(source / 'instruction.md', folder / 'instruction.md')
    for name, dest in [('task-blur.json','receipts/blur/task-blur.json'),('blur-receipt.json','receipts/blur/receipt.json'),
                       ('opening-receipt.json','receipts/opening.json')]:
        target=ext/dest;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source/name,target)
    blur = rt.read(source / 'task-blur.json')
    rt.dump(ext / 'blurred_task.json', {**blur, 'frozen_before_persona_loading': True,
        'freeze_scope': 'Isolated task-only generation; complete persona opening frozen separately before first agent action',
        'complete_opening_path': '../../instruction.md', 'opening_receipt': 'receipts/opening.json'})
    return 'frozen_before_first_agent_action'
