"""Build a portable DeepSWE Persona offline delivery using the reference layout.

Consumes an existing result export and its batch. No model, Docker or network calls.
This is a data reader/review package, not a TB3 experiment implementation.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import shutil
import tarfile
import tempfile

from . import delivery_reader
from .export_tb3_results import Package, dumps, identifier, sha, validate_package

ROOT = Path(__file__).resolve().parents[1]
VERSION = delivery_reader.VERSION
PAIR_GAPS = [
    ('pre_persona_blur_receipt', 'tests/persona/receipts/blur/', 'missing', 'The historical opening exists; a receipt proving freezing before persona loading is not available.'),
    ('tb3_objective_checker', 'tests/persona/checker.py', 'missing', 'Legacy checks mix executable checks and LLM judgment; no independent TB3 checker is certified.'),
    ('tb3_four_level_schema', 'tests/persona/closed_loop/response_schema.json', 'missing', 'Legacy statuses are retained; no four-level TB3 judgment was executed.'),
    ('tb3_judge_prompt', 'tests/persona/closed_loop/judge_prompt.md', 'partial', 'Source pointers to legacy prompt code are supplied; this is not a TB3 visible-message-only judge prompt.'),
    ('tb3_simulator_prompt', 'tests/persona/closed_loop/simulator_prompt.md', 'partial', 'Legacy simulator code is supplied; the TB3 requirement loop is not implemented.'),
    ('tb3_rubric', 'tests/persona/closed_loop/rubric.json', 'partial', 'Exact planned requirement text is exported without inventing TB3 criteria or acceptance anchors.'),
    ('tb3_requirement_loop', 'tests/persona/closed_loop/policy.json', 'partial', 'Existing staged gates and offline final review are retained, without MET-only advancement or reopening.'),
    ('tb3_visible_citations', 'results:persona-check.json', 'missing', 'No historical criterion_id/event_id/quote evidence is fabricated.'),
    ('tb3_budget_and_calibration', 'tests/persona/protocol.json', 'partial', 'Historical budgets are recorded; original-timeout accounting and mandatory six-case online calibration are not certified.'),
    ('persona_protocol_entrypoint', 'task.toml', 'missing', 'Original task configuration is preserved; it does not invoke a TB3 persona runner.'),
    ('optional_experience', 'tests/persona/closed_loop/experience_*.json', 'not_recorded_optional', 'No independent TB3 UX rubric or score was recorded.'),
]
GLOBAL_GAPS = [
    {'id': 'experiment_runner', 'status': 'missing', 'paths': ['dataset/run.py', 'dataset/run_batch.py', 'dataset/calibrate_judge.py'],
     'reason': 'The attachment supplies an offline reader and source snapshot only; it is not an installable TB3 execution runtime.'},
    {'id': 'official_harbor_packaging', 'status': 'not_validated', 'paths': ['dataset/dataset.toml'],
     'reason': 'The TOML inventory uses the documented adapter schema and tree hashes, not certified Harbor registry digests. Official Packager collection and acceptance remain untested.'},
    {'id': 'receiver_tb3_schema', 'status': 'not_validated', 'paths': ['schemas/delivery.schema.json'],
     'reason': 'Local adapter reader tested; receiver-specific TB3 JSON schemas and ingestion implementation are not available here.'},
]


def seal(directory):
    files = {p.relative_to(directory).as_posix(): delivery_reader.digest(p)
             for p in sorted(directory.rglob('*')) if p.is_file()}
    (directory / 'release-lock.json').write_bytes(dumps({'schema_version': VERSION, 'files': files}))
    sums = {**files, 'release-lock.json': delivery_reader.digest(directory / 'release-lock.json')}
    (directory / 'SHA256SUMS').write_text(''.join(f'{h}  {p}\n' for p, h in sorted(sums.items())))


def pair_id(task, persona):
    # Full identifiers remain in inventory; bounded directory names avoid collisions.
    return identifier(task) + '--' + sha((task + '\0' + identifier(persona)).encode())[:16]


def export_delivery(root, batch_dir, result_export, output):
    root, batch_dir, result_export, output = map(lambda p: Path(p).absolute(), (root, batch_dir, result_export, output))
    for p in (root, batch_dir, result_export, output):
        if any(x.is_symlink() for x in (p, *p.parents)) or '..' in p.parts:
            raise ValueError('Symlink/traversal paths are not accepted')
    if not batch_dir.is_relative_to(root / 'runs') or not result_export.is_relative_to(root):
        raise ValueError('Sources must be under the module root')
    if output.exists() or output.is_relative_to(root / 'runs') or output.is_relative_to(result_export) or root.is_relative_to(output):
        raise ValueError('Use a new output directory outside source runs/results')
    result_validation = validate_package(result_export)
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.' + output.name + '-', dir=output.parent))
    try:
        pkg = Package(root, stage)
        manifest = pkg.read(batch_dir / 'manifest.json')
        copied_manifest = pkg.read(result_export / 'provenance/source-batch-manifest.json')
        if manifest != copied_manifest:
            raise ValueError('Result export belongs to a different batch manifest')
        entries = pkg.read(result_export / 'results.json')['entries']
        by_run = {e['run_id']: e for e in entries}
        if set(by_run) != {j['run_id'] for j in manifest['jobs']}:
            raise ValueError('Batch/result run identities differ')
        for p in sorted(result_export.rglob('*')):
            if p.is_symlink():
                raise ValueError('Symlink in result export')
            if p.is_file():
                pkg.copy(p, Path('results') / p.relative_to(result_export))
        catalog_path = root / 'data/personas.v3.json'
        personas = {p['id']: p for p in pkg.read(catalog_path)['personas']}
        pins = manifest.get('sources', {})

        def pinned(path):
            data = pkg.source_bytes(path)
            rel = path.relative_to(root).as_posix()
            if rel not in pins or sha(data) != pins[rel]:
                raise ValueError('Historical source identity not established: ' + rel)
            return json.loads(data)

        pinned(catalog_path)
        # Exact current source snapshots for inspection, not silently attributed to old runs.
        runtime = []
        for p in sorted((root / 'dspilot').glob('*.py')):
            ref = pkg.copy(p, Path('dataset/runtime/source/dspilot') / p.name)
            old = pins.get(p.relative_to(root).as_posix())
            runtime.append({**ref, 'historical_sha256': old, 'matches_historical': ref['sha256'] == old if old else None})
        pkg.write('dataset/runtime/source-index.json', {'scope': 'Current source for inspection; no experiment launcher or dependency environment supplied', 'files': runtime})
        reader = Path(delivery_reader.__file__)
        for dest in ('read_delivery.py', 'dataset/runtime/read_delivery.py'):
            target = stage / dest
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(reader.read_bytes())
        pkg.write('dataset/provenance/reader-implementation.json', {
            'sha256': delivery_reader.digest(reader), 'scope': 'Current export reader, not historical experiment code'})
        pkg.copy(batch_dir / 'manifest.json', 'dataset/provenance/source-batch-manifest.json')
        for filename in ('NOTICE.md', 'sources.lock.json', 'pier-source-lock.json', 'requirements-pier.lock'):
            pkg.copy(root / filename, Path('dataset/provenance') / filename)
        for source, dest in ((root.parent / 'LICENSE' if (root.parent / 'LICENSE').exists() else root / 'LICENSE', 'dataset/provenance/PersonaCodeBench-LICENSE'),
                             (root / '.cache/deepswe-source/LICENSE', 'dataset/provenance/DeepSWE-LICENSE'),
                             (root.parent / 'TB3_DELIVERY_FORMAT.md', 'dataset/provenance/TB3_DELIVERY_FORMAT.md')):
            if source.is_file() and not source.is_symlink():
                data = source.read_bytes()
                target = stage / dest; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)

        groups = {}
        for job in manifest['jobs']:
            key = (identifier(job['task_id']), identifier(job['persona_id']))
            groups.setdefault(key, []).append(job)
        rows, gap_counts, source_tasks = [], Counter(), {}
        for (task_id, persona_id), jobs in groups.items():
            delivery_id = pair_id(task_id, persona_id)
            taskdir = Path('dataset/tasks') / delivery_id
            ext = taskdir / 'tests/persona'
            task = pinned(root / 'data/tasks' / (task_id + '.json'))
            binding = pinned(root / 'data/bindings' / f'{task_id}--{persona_id}.json')
            configs = [pkg.read(root / 'runs' / identifier(j['run_id']) / 'config.json') for j in jobs]
            for j, cfg in zip(jobs, configs):
                if any(cfg[k] != j[k] for k in ('task_id', 'persona_id', 'arm')):
                    raise ValueError('Run/config identity mismatch')
            plan = configs[0]['plan']
            if any(c['plan'] != plan for c in configs) or any(plan[k] != v for k, v in (('task_id', task_id), ('persona_id', persona_id))):
                raise ValueError('Same pair has divergent plans; export distinct protocol versions separately')
            source = task['source']
            source_tasks[task_id] = source
            if source.get('lock_file'):
                lock = pkg.read(delivery_reader.local(root, source['lock_file']))
                if lock['task'] != task_id:
                    raise ValueError('Source lock task mismatch')
                original_files = lock['task_files']
            else:
                original_files = source['files']
            cache = delivery_reader.local(root, '.cache/tasks/' + task.get('cache_dir', task_id))
            for rel, h in original_files.items():
                path = delivery_reader.local(cache, rel)
                if sha(pkg.source_bytes(path)) != h:
                    raise ValueError('Original task source changed: ' + task_id + '/' + rel)
                pkg.copy(path, taskdir / rel)
                if rel in ('instruction.md', 'task.toml'):
                    pkg.copy(path, ext / 'original' / rel)
            # Preserve the full original task configuration; no fake protocol entrypoint.
            pkg.copy(cache / 'instruction.md', taskdir / 'README.md')
            (stage / taskdir / 'instruction.md').write_text(plan['opening'])
            pkg.write(ext / 'persona.json', personas[persona_id])
            pkg.write(ext / 'binding.json', binding)
            pkg.write(ext / 'receipts/binding/source-identity.json', {
                'schema_version': VERSION, 'task_id': task_id, 'persona_id': persona_id,
                'source_binding_sha256': sha(dumps(binding)),
                'note': 'Parsed binding identity; exact raw source hash is recorded in provenance/source-files.json. Not a new semantic approval.'})
            pkg.write(ext / 'legacy-plan.json', plan)
            pkg.write(ext / 'receipts/source-protocol-deepswe.json', {
                'schema_version': VERSION, 'dataset_family': 'deepswe',
                'task_id': task_id, 'persona_id': persona_id,
                'source_protocol': plan['protocol'], 'source_commit': manifest.get('source_commit'),
                'source_batch': batch_dir.name, 'source_run_ids': [j['run_id'] for j in jobs],
                'provenance_kind': 'Recorded historical DeepSWE execution; not a TB3 v5 migration or new approval'})
            pkg.write(ext / 'legacy-task-profile.json', task)
            pkg.write(ext / 'blurred_task.json', {'schema_version': VERSION, 'opening': plan['opening'],
                'task_units': plan.get('task_units'), 'stages': plan['stages'],
                'source': 'Historical execution config.plan; opening seed, not claimed to be the complete generated first user message',
                'frozen_before_persona_loading': None, 'tb3_blur_receipt': None})
            pkg.write(ext / 'interaction.json', {'schema_version': VERSION, 'opening': plan['opening'],
                'initial_exchange': plan.get('initial_exchange'), 'initial_plan_request': plan.get('initial_plan_request'),
                'implementation_authorization': plan.get('implementation_authorization'),
                'task_disclosure_mode': plan.get('task_disclosure_mode'), 'persona_disclosure_mode': plan.get('persona_disclosure_mode'),
                'stages': plan['stages']})
            raw_requirements = {r['id']: r for r in binding['requirements']}
            raw_requirements.update({r['id']: r for r in plan.get('additional_requirements', [])})
            requirements = [{'requirement_id': rid, 'disclosure': life['statement'], 'lifecycle': life,
                             'source_binding_requirement': raw_requirements.get(rid),
                             'plan_override': plan.get('requirement_overrides', {}).get(rid),
                             'tb3_criteria': None, 'checker_scope': 'Legacy definitions; runtime may further specialize them. Not an independent objective checker.'}
                            for rid, life in plan['requirements'].items()]
            pkg.write(ext / 'evolved_requirements.json', {'schema_version': VERSION, 'task_id': task_id,
                'persona_id': persona_id, 'requirements': requirements})
            pkg.write(ext / 'closed_loop/rubric.json', {'schema_version': VERSION, 'scope': 'Exact legacy planned disclosures; TB3 criterion schema/anchors missing',
                'requirements': [{'requirement_id': r['requirement_id'], 'contract': r['disclosure'], 'tb3_criteria': None} for r in requirements]})
            pkg.write(ext / 'closed_loop/policy.json', {'schema_version': VERSION, 'source_protocol': plan['protocol'],
                'stages': plan['stages'], 'budgets': configs[0]['budgets'],
                'tb3_requirement_loop': False, 'final_review_reopens_interaction': False,
                'online_evidence': 'Legacy mixed messages, code snapshots, tool records and public checks'})
            pkg.write(ext / 'protocol.json', {'schema_version': VERSION, 'source_protocol': plan['protocol'],
                'dataset_family': 'deepswe', 'format_reference': 'TB3_DELIVERY_FORMAT.md',
                'reference_protocol': 'tb3-requirement-loop-v1', 'reference_protocol_executed': False,
                'historical_budgets': configs[0]['budgets'], 'tb3_online_calibration': None,
                'experiment_runnable_from_this_package': False})
            pkg.write(ext / 'provenance.json', {'schema_version': VERSION, 'source': source,
                'original_files': original_files, 'source_commit': manifest.get('source_commit'),
                'plan_source': 'results trial agent-private/audit/source-config.json',
                'current_runtime_snapshot': 'dataset/runtime/source-index.json'})
            pkg.write(ext / 'closed-loop-provenance.json', {'schema_version': VERSION,
                'dataset_family': 'deepswe', 'source_protocol': plan['protocol'],
                'source_receipt': 'receipts/source-protocol-deepswe.json', 'reference_closed_loop_executed': False,
                'conversion': 'Offline data organization only; historical semantics retained'})
            for kind, modules in [('judge', ['checks.py', 'checks_v3.py', 'checks_staged.py', 'check_contracts.py']),
                                  ('simulator', ['simulator_staged.py', 'staged.py'])]:
                text = 'Legacy source reading index; NOT a TB3 prompt.\n\nComplete current Python source snapshots are at:\n'
                text += ''.join(f'- dataset/runtime/source/dspilot/{name}\n' for name in modules)
                text += '\nPrompts are assembled dynamically. A complete per-trial TB3 prompt/schema is unavailable; historical API audits are linked from the trial.\n'
                (stage / ext / 'closed_loop' / f'{kind}_prompt.md').write_text(text)
            (stage / ext / 'closed_loop/README.md').write_text('Offline legacy rule inspection only. See delivery-status.json in the parent directory for missing TB3 fields. No experiment entrypoint is supplied.\n')
            gaps = [dict(id=k, path=p, status=s, reason=r) for k, p, s, r in PAIR_GAPS]
            gap_counts.update(g['id'] for g in gaps if g['status'] not in ('not_applicable', 'not_recorded_optional'))
            pkg.write(ext / 'delivery-status.json', {'schema_version': VERSION, 'task_id': task_id,
                'persona_id': persona_id, 'tb3_protocol_compliant': False, 'items': gaps})
            files = {p.relative_to(stage / ext).as_posix(): delivery_reader.digest(p)
                     for p in sorted((stage / ext / 'closed_loop').iterdir()) if p.is_file()}
            pkg.write(ext / 'closed_loop/manifest.json', {'schema_version': VERSION, 'files_relative_to_persona_dir': files,
                'source_plan_sha256': sha(dumps(plan)), 'tb3_compliant': False})
            trials = []
            for job in jobs:
                entry = by_run[job['run_id']]
                if any(entry[k] != job[k] for k in ('task_id', 'persona_id', 'arm')):
                    raise ValueError('Result identity mismatch')
                trials.append({'run_id': job['run_id'], 'arm': job['arm'], 'source_ordinal': job.get('ordinal'),
                               'trial_directory': entry['trial_directory'], 'status': entry['status']})
            rows.append({'delivery_id': delivery_id, 'task_id': task_id, 'persona_id': persona_id,
                         'task_directory': taskdir.as_posix(), 'requirement_count': len(requirements), 'trials': trials})

        dataset_name = f'deepswe-persona-{len(rows)}pairs'
        pkg.write('dataset/inventory.json', {'schema_version': VERSION,
            'dataset_name': dataset_name, 'dataset_family': 'deepswe', 'trial_path_base': 'results_package', 'pairs': rows})
        pkg.write('dataset/source-deepswe.json', {'schema_version': VERSION,
            'dataset_family': 'deepswe', 'source_batch': batch_dir.name,
            'source_commit': manifest.get('source_commit'),
            'source_manifest': 'provenance/source-batch-manifest.json',
            'format_reference': 'TB3_DELIVERY_FORMAT.md sections 2 and 3; names and lineage adapted to DeepSWE',
            'tasks': [{'task_id': key, 'source': value} for key, value in sorted(source_tasks.items())],
            'pairs': [{k: r[k] for k in ('delivery_id', 'task_id', 'persona_id')} for r in rows],
            'semantic_approval_claimed': False})
        pkg.write('dataset/registry.json', {'schema_version': VERSION, 'identity_policy': 'One unique delivery task directory per task-persona pair; original IDs retained',
            'pairs': [{k: r[k] for k in ('delivery_id', 'task_id', 'persona_id', 'task_directory')} for r in rows]})
        status = {'schema_version': VERSION, 'purpose': 'offline_delivery_review', 'data_readable': True,
                  'tb3_protocol_compliant': False, 'experiment_runnable': False,
                  'official_harbor_validation': 'not_performed', 'missing_item_counts': dict(gap_counts),
                  'global_gaps': GLOBAL_GAPS, 'pair_gap_definitions': [dict(id=k, path=p, status=s, reason=r) for k, p, s, r in PAIR_GAPS]}
        pkg.write('delivery-status.json', status)
        pkg.write('dataset/release-manifest.json', {'schema_version': VERSION, 'batch_id': batch_dir.name,
            'dataset_name': dataset_name, 'dataset_family': 'deepswe', 'release_name': output.name,
            'format_reference': 'TB3_DELIVERY_FORMAT.md', 'source_index': 'source-deepswe.json',
            'created_at_utc': datetime.now(timezone.utc).isoformat(), 'pairs': len(rows),
            'original_tasks': len({r['task_id'] for r in rows}), 'trials': len(entries),
            'results_reference': {'status': 'external', 'path_base': 'results_package',
                'index_sha256': delivery_reader.digest(stage / 'results/results.json'),
                'manifest_sha256': delivery_reader.digest(stage / 'results/export-manifest.json'),
                'resolution': 'Outer package: results/. Standalone dataset: explicit --results-dir.'},
            'tb3_protocol_compliant': False, 'experiment_runnable': False,
            'source_results_validation': result_validation})
        pkg.write('schemas/delivery.schema.json', {'$schema': 'https://json-schema.org/draft/2020-12/schema',
            'title': 'DeepSWE delivery adapter inventory; not the official TB3 schema', 'type': 'object',
            'required': ['schema_version', 'pairs'], 'properties': {'schema_version': {'const': VERSION},
                'pairs': {'type': 'array', 'items': {'type': 'object', 'required': ['delivery_id', 'task_id', 'persona_id', 'task_directory', 'trials'],
                    'properties': {k: {'type': 'string'} for k in ('delivery_id', 'task_id', 'persona_id', 'task_directory')}}}}})
        pkg.write('schemas/field-map.json', {'schema_version': VERSION, 'mappings': {
            'persona-result.original_reward': 'Historical result.reward; preserved',
            'persona-result.benchmark_outcome': 'DeepSWE v2: explicit deepswe-staged-outcome-v1 aggregation; legacy v1: null; neither claims TB3 acceptance',
            'persona-result.requirement_judgments': 'Recorded online checks/controller state with per-requirement source references; distinct from final review',
            'persona-result.checker_results': 'Recorded final mixed checker results; independent objective acceptance not certified',
            'persona-check.requirements[].tb3_verdict': 'null: no historical four-level judgment',
            'persona-check.requirements[].objective_verdict': 'null: mixed legacy check is not certified independent checker',
            'episode.tb3_requirement_judgments': 'null: actual legacy events and references retained',
            'closed_loop/rubric.requirements[].contract': 'Historical config.plan.requirements[id].statement; exact text',
            'task.toml': 'Original source bytes; no TB3 protocol entrypoint added',
            'instruction.md': 'Historical plan.opening seed; pre-persona blur freeze not established'}})
        # Attachment is an honest offline reader bundle, not a pretend experiment runner.
        (stage / 'dataset/requirements.txt').write_text('# Offline reader requires Python >=3.11 and standard library only.\n# No experiment runtime dependencies are installed by this package.\n')
        with tarfile.open(stage / 'dataset/runtime_bundle.tar.gz', 'w:gz') as archive:
            for p in sorted((stage / 'dataset/runtime').rglob('*')):
                if p.is_file():
                    archive.add(p, arcname=p.relative_to(stage / 'dataset').as_posix(), recursive=False)
        toml = f'schema_version = "{VERSION}"\n[dataset]\nname = "{dataset_name}"\nfamily = "deepswe"\nsource_index = "source-deepswe.json"\n'
        toml += 'purpose = "offline-data-review"\nexperiment_runnable = false\nharbor_schema_certified = false\ndigest_algorithm = "sha256-of-sorted-file-hash-json"\n'
        toml += f'runtime_bundle_sha256 = "{delivery_reader.digest(stage / "dataset/runtime_bundle.tar.gz")}"\n'
        for row in rows:
            folder = stage / row['task_directory']
            tree = {p.relative_to(folder).as_posix(): delivery_reader.digest(p) for p in sorted(folder.rglob('*')) if p.is_file()}
            toml += f'\n[[tasks]]\ndelivery_id = "{row["delivery_id"]}"\npath = "tasks/{row["delivery_id"]}"\nsha256 = "{sha(dumps(tree))}"\n'
        (stage / 'dataset/dataset.toml').write_text(toml)
        readme = f'''# DeepSWE Persona delivery: {len(rows)} task–persona pairs

This package contains DeepSWE Persona data. TB3 supplies a layout example only;
it is not this dataset's name, source cohort or executed protocol.
source-deepswe.json records the real dataset lineage; each task includes
tests/persona/receipts/source-protocol-deepswe.json. The example's 52-task and v5
source filenames do not apply to this dataset.
It is readable with the supplied adapter; it is NOT a certified TB3/Harbor release
and cannot run persona experiments. Missing fields stay null or are listed in
delivery-status.json; no checker, calibration, four-level judgment or approval is invented.

Requires Python 3.11+; no installation, API key, network or Docker is needed.
From the extracted package root:

    python3 -B read_delivery.py
    python3 -B read_delivery.py --pair {rows[0]['delivery_id']}

Paths in dataset/inventory.json are relative to this package root. Result-internal
paths are relative to results/. One directory represents one task–persona pair;
task_id remains the original task identity. Original files live under each task;
original instruction and task.toml are also saved in tests/persona/original/.
The root instruction is the historical opening seed, not a claim of pre-persona
freezing. The original task.toml has no new persona entrypoint: do not execute this
review directory as a TB3 benchmark.

dataset/runtime_bundle.tar.gz contains the offline reader and CURRENT source
snapshots for inspection; source-index.json distinguishes these from historical
source hashes. It is not an installable experiment runtime. Results preserve
actual historical traces and legacy scores. See schemas/field-map.json for field
meanings and delivery-status.json for missing requirements. The optional UX score
is not recorded. Original solutions are only for original tasks, not persona positives.

Open index.html to inspect the pair index and gaps. Local validation checks every
file hash, JSON/TOML parsing, pair/result links and preservation of original task
bytes; it does not certify a receiver's unavailable TB3 schema or official Harbor
packaging. Credentials/env files are excluded by allowlisted source selection;
historical runtime redactions are preserved in copied evidence.
'''
        (stage / 'README.md').write_text(readme)
        (stage / 'dataset/README.md').write_text(readme)
        body = '<!doctype html><meta charset="utf-8"><title>DeepSWE Persona 数据交付</title><style>body{font:16px system-ui;max-width:1250px;margin:40px auto}table{border-collapse:collapse;width:100%}td,th{padding:9px;border:1px solid #ddd;text-align:left}code{background:#eee}</style>'
        body += f'<h1>DeepSWE Persona 数据交付：{len(rows)} 个 pair</h1><p>TB3 仅作为目录与字段格式参考，数据来源和实验协议保留 DeepSWE 记录。可离线读取；尚不包含完整实验执行入口。</p><p><a href="dataset/source-deepswe.json">DeepSWE 来源</a> · <a href="delivery-status.json">缺失项</a> · <a href="dataset/inventory.json">配对清单</a> · <a href="schemas/field-map.json">字段映射</a> · <a href="results/results.json">实验结果索引</a></p>'
        body += '<h2>待补充内容</h2><table><tr><th>项目</th><th>状态</th><th>原因</th></tr>'
        for k, _, s, reason in PAIR_GAPS:
            body += '<tr>' + ''.join('<td>' + html.escape(v) + '</td>' for v in (k, s, reason)) + '</tr>'
        body += '</table><h2>Pair 与结果</h2><table><tr><th>原题</th><th>Persona</th><th>需求数</th><th>规则及缺失项</th><th>历史结果</th></tr>'
        for row in rows:
            ext = row['task_directory'] + '/tests/persona/'
            links = ' '.join(f'<a href="results/{html.escape(t["trial_directory"])}/persona-result.json">{html.escape(t["arm"])}: {html.escape(t["status"])}</a>' for t in row['trials'])
            body += f'<tr><td>{html.escape(row["task_id"])}</td><td>{html.escape(row["persona_id"])}</td><td>{row["requirement_count"]}</td><td><a href="{ext}evolved_requirements.json">需求</a> / <a href="{ext}delivery-status.json">缺失</a></td><td>{links}</td></tr>'
        (stage / 'index.html').write_text(body + '</table>')
        pkg.verify_sources()
        pkg.write('dataset/provenance/source-files.json', pkg.sources)
        seal(stage / 'dataset')
        seal(stage)
        validation = delivery_reader.validate(stage)
        if output.exists():
            raise ValueError('Output appeared; refusing overwrite')
        stage.rename(output)
        return {**validation, 'output': str(output)}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch-dir', type=Path, required=True)
    parser.add_argument('--results', type=Path, required=True, help='Existing export_tb3_results package')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export_delivery(ROOT, args.batch_dir, args.results, args.output), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
