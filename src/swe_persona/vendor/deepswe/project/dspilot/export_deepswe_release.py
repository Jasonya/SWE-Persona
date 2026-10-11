"""Package the existing DeepSWE engine, runnable entrypoints and per-pair rules.

Builds a fresh version without changing previous deliveries or historical runs.
Only offline structural checks are executed while building this release.
"""
import argparse
import hashlib
import json
import os
import re
import runpy
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

from .export_tb3_delivery import export_delivery, seal
from .export_tb3_results import dumps
from . import delivery_runtime, delivery_rules, delivery_reader
from . import delivery_harbor, delivery_calibration, delivery_dataset, delivery_experience
from . import delivery_metadata, delivery_timeouts

ROOT = Path(__file__).resolve().parents[1]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dumps(value))


def entrypoint(batch=False):
    return ('import sys\nfrom pathlib import Path\nsys.dont_write_bytecode = True\n'
            'sys.path.insert(0, str(Path(__file__).resolve().parent))\n'
            'from runtime.delivery_runtime import main\n'
            f'if __name__ == "__main__": main(Path(__file__).resolve().parent, batch={batch!r})\n')


def module_entrypoint(module):
    return ('import sys\nfrom pathlib import Path\nsys.dont_write_bytecode = True\n'
            'root = Path(__file__).resolve().parent\n'
            'sys.path.insert(0, str(root/"runtime/project"))\n'
            f'from dspilot.{module} import main\n'
            'if __name__ == "__main__": main(root)\n')


def task_identity(config, name):
    match = re.search(r'(?ms)^\[task\]\s*\n(.*?)(?=^\[|\Z)', config)
    if match:
        body = re.sub(r'(?m)^name\s*=.*$', 'name = ' + json.dumps(name), match.group(1), count=1)
        if body == match.group(1) and not re.search(r'(?m)^name\s*=', body):
            body = 'name = ' + json.dumps(name) + '\n' + body
        return config[:match.start(1)] + body + config[match.end(1):]
    return config + '\n[task]\nname = ' + json.dumps(name) + '\n'


def copy_source(source, target, records):
    if source.is_symlink(): raise ValueError('Source symlink rejected')
    if source.is_dir():
        for p in sorted(source.iterdir()):
            if p.name in ('__pycache__', '.env', '.git') or p.suffix in ('.pyc', '.env'): continue
            copy_source(p, target / p.name, records)
    elif source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        records.append((source, delivery_runtime.sha(source), target))


def package(root, batch, results, output, *, selection=None, require_frozen_openings=False, runtime_source=None):
    root, output = Path(root).absolute(), Path(output).absolute()
    runtime_source = Path(runtime_source).absolute() if runtime_source else root
    batch = Path(batch).absolute() if batch else None
    results = Path(results).absolute() if results else None
    if output.exists() or output.is_relative_to(root / 'runs') or (results and output.is_relative_to(results)) or root.is_relative_to(output):
        raise ValueError('Use a fresh release directory outside sources')
    if any(p.is_symlink() for p in (output, *output.parents)) or '..' in output.parts:
        raise ValueError('Unsafe output directory')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.deepswe-release-', dir=output.parent))
    try:
        outer = temporary / output.name
        if selection:
            delivery_dataset.seed(root, Path(selection).absolute(), outer)
        else:
            export_delivery(root, batch, results, outer)
        dataset = outer / 'dataset'; project = dataset / 'runtime/project'; records = []
        for base in (outer, dataset):
            (base / 'release-lock.json').unlink(); (base / 'SHA256SUMS').unlink()
        # Replace the inspection-only snapshot by a complete relocatable project.
        shutil.rmtree(dataset / 'runtime/source')
        (dataset / 'runtime/source-index.json').unlink()
        for name in ('dspilot', 'data', 'environments', 'sources.lock.json', 'pier-source-lock.json',
                     'requirements-pier.lock', 'requirements-terminus2.lock', 'requirements-harbor-packaging.lock', 'NOTICE.md'):
            copy_source((runtime_source if name == 'dspilot' else root) / name, project / name, records)
        shipped_judge = runpy.run_path(str(project / 'dspilot/requirement_judge.py'))
        quote_policy = shipped_judge.get('QUOTE_POLICY', 'exact-contiguous-substring-v1')
        copy_source(root.parent/'LICENSE' if (root.parent/'LICENSE').exists() else root/'LICENSE', project/'LICENSE', records)
        copy_source(root/'.cache/deepswe-source/LICENSE', project/'.cache/deepswe-source/LICENSE', records)
        rows = delivery_runtime.pairs(dataset)
        for task_id in sorted({p['task_id'] for p in rows}):
            profile = delivery_runtime.read(root / 'data/tasks' / (task_id + '.json'))
            cache_name = profile.get('cache_dir', task_id)
            copy_source(root / '.cache/tasks' / cache_name, project / '.cache/tasks' / cache_name, records)
        for pair in rows:
            ext = delivery_runtime.task_path(dataset, pair) / 'tests/persona'
            shutil.copy2(ext / 'legacy-plan.json', project / 'data/staged_bindings' / f'{pair["task_id"]}--{pair["persona_id"]}.json')
        # All rule derivations are executed in the shipped project, with no API or Docker calls.
        rules = delivery_runtime.command(project, rows[0], 'legacy', 'rules',
                                          arguments=('--pairs-json', json.dumps(rows)))
        by_id = {r['pair_id']: r for r in rules}
        schema = delivery_rules.response_schema(project)
        common = dataset / 'prompts'; common.mkdir(exist_ok=True)
        write(common / 'response_schema.json', schema)
        for role in ('judge', 'simulator'):
            doc = delivery_rules.prompt_document(project, role)
            write(common / (role + '-builders.json'), doc)
            (common / (role + '_prompt.md')).write_text(delivery_rules.markdown(doc))
        copy_source(runtime_source / 'dspilot/delivery_runtime.py', dataset / 'runtime/delivery_runtime.py', records)
        copy_source(runtime_source / 'dspilot/api_config.py', dataset / 'runtime/api_config.py', records)
        (dataset / 'runtime/__init__.py').write_text('')
        (dataset / 'run.py').write_text(entrypoint())
        (dataset / 'run_batch.py').write_text(entrypoint(True))
        (dataset / 'calibrate_judge.py').write_text(module_entrypoint('delivery_calibration'))
        (dataset / 'validate_harbor.py').write_text(module_entrypoint('delivery_harbor'))
        (dataset / 'finalize_delivery.py').write_text(module_entrypoint('delivery_finalize'))
        (dataset / 'configure_runtime.py').write_text(module_entrypoint('delivery_timeouts'))
        (dataset / 'validate_release.py').write_text('''import argparse, json, sys
from pathlib import Path
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime.delivery_runtime import verify_dataset, pairs, command
from runtime.read_delivery import result_links
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--results-dir', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    result = verify_dataset(root)
    result['results'] = result_links(root, args.results_dir)
    rows = pairs(root)
    result['plans'] = command(root/'runtime/project', rows[0], 'legacy', 'validate', arguments=('--pairs-json', json.dumps(rows)))
    print(json.dumps(result, indent=2))
''')
        (dataset / 'requirements.txt').write_text('# Legacy/native_act: Python >=3.11, Linux, Docker CLI/daemon; standard-library host code.\n# Pier-native: Python >=3.12 REQUIRED; install requirements-pier.lock in a separate environment.\n')
        shutil.copy2(root / 'requirements-pier.lock', dataset / 'requirements-pier.lock')
        shutil.copy2(root / 'requirements-harbor-packaging.lock', dataset / 'requirements-harbor-packaging.lock')
        write(dataset / 'tests-calibration.json', {'version': delivery_calibration.VERSION,
              'suite_sha256': delivery_calibration.suite_digest(), 'cases': delivery_calibration.fixtures()+delivery_calibration.online_fixtures(),
              'scope': 'Synthetic minimal diagnostics, not scored pair results or per-pair checker certification'})
        environment = []
        for task_id in sorted({p['task_id'] for p in rows}):
            profile = delivery_runtime.read(project / 'data/tasks' / (task_id + '.json'))
            recipe = project / 'environments/scaleup' / task_id / 'Dockerfile'
            environment.append({'task_id': task_id, 'image': profile['image'],
                'resources': profile['environment'], 'local_image_identity': profile['image'].startswith('sha256:'),
                'build_recipe': recipe.relative_to(dataset).as_posix() if recipe.exists() else None})
        write(dataset / 'provenance/environment-requirements.json', {'tasks': environment,
            'policy': 'Images are not bundled. Pull pinned public images or rebuild supplied recipes. Record explicit --image-map overrides in the external work directory before freezing.'})
        completed = ['A1', 'A2', 'A3', 'A4', 'A5', 'A6', 'A7', 'A8', 'B1', 'B2', 'B3', 'B4', 'B5', 'B6']
        opening_states = {}; missing_blur = 0
        for pair in rows:
            folder = delivery_runtime.task_path(dataset, pair); ext = folder / 'tests/persona'; rule = by_id[pair['delivery_id']]
            write(ext / 'effective-binding.json', rule['effective_binding'])
            binding_hash = delivery_runtime.sha(ext / 'binding.json')
            opening_status = delivery_dataset.materialize_opening(root, folder, pair)
            opening_states[pair['delivery_id']] = opening_status
            if require_frozen_openings and opening_status != 'frozen_before_first_agent_action':
                raise ValueError('Complete first-message receipt is missing for ' + pair['delivery_id'])
            missing_blur += not (ext / 'receipts/blur/receipt.json').exists()
            write(ext / 'receipts/binding/source-identity.json', {'schema_version': 'deepswe-staged-rules-v1',
                'task_id': pair['task_id'], 'persona_id': pair['persona_id'], 'source_binding_sha256': binding_hash,
                'binding_path': '../../binding.json', 'paths_relative_to': 'this_file_parent',
                'source_index': 'provenance/previous-review-source-files.json', 'source_index_base': 'dataset',
                'semantic_approval_claimed': False})
            interaction = delivery_runtime.read(ext / 'interaction.json')
            interaction.update(opening_status=opening_status, opening_seed=delivery_runtime.read(ext/'legacy-plan.json')['opening'],
                complete_opening_path='../../instruction.md' if opening_status == 'frozen_before_first_agent_action' else None,
                complete_opening_frozen=opening_status == 'frozen_before_first_agent_action')
            if opening_status == 'frozen_before_first_agent_action': interaction['opening'] = (folder/'instruction.md').read_text()
            write(ext / 'interaction.json', interaction)
            review_file = project / 'data/delivery_reviews' / f'{pair["task_id"]}--{pair["persona_id"]}.json'
            delivery_metadata.enrich_pair(folder, pair, rule['rules'], opening_status,
                binding_review=delivery_runtime.read(review_file) if review_file.exists() else None)
            provenance = delivery_runtime.read(ext/'provenance.json')
            provenance.update(current_runtime_snapshot='runtime/source-index.json', current_runtime_snapshot_base='dataset',
                packaged_plan='tests/persona/legacy-plan.json', packaged_plan_base='task_directory')
            write(ext/'provenance.json',provenance)
            ux_rubric, ux_prompt, ux_schema = delivery_experience.rules(delivery_runtime.read(ext / 'persona.json'))
            write(ext / 'closed_loop/experience_rubric.json', ux_rubric)
            write(ext / 'closed_loop/experience_schema.json', ux_schema)
            (ext / 'closed_loop/experience_prompt.md').write_text(ux_prompt)
            write(ext / 'closed_loop/rubric.json', {'schema_version': 'deepswe-staged-rules-v2',
                'source_binding_sha256': binding_hash, 'source_binding_path': '../binding.json',
                'task_id': pair['task_id'], 'persona_id': pair['persona_id'], 'requirements': rule['rules'],
                'scope': 'online_rubric is the exact online judge rule; route/evidence_contract describe the separate final checker. Full effective disclosures and source satisfaction rubrics are preserved.'})
            for name in ('judge_prompt.md', 'simulator_prompt.md', 'response_schema.json'):
                shutil.copy2(common / name, ext / 'closed_loop' / name)
            metadata = {'pair_id': pair['delivery_id'], 'task_id': pair['task_id'], 'persona_id': pair['persona_id'],
                        'harbor_task_name': 'MatrAIx-ai/' + pair['delivery_id'],
                        'runner': 'run.py', 'batch_runner': 'run_batch.py', 'protocol': 'tests/persona/protocol.json'}
            config = task_identity((ext / 'original/task.toml').read_text(), metadata['harbor_task_name'])
            config += '\n[metadata.deepswe_persona]\n' + ''.join(f'{k} = {json.dumps(v)}\n' for k, v in metadata.items())
            (folder / 'task.toml').write_text(config)
            plan = delivery_runtime.read(ext / 'legacy-plan.json')
            write(ext / 'runtime-settings.json', delivery_timeouts.settings(folder, plan))
            (ext / 'configure_runtime.py').write_text('import sys\nfrom pathlib import Path\n'
                'sys.dont_write_bytecode = True\nroot = Path(__file__).resolve().parents[4]\n'
                'sys.path.insert(0, str(root/"runtime/project"))\n'
                'from dspilot.delivery_timeouts import main\n'
                f'if __name__ == "__main__": main(root, {pair["delivery_id"]!r})\n')
            delivery_metadata.write_task_readme(folder, pair)
            write(ext / 'protocol.json', {'schema_version': 'deepswe-staged-runtime-v1',
                'source_protocol': plan['protocol'], 'task_id': pair['task_id'], 'persona_id': pair['persona_id'],
                'runner': {'entrypoint': 'run.py', 'pair_id': pair['delivery_id'], 'runtime_choices': ['legacy', 'pier_native'],
                           'default_runtime': 'legacy', 'coding_backend': 'native_act', 'work_directory': 'external; fresh freeze required'},
                'budgets': plan['budgets'], 'response_schema': 'closed_loop/response_schema.json',
                'time_limits': 'runtime-settings.json', 'time_configuration_entrypoint': 'configure_runtime.py',
                'closed_loop': {'manifest': 'closed_loop/manifest.json', 'policy': 'closed_loop/policy.json',
                    'rubric': 'closed_loop/rubric.json', 'paths_relative_to': 'tests/persona'},
                'online_judge_protocol': shipped_judge['VERSION'],
                'quote_matching_policy': quote_policy,
                'checker': 'checker.py', 'checker_semantics': 'separate final DeepSWE programmatic or mixed checks',
                'calibration': {'entrypoint': 'calibrate_judge.py', 'suite': delivery_calibration.VERSION,
                    'batch_default': 'required_before_start', 'single_default': 'optional --calibrate',
                    'override': '--skip-calibration recorded in run-plan', 'per_pair_checker_certification': False}})
            write(ext / 'closed_loop/policy.json', {'schema_version': 'deepswe-staged-policy-v1',
                'source_protocol': plan['protocol'], 'controller': 'dspilot.staged.Controller',
                'stages': plan['stages'], 'requirements': plan['requirements'],
                'implementation_authorization': plan['implementation_authorization'], 'budgets': plan['budgets'],
                'online_verdicts': ['MET', 'PARTIAL', 'UNMET', 'UNOBSERVABLE'],
                'online_evidence': 'Exact event_id from same-run, same-pair agent user-visible messages after requirement disclosure; compare quote under quote_matching_policy',
                'quote_matching_policy': quote_policy,
                'quote_matching_audit': ('Preserve raw quote and source; record exact/normalized mode and original character offsets. Ignore Unicode case, whitespace, punctuation and Markdown backticks only; reject substantive content changes.'
                    if 'QUOTE_POLICY' in shipped_judge else 'Historical exact substring validation; do not reinterpret using a later matching policy.'),
                'controller_statuses': ['met', 'not_met', 'unknown', 'pending', 'unreached'],
                'controller_adapter': {'MET': 'met', 'PARTIAL': 'not_met', 'UNMET': 'not_met', 'UNOBSERVABLE': 'unknown'},
                'pending_scope': 'Lifecycle scheduling only; not a judge verdict',
                'source_scope_guard': {'source': 'task profile source_roots',
                    'evidence': 'Public committed candidate paths relative to pinned baseline, refreshed each interaction turn',
                    'on_violation': 'Block advance/finish and request correction within remaining budget; do not alter roots, candidate or original verifier automatically'},
                'simulator_message_repair': {'max_render_attempts': 2, 'max_reviews_per_render': 2,
                    'authority': 'Current host checks and receipt take precedence over historical quoted statuses; absent probe records do not establish non-execution',
                    'audit': 'Retain raw drafts and reviews; no automatic pass or fallback prose'},
                'final_checker_statuses': ['met', 'not_met', 'unknown'],
                'final_review': 'Independent final review; no reopening or feedback to coding agent',
                'full_execution_plan': '../legacy-plan.json'})
            (ext / 'checker.py').write_text('''import os, sys
from pathlib import Path
sys.dont_write_bytecode = True
here = Path(__file__).resolve()
dataset = next(p for p in here.parents if (p/'runtime/project/dspilot').is_dir())
sys.path.insert(0, str(dataset/'runtime/project'))
''' + f'os.environ.update(DS_TASK_ID={pair["task_id"]!r}, DS_PERSONA_ID={pair["persona_id"]!r}, DS_PROTOCOL="staged", DS_CODING_BACKEND="native_act", DS_RUNTIME="legacy")\n'
                + 'os.environ.pop("DS_STAGED_PLAN_PATH", None)\nfrom dspilot.delivery_checker import main\nif __name__ == "__main__": main()\n')
            (ext / 'closed_loop/README.md').write_text('DeepSWE staged rule package. Online requirement_judge uses four-level verdicts and validated post-disclosure visible message citations. Each online_rubric has stable <requirement_id>.contract criteria with the complete disclosure. Prompt files also include separate final checker builders; those use host evidence and legacy three-state results. checker.py --describe lists final checker inputs; no model is called unless --allow-llm is supplied. S1-S4 scheduling remains; this is not a claim of full TB3 sequential-loop compliance.\n')
            write(ext / 'closed_loop/manifest.json', {'schema_version': 'deepswe-staged-rules-v1',
                'protocol_version': plan['protocol'], 'protocol_path': '../protocol.json',
                'source_binding_sha256': binding_hash, 'source_binding_path': '../binding.json',
                'files': {p.name: delivery_runtime.sha(p) for p in sorted((ext / 'closed_loop').iterdir()) if p.is_file() and p.name != 'manifest.json'}})
            write(ext / 'delivery-status.json', {'completed': ['B1', 'B2', 'B3', 'B4', 'B5', 'B6'],
                'opening_status': opening_status, 'blur_receipt_present': (ext/'receipts/blur/receipt.json').exists(),
                'experience_rules_present': True, 'experience_scoring': 'optional; no score inferred from rules',
                'remaining': ([] if (ext/'receipts/blur/receipt.json').exists() else ['Historical task-only blur receipt unavailable']) +
                    ([] if opening_status == 'frozen_before_first_agent_action' else ['Complete opening not yet recorded']),
                'tb3_protocol_compliant': False, 'protocol': 'DeepSWE staged', 'independent_objective_checker': False})
        vendor = dataset / 'runtime/vendor/user_simulation'; vendor.mkdir(parents=True)
        (vendor/'__init__.py').write_text('''"""PersonaCodeBench's own user simulator compatibility entrypoint, not a third-party library."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'project'))
from dspilot.simulator_staged import render, propose, state_view, dialogue_style
''')
        (vendor/'README.md').write_text('This is the project-owned DeepSWE simulator adapter. The implementation is runtime/project/dspilot/simulator_staged.py. It is not a vendored third-party User Simulation library. The attached project license applies.\n')
        shutil.copy2(root.parent/'LICENSE' if (root.parent/'LICENSE').exists() else root/'LICENSE', vendor/'LICENSE')
        for name in ('inventory.json','registry.json'):
            index = delivery_runtime.read(dataset/name)
            index.update(task_path_base='dataset', trial_path_base='results_package')
            for row in index['pairs']: row['task_directory'] = 'tasks/' + row['delivery_id']
            write(dataset/name,index)
        # Ship only public templates/instructions, never the host credential file.
        for name in ('config/model-roles.env.example', 'docs/model-roles.md'):
            copy_source(runtime_source / name, dataset / name, records)
            copy_source(runtime_source / name, project / name, records)
        tests = dataset / 'tests'; tests.mkdir()
        for name in ('test_coding_budget.py', 'test_delivery_binding_review.py', 'test_delivery_timeouts.py', 'test_api.py', 'test_source_scope.py', 'test_simulator_state_scope.py', 'test_simulator_failure_history.py', 'test_pier_adapter.py', 'test_delivery_runtime.py', 'test_delivery_calibration.py',
                     'test_delivery_metadata.py', 'test_delivery_records.py', 'test_export_tb3_results.py', 'test_requirement_judge.py',
                     'test_delivery_capture.py', 'delivery_episode_fixture.py', 'staged_execution_fixture.py', 'staged_episode_fixture.py', 'staged_dialogue_input.py',
                     'fixtures/online_judge_failures_v14.json'):
            copy_source(runtime_source / 'tests' / name, tests / name, records)
            # The external work copy must itself support a later final export.
            copy_source(runtime_source / 'tests' / name, project / 'tests' / name, records)
        (tests / '__init__.py').write_text('')
        # A self-contained test entrypoint requires no package installation or caller PYTHONPATH.
        (dataset / 'test_release.py').write_text('''import os, sys, unittest
from pathlib import Path
sys.dont_write_bytecode = True
root = Path(__file__).resolve().parent
sys.path.insert(0, str(root/'runtime/project'))
os.environ['PYTHONPATH'] = str(root/'runtime/project')
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
if __name__ == '__main__':
    suite = unittest.defaultTestLoader.discover(str(root/'tests'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
''')
        write(dataset / 'tests/fixtures.json', {'pairs': [rows[0]], 'scope': 'Real frozen identity/plan, used for offline smoke validation; no fabricated scored result'})
        status = delivery_runtime.read(outer / 'delivery-status.json')
        status.update(purpose='deepswe_staged_execution_delivery', experiment_runnable=True,
            data_readable=True,
            execution_protocol='DeepSWE staged', completed_items=completed,
            live_execution_verified=False, official_harbor_validation='passed_local',
            online_calibration_verified=False, opening_states=opening_states,
            remaining=['Live calibration/execution must be checked separately; pending/missing opening receipts are explicitly recorded'],
            missing_item_counts={'historical_blur_receipt': missing_blur, 'optional_experience_rules': 0,
                'complete_frozen_opening': sum(v != 'frozen_before_first_agent_action' for v in opening_states.values())})
        status.pop('pair_gap_definitions', None); status.pop('global_gaps', None)
        write(outer / 'delivery-status.json', status)
        manifest = delivery_runtime.read(dataset / 'release-manifest.json')
        manifest.update(experiment_runnable=True, execution_protocol='DeepSWE staged', completed_items=completed,
                        release_name=output.name, live_execution_verified=False)
        write(dataset / 'release-manifest.json', manifest)
        delivery_metadata.enrich_release(dataset, rows, output.name)
        field_map = delivery_runtime.read(outer / 'schemas/field-map.json')
        field_map['mappings'].pop('closed_loop/rubric.requirements[].contract', None)
        field_map['mappings'].update({
            'evolved_requirements.requirements[]': 'Full original disclosure plus effective statement, source persona template, trigger, task dependencies, existing checker and artifact paths',
            'interaction.expectations': 'Exact persona interaction_preferences, or explicit not_recorded',
            'blurred_task.stage_summary': 'Explicit absence if no task-only stage summary was frozen; bound_stage_summary is separately labeled after-persona derivation',
            'receipts/binding/review.json': 'Source review fields only; export does not certify or upgrade semantic review',
            'release-manifest.task_entries': 'Per-pair identities, original sources and delivered file hashes',
            'persona-result.experience': 'Actual persona-experience status, satisfaction and result reference; missing and not_requested are distinct',
            'results/preflight.json': 'Links to captured execution preflight/calibration records; exporter does not execute them',
            'closed_loop/rubric.requirements[].effective_requirement.statement': 'Exact effective bound requirement; lifecycle.statement retains its staged plan wording',
            'instruction.md': 'Complete first user message when opening_status=frozen_before_first_agent_action; otherwise explicitly pending or historical seed',
            'task.toml': 'Unique Harbor task.name per pair plus metadata.deepswe_persona runner/pair/protocol entrypoint; original bytes kept in tests/persona/original/task.toml; all other settings preserved',
            'closed_loop/rubric.requirements': 'Effective runtime requirements, original persona satisfaction rubrics, lifecycle, checker routes and evidence contracts',
            'closed_loop/response_schema.json': 'DeepSWE response templates; dynamic enums/properties bound by the exact runtime prompt builders',
            'tests/persona/checker.py': 'Entry to existing mixed programmatic/LLM checks over trusted host evidence; no independent objective-oracle claim'})
        write(outer / 'schemas/field-map.json', field_map)
        (dataset / 'dataset.toml').write_text((dataset / 'dataset.toml').read_text().replace('experiment_runnable = false', 'experiment_runnable = true'))
        # Dataset task tree hashes and runtime archive hash are refreshed after all changes below.
        (dataset / 'README.md').write_text(readme(rows[0]['delivery_id']))
        (outer / 'README.md').write_text('DeepSWE Persona execution delivery. Enter dataset/ and follow README.md. Historical results/ are preserved separately.\n\n' + readme(rows[0]['delivery_id']))
        page = (outer / 'index.html').read_text().replace('尚不包含完整实验执行入口', '已补齐 DeepSWE 单题／批量运行入口；本版未执行在线实验')
        start = page.index('<h2>待补充内容</h2>'); end = page.index('<h2>Pair 与结果</h2>')
        page = page[:start] + '<h2>本版交付</h2><p>A1–A8、B1–B6 已实现，保留 DeepSWE staged 规则。Harbor 本地验证通过；在线校准尚未执行。<a href="dataset/README.md">运行说明</a>；<a href="delivery-status.json">剩余项</a>。</p>' + page[end:]
        (outer / 'index.html').write_text(page)
        for source, digest, target in records:
            if delivery_runtime.sha(source) != digest: raise ValueError('Runtime source changed during packaging')
        write(dataset / 'runtime/source-index.json', {'scope': 'Current executable project, newly frozen on recipient host; historical identities remain in results/',
              'files': {p.relative_to(project).as_posix(): delivery_runtime.sha(p) for p in sorted(project.rglob('*')) if p.is_file()}})
        # Retire the old copy index, whose paths described the superseded source-only snapshot.
        (dataset / 'provenance/source-files.json').rename(dataset / 'provenance/previous-review-source-files.json')
        with tarfile.open(dataset / 'runtime_bundle.tar.gz', 'w:gz') as archive:
            for name in ('runtime', 'prompts', 'tests', 'provenance', 'config', 'docs', 'inventory.json', 'registry.json', 'release-manifest.json',
                         'source-deepswe.json', 'tests-calibration.json', 'run.py', 'run_batch.py', 'calibrate_judge.py',
                         'validate_harbor.py', 'validate_release.py', 'configure_runtime.py', 'finalize_delivery.py', 'test_release.py', 'requirements.txt',
                         'requirements-pier.lock', 'requirements-harbor-packaging.lock'):
                archive.add(dataset / name, arcname=name)
        delivery_harbor.write_manifest(dataset, ['README.md', 'runtime_bundle.tar.gz'])
        official_report = delivery_harbor.validate(dataset)
        write(dataset / 'provenance/harbor-validation.json', official_report)
        seal(dataset); seal(outer)
        validation = delivery_runtime.verify_dataset(dataset)
        delivery_reader.validate(outer)
        if output.exists(): raise ValueError('Output appeared during packaging')
        outer.rename(output)
        return {**validation, 'output': str(output), 'completed': completed, 'live_execution_verified': False}
    finally:
        shutil.rmtree(temporary)


def readme(pair):
    return f'''# DeepSWE Persona runnable delivery

This release preserves the existing DeepSWE staged/Pivot rules. TB3 is a layout
example only. Historical results do not become results of a new protocol/runtime.

Configure the three model roles with [config/model-roles.env.example](config/model-roles.env.example).
Copy the template to a host-only file outside this frozen dataset, replace its
placeholder models/endpoints/keys, and pass --env-file /absolute/path/to/host.env
to run.py or run_batch.py. Agent, simulator and judge each have independent
model, endpoint, credentials and API protocol settings. See
[docs/model-roles.md](docs/model-roles.md) for details. Both public files are also
included in runtime_bundle.tar.gz; real host credential files are never bundled.

Each task includes tests/persona/runtime-settings.json and configure_runtime.py.
From that task directory, run `python -B tests/persona/configure_runtime.py --show`
to inspect limits, or supply --pier-agent-seconds, --interaction-seconds and/or
--final-review-seconds or --verifier-seconds with --output /path/to/new-release to create a new input
release. This uses the packaging dependencies and makes no model/Docker calls.
Only the selected pair changes; original task settings remain archived.
Pier-native verification defaults to at most 900 seconds and one attempt (no
timeout retry). Original tests and grading stay unchanged; overrides are recorded
in the actual task.toml and adapter-overrides.json. Cancellation/cleanup can take
additional time. Explicit Pier overrides require --runtime pier_native. Read the task
README for timer scope: a coding-segment timeout is not a whole-run deadline.

Metadata includes per-pair source/file hashes in release-manifest.json; full
persona basis, triggers, dependencies and existing artifact paths in
evolved_requirements.json; and explicit interaction preferences. Review records
remain not_certified unless actual source evidence establishes otherwise. A
missing task-only stage summary is explicit; bound stages are separately labeled
as derived after persona binding, never backdated into a blur receipt.

New cohorts can be prepared without historical runs using the source builder's
--selection manifest.json (pairs[] or jobs[], with task_id and persona_id).
There is no fixed 175-pair limit. Choose the intended 400 identities explicitly.
Task-only blur is generated in a fresh process without reading persona/binding
files. The method is a deterministic projection of the public task title; full
task constraints remain in the unchanged staged plan. This records construction
provenance, not semantic approval of blur quality.

A prepared input package has opening_status=pending_runtime_freeze. The current
simulator needs the actual initial workspace to compose the complete first message.
New frozen executions save that exact message and its hashes BEFORE the first
user delivery/coding action. A historical seed is never promoted to this status.
After a new Pivot batch, create a FINAL immutable delivery in the packaging env:

    python -B finalize_delivery.py --run /path/to/new-batch --output /path/to/final-delivery

This command reuses saved evidence and makes no model calls. It requires complete
opening receipts for every selected pair; pre-opening failures remain incomplete
and retain their separate result export. Differing first messages across attempts
must be delivered in separate dataset versions. The original prepared package and
all historical results remain unchanged.

Every task includes the three experience_* rule files. UX scoring is disabled by
default (persona-experience.json records not_requested with a null score); enable
it with run.py/run_batch.py --experience. This optional judge reads only visible
dialogue, runs after benchmark grading, and cannot alter progression or scores.
The runtime/vendor/user_simulation/ entrypoint exposes this project's own simulator
and includes its license; it is not a third-party User Simulation library.

For legacy/native_act use Python >=3.11 on Linux with Docker installed. Its host
execution needs only the Python standard library. Install in an isolated environment:

    python -m pip install -r requirements.txt

For Pier-native, Python >=3.12 is REQUIRED by pinned datacurve-pier 0.3.1
and harbor 0.5.0. Create a separate Python 3.12+ environment and install the pinned lock:

    python -m pip install -r requirements-pier.lock

Offline validation and tests (no Docker or model calls):

    python -B validate_release.py
    python -B test_release.py
    python -B run.py --pair {pair} --dry-run
    python -B run_batch.py --dry-run
    python -B calibrate_judge.py --dry-run

Historical results are an external, hash-bound package. inventory.json trial paths
are relative to the RESULT PACKAGE root, not the dataset directory. The combined
outer layout is dataset/ plus results/. When delivering dataset/ alone, provide the
result package separately and resolve it explicitly:

    python -B validate_release.py --results-dir /path/to/results
    python -B runtime/read_delivery.py --root . --results-dir /path/to/results

Without --results-dir, standalone validation reports external_not_provided, not
successful result resolution. Unexecuted input packages report not_run. Mismatched
packages or changed result artifacts fail identity/checksum validation. No parent
directory result links are embedded in the dataset.

Official Harbor local validation uses pinned, unmodified upstream modules at
dbd6dd045cbd7135c4a3994515df3907e3b9f0ea (0.23.0). Install the small validation
dependency lock in a SEPARATE Python >=3.12 environment, not the Pier environment:

    python -m pip install -r requirements-harbor-packaging.lock
    python -B validate_harbor.py --output /path/to/new-harbor-report.json

This checks official DatasetManifest/Task parsing, official Packager file selection
and digests for every pair, then rebuilds a dataset from only collected task files
and declared attachments, extracts the runtime and validates every plan offline.
It neither uploads to Harbor nor certifies live execution. The recorded report is
provenance/harbor-validation.json. Upstream overlapping-artifact warnings are
retained in that report; original artifact configuration is preserved.

The full delivery archive already contains local release locks. If instead you
download only the official Harbor task file set and declared attachments, place
tasks under tasks/<pair_id>/, extract runtime_bundle.tar.gz into the dataset root,
then run the following ONCE in the separate packaging environment:

    python -B validate_harbor.py --initialize-lock --output /path/to/new-reconstruction-report.json

This verifies official digests and the exact reconstructed file set before writing
new local locks and the provenance report. Existing locks are never replaced.
Then the ordinary validate_release.py, calibration and experiment runners work.

Configure host environment variables or a HOST credential file outside this release.
Set API_PROVIDER=openai with OPENAI_API_KEY (optional OPENAI_BASE_URL), or
API_PROVIDER=azure with AZURE_OPENAI_BASE_URL and AZURE_OPENAI_API_KEY.
Azure uses a /openai/v1 base and api-key; OpenAI uses Bearer authentication.
Set AGENT_MODEL, SIMULATOR_MODEL and JUDGE_MODEL independently; PCB_MODEL is
a shared fallback. API_MODE selects responses (default) or chat_completions.
Each role may override API_PROVIDER, API_BASE_URL, API_KEY, API_MODE and
REASONING_EFFORT with AGENT_, SIMULATOR_ or JUDGE_ prefixes.
Use REASONING_EFFORT=omit for models without that parameter; Chat-compatible
services may select MAX_TOKENS_FIELD=max_tokens. Required capabilities remain
strict JSON schema for simulator/judge and function calling for native_act.
Legacy API_BASE_URL/API_KEY settings retain Azure authentication.
The file is read as data, never sourced, copied into the dataset or mounted in
a container. Use matching role routes/models when comparing runtimes; changed
routes require a new output directory and fresh freeze/calibration.

Single execution (calls models and Docker):

    python -B run.py --pair {pair} --runtime legacy --env-file /path/to/host.env --output /path/to/new-run

Use --runtime pier_native after installing its lock. Default arm is pivot;
--arm baseline/control is also supported. Each output must be outside this dataset.
The runner copies a complete project, freezes a fresh plan with the chosen models
and runtime, checks Docker/images, then calls the existing staged execution engine.

Batch execution and resume (calls models/Docker only for not-yet-started pairs):

    python -B run_batch.py --runtime legacy --env-file /path/to/host.env --output /path/to/new-batch
    python -B run_batch.py --runtime legacy --env-file /path/to/host.env --output /path/to/new-batch --resume

Batch runs check Docker/images and require sixteen minimal model diagnostic cases
before starting a pair: six final-checker GroundedJudge cases plus ten visible-message
requirement judge cases (four verdict anchors and six wrapped-prose/multiline/bullet/code/Unicode/case
citation cases, with long event IDs). Runtime schemas
restrict event IDs to eligible messages. A decoded verbatim view and per-citation
diagnostics and lossless source-line slices guide copying without changing source text.
Each online judge allows at most one model-authored correction with medium reasoning effort;
all raw attempts and rejection reasons are retained. Under online protocol v3, citation matching ignores Unicode
case, whitespace/newlines, punctuation and Markdown backticks; it still requires exact
event IDs, post-disclosure eligible messages and contiguous substantive content.
Audits preserve the model quote, original source span/offsets and exact/normalized
match mode under a versioned policy. Matching alone does not prove semantic fulfilment. Historical v1/v2 runtimes retain
exact matching; consult each task protocol.json and policy.json for the shipped version. These do not certify all per-pair checkers. The gate
does not alter scoring or the staged protocol. A mismatch or API/schema error blocks
the batch. A passing receipt is reused only for the same release, suite, endpoint
and judge model. Failed/interrupted calibration is not retried automatically.
--skip-calibration explicitly opts out and is recorded in run-plan.json. Single
runs can opt in with --calibrate. Offline dry-run never calls a model.

Standalone online calibration (sixteen nominal judge calls, at most thirty-two
including corrections and transport retries, five-minute budget, no Docker):

    python -B calibrate_judge.py --env-file /path/to/host.env --output /path/to/new-calibration

calibration.json records identity, expected/observed verdicts, call counts and usage;
audit/ contains fixture inputs and actual reference-resolution records. Synthetic
fixtures are labeled. No online success is claimed until this command has passed.

Repeat --pair to select a subset. Resume requires the identical selection/runtime/
arm/attempt. Completed, failed and interrupted attempts are retained and skipped;
retry deliberately in a fresh output directory with --attempt 02. An execution
error never becomes a normal task failure. status.json/results.json index all
attempts, task-logs/ holds subprocess logs, project/runs/ holds raw evidence, and
delivery-results/ holds the standard result adapter output. --preflight-only checks
Docker and fresh freeze without model calls; use a separate output for the paid run.

delivery-results/ is the unified result delivery root: run-plan.json,
preflight.json, status.json, results.json, calibration/ (including audit/),
preflight/, task-logs/ and trials/. Only recorded files are exported; absent
preflight/calibration/logs are listed explicitly. provenance/execution/ preserves
the actual batch plan/status/source pins; run-plan.json links those files and
each trial's saved model/backend configuration. Export does not claim to have
performed the source calibration. The result's experience summary points to the
actual UX file, distinguishing not_requested, scored, unsupported,
infrastructure_error and missing/not_recorded. Known credential fields in the
additional execution sidecars are redacted; arbitrary log content still needs
review before external publication. Raw work files are not rewritten.

Container images are not bundled. See provenance/environment-requirements.json.
Pull the pinned public images before running. For local sha256-only image IDs,
rebuild the supplied runtime/project/environments/scaleup/<task>/Dockerfile and
provide --image-map /path/to/images.json containing {{"task_id":"rebuilt-image-id"}}.
Overrides affect only the external work copy and are recorded before fresh freeze.
Original verifier/environment source files remain preserved in each task folder.

Each task.toml uses a unique Harbor task.name per pair and adds metadata.deepswe_persona
pointing to this runner and pair ID; all other original settings remain unchanged.
The original task.toml is retained byte-for-byte. This is a runner-aware extension,
not a claim that a generic Harbor command automatically executes Persona.

Per-task tests/persona/checker.py --describe explains evidence inputs. For an actual
check pass --requirement ID --evidence /path/to/host-bundle.json. Input must contain
real trusted host snapshots and records. No tools or probes are launched by this
checker entrypoint. Missing semantic verification remains unknown without
--allow-llm --env-file /path/to/host.env. This reuses the existing mixed checker,
not a new independent objective acceptance oracle.

prompts/ and each closed_loop/ contain complete literal prompts plus their exact
executable builders, response-schema templates with runtime enum annotations,
and effective per-requirement rubrics/evidence contracts. New online judgments use
MET/PARTIAL/UNMET/UNOBSERVABLE and post-disclosure event_id + quote evidence.
Final checker results remain separate. Legacy results are never retroactively
converted to four-level verdicts. No v5 lineage, historical receipts or UX scores
are invented. S1-S4 scheduling remains; full TB3 sequential-loop compliance is not claimed.

Real online calibration and paid end-to-end validation on recipient infrastructure
must still be checked. Old runs may lack blur/opening receipts; see delivery-status.json.
The pinned Harbor packaging validator is separate from Pier's harbor==0.5.0
dependency; this work does not upgrade the pinned Pier runtime.
'''


def main():
    p = argparse.ArgumentParser(description=__doc__)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--batch-dir', type=Path); source.add_argument('--selection', type=Path, help='pairs[] or jobs[] manifest for a new, unexecuted cohort')
    p.add_argument('--results', type=Path)
    p.add_argument('--require-frozen-openings', action='store_true')
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    if a.batch_dir and not a.results: p.error('--batch-dir requires --results')
    print(json.dumps(package(ROOT, a.batch_dir, a.results, a.output, selection=a.selection,
        require_frozen_openings=a.require_frozen_openings), indent=2))


if __name__ == '__main__': main()
