"""Publish completed MultiLang trials in the shared TB3 layout, with full accounting.

No Docker/model calls, rescoring, or mutation of frozen experiment sources.
Incomplete trials stay local; their statuses remain in the public denominator.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / 'scripts/swe_live_multilang/tb3_adapter'
sys.path.insert(0, str(ADAPTER))
from package_release import package
from build_task import validate as validate_task

PACKAGE_NAME = 'multilang60'
RUN_NAME = 'collection-20261003'
WORKER_FILES = {
    'persona-result.json': 'persona-result.json',
    'persona-check.json': 'persona-check.json',
    'agent-private/episode.json': 'agent-private/episode.json',
    'transcript.json': 'agent/transcript.json',
    'transcript.md': 'agent/transcript.md',
    'protocol.json': 'provenance/protocol.json',
    'task.public.json': 'provenance/task.public.json',
    'persona.json': 'provenance/persona.json',
    'applicability.json': 'provenance/applicability.json',
    'gold-proof.json': 'provenance/gold-proof.json',
    'initial_baseline.json': 'provenance/initial_baseline.json',
    'release_check.json': 'provenance/release_check.json',
    'evaluation/results.json': 'verifier/results.json',
    'evaluation/runtime-summary.json': 'verifier/runtime-summary.json',
}


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def portable(value):
    if isinstance(value, dict):
        return {portable(k): portable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [portable(v) for v in value]
    if isinstance(value, str):
        return value.replace(str(ROOT), '<workspace>').replace(ROOT.as_posix(), '<workspace>')
    return value


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Some upstream test names contain lone UTF-16 surrogates. Preserve their
    # JSON value as a \uXXXX escape instead of dropping or replacing the value.
    path.write_text(json.dumps(portable(value), ensure_ascii=False, indent=2)+'\n',
                    encoding='utf-8', errors='backslashreplace')


def text(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding='utf-8')


def export(study, output, staging):
    study, output, staging = [p.resolve() for p in (study, output, staging)]
    if any(not p.is_relative_to(ROOT) for p in (study, output, staging)):
        raise ValueError('All paths must be inside this workspace')
    if output.exists() or staging.exists() or output.is_relative_to(study) or staging.is_relative_to(study):
        raise ValueError('Use new output/staging directories outside the source experiment')
    rows, plan, status = [read(study/n) for n in ('results.json', 'run-plan.json', 'status.json')]
    source_digests = {n: sha(study/n) for n in ('results.json', 'run-plan.json', 'status.json', 'task-results.json')}
    changed = [n for n, h in plan['sources'].items() if sha(ROOT/n) != h]
    if changed:
        raise ValueError('Measured runtime has changed: '+str(changed))
    selected = [r for r in rows if r['status'] == 'completed']
    if len(rows) != 1975 or len(selected) != 60 or sum(r['joint_success'] == 1 for r in selected) != 30:
        raise ValueError('This dated snapshot expects 1,975 records and 60 completed trials, 30 passing')
    output.mkdir(parents=True); staging.mkdir(parents=True)
    text(output/'.gitattributes', '* -text\n')
    origins = {}

    def copy(source, target, exact=False):
        source = source.resolve()
        if not source.is_relative_to(ROOT):
            raise ValueError('Source artifact outside workspace')
        if not source.exists():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        if exact:
            shutil.copyfile(source, target)
        elif source.suffix == '.json':
            save(target, read(source))
        else:
            text(target, portable(source.read_text(encoding='utf-8')))
        origins[target.relative_to(output).as_posix()] = {
            'source': source.relative_to(ROOT).as_posix(), 'source_sha256': sha(source),
            'export_sha256': sha(target),
            'transformation': 'exact bytes' if exact else 'JSON formatting/workspace-path projection only'}
        return True

    # All bindings were frozen before execution. Never rebuild/relabel their validation receipts.
    for row in selected:
        name = row['task_key']+'--'+row['persona']
        source = study/'release-candidates/tasks'/name
        validate_task(source)
        shutil.copytree(source, staging/name)
    bundle = output/'release-candidates'/PACKAGE_NAME
    packaging = package(staging, bundle)
    print('Frozen bundle: '+json.dumps(packaging), flush=True)
    archive = bundle.with_suffix('.tar.gz')
    with tarfile.open(archive, 'w:gz') as tar:
        tar.add(bundle, arcname=bundle.name)
    text(Path(str(archive)+'.sha256'), sha(archive)+'  '+archive.name+'\n')
    copy(bundle/'inventory.json', output/'release-candidates/all-completed.json', exact=True)

    run = output/'runs'/RUN_NAME
    copy(study/'run-plan.json', run/'run-plan.json')
    copy(study/'status.json', run/'status.json')
    copy(study/'task-results.json', run/'task-results.json')
    copy(study/'online-budget.json', run/'online-budget.json')
    copy(study/'calibration/calibration.json', run/'calibration/calibration.json')
    for source in (study/'calibration/audit').glob('*.json'):
        copy(source, run/'calibration/audit'/source.name)
    preflight = []
    for source in sorted((study/'preflight').glob('*/result.json')):
        receipt = read(source)
        preflight.append({'task_key': source.parent.name, 'status': receipt['status'],
            'seconds': receipt.get('seconds'), 'cap_seconds': receipt.get('cap_seconds'),
            'source_sha256': sha(source)})
    save(run/'preflight.json', {'counts': dict(Counter(r['status'] for r in preflight)), 'tasks': preflight,
        'scope': 'Per-task image/gold preparation; not agent task outcomes'})
    index, complete_index = [], []
    for row in rows:
        src = Path(row['result']); receipt = read(src)
        if not src.resolve().is_relative_to(study):
            raise ValueError('Receipt is not from this study')
        fields = ('status', 'base_task_success', 'joint_success', 'seconds', 'cap_seconds')
        if any(receipt.get(k) != row.get(k) for k in fields):
            raise ValueError('Numerical receipt mismatch')
        key, persona = row['task_key'], row['persona']
        item = {k: row.get(k) for k in ('task_key', 'instance_id', 'persona', 'attempt', *fields)}
        item.update(source_receipt_sha256=sha(src), detail_included=row['status']=='completed', trial=None,
                    detail_omission=None if row['status']=='completed' else 'Incomplete/non-evaluable: counts and status retained; raw details remain local')
        if row['status'] != 'completed':
            index.append(item); continue
        dest = run/'trials'/key/f'{row["attempt"]:02d}-{persona}';worker = src.parent/'worker'
        for name, target in WORKER_FILES.items():
            if not copy(worker/name, dest/target):
                raise ValueError('Completed trial missing required artifact: '+str(worker/name))
        copy(src, dest/'provenance/supervisor-result.json')
        copy(src.parent/'started.json', dest/'provenance/started.json')
        copy(src.parent/'worker.log', run/'task-logs'/f'{key}--{row["attempt"]:02d}-{persona}.log')
        # Include the actual visible-only judge payloads, never raw coding-agent model context.
        for source in (worker/'agent-private/requirement-judge-audit').glob('*.json'):
            copy(source, dest/'agent-private/requirement-judge-audit'/source.name)
        calls = []
        source = worker/'agent-private/audit/llm_calls.jsonl'
        for line in source.read_text(encoding='utf-8').splitlines():
            call = json.loads(line)
            calls.append({k: call.get(k) for k in ('ts','role','model','provider','attempt','latency_sec','input_tokens','output_tokens')})
        save(dest/'agent-private/audit/call-metadata.json', {'source_sha256': sha(source), 'calls': calls,
            'omitted': 'Raw provider messages/output_text and agent internal context; requirement judge inputs/outputs are included separately'})
        patch = read(worker/'predictions.json')[row['instance_id']]['model_patch']
        patchfile = dest/'agent/model.patch';patchfile.write_bytes(patch.encode('utf-8'))
        origins[patchfile.relative_to(output).as_posix()] = {'source': (worker/'predictions.json').relative_to(ROOT).as_posix(),
            'source_sha256': sha(worker/'predictions.json'), 'export_sha256': sha(patchfile), 'transformation': 'Exact model_patch UTF-8 bytes'}
        for source in (worker/'evaluation/patch-projection').glob('*'):
            if source.suffix in ('.json','.diff'):
                copy(source, dest/'verifier/patch-projection'/source.name, exact=source.suffix=='.diff')
        for name in ('report.json','status.json'):
            copy(worker/'evaluation'/row['instance_id']/name, dest/'verifier'/name)
        result, checks, episode, official = [read(worker/n) for n in ('persona-result.json','persona-check.json','agent-private/episode.json','evaluation/results.json')]
        if result['original_reward'] != row['base_task_success'] or int(official['success']==1) != row['base_task_success']:
            raise ValueError('Original verifier outcome mismatch')
        if int(result['benchmark_outcome']=='pass') != row['joint_success'] or row['seconds'] > row['cap_seconds']:
            raise ValueError('Joint result/deadline mismatch')
        if row['joint_success']==1 and (not all(c['verdict']=='pass' for c in checks['checks']) or not episode['simulated_user_satisfied']):
            raise ValueError('Passing trial lacks independent acceptance')
        item.update(trial=dest.relative_to(run).as_posix(), benchmark_outcome=result['benchmark_outcome'],
                    binding='tasks/'+key+'--'+persona)
        save(dest/'provenance/delivery.json', {'dataset_task': item['binding'], 'protocol': result['protocol'],
            'source_receipt_sha256': sha(src), 'scope': 'Visible dialogue, online judgments, original verifier and bounded handoff checker; no new evaluation'})
        text(dest/'README.md', f'# {row["instance_id"]} / {persona}\n\n'
            f'Outcome: **{result["benchmark_outcome"]}**. Original reward: {result["original_reward"]}. '
            f'Whole sample: {row["seconds"]:.3f}s / {row["cap_seconds"]:.3f}s.\n\n'
            '[Result](persona-result.json) · [Checker](persona-check.json) · [Visible conversation](agent/transcript.md) · '
            '[Candidate patch](agent/model.patch) · [Episode](agent-private/episode.json) · [Original verification](verifier/results.json)\n')
        index.append(item);complete_index.append(item)
    save(run/'results.json', index)
    save(output/'completed-cases.json', complete_index)
    save(output/'successful-cases.json', [r for r in complete_index if r['joint_success']==1])
    legacy = ROOT/'.local/multilang-tb3-legacy-774'
    copy(legacy/'status.json', output/'legacy/summary.json')
    historical = read(legacy/'results.json')
    for record in historical:
        key=record['result'].split('/')[1]
        record['local_migration_result']=record.pop('result')
        record['legacy_evidence']='../../fullset-checkpoint-20261001/records/'+key+'/'+record['persona']+'/README.md'
    save(output/'legacy/results.json', { 'scope': 'Legacy layout index; new TB3 judge/checker stages not executed',
        'original_evidence': '../../fullset-checkpoint-20261001/', 'results': historical})
    copy(ROOT/'.local/tb3-final-inspection-20261003.json', output/'provenance/completion-audit.json')
    spec = subprocess.check_output(['git','show','origin/main:TB3_DELIVERY_FORMAT.md'])
    (output/'provenance/TB3_DELIVERY_FORMAT.md').write_bytes(spec)
    spec_revision = subprocess.check_output(['git','rev-parse','origin/main']).decode().strip()
    save(output/'provenance/team-comparison.json', {
        'spec_revision': spec_revision, 'spec_sha256': hashlib.sha256(spec).hexdigest(),
        'python_only_peer': {'pr': 'https://github.com/MatrAIx-ai/PersonaCodeBench/pull/7',
            'commit': '8c7d907870306625ca49e5589c972644a47fe9e5',
            'file': 'personalized_coding_agent_interactive_eval/paper_material/swebench_live_python_only_persona_pilot/cases/python-babel__babel-1141/evaluation.json',
            'observed': 'progressive_persona.official_resolved=false; failed case evidence is committed'},
        'policy': 'Include complete pass AND fail evidence; retain all other outcomes in denominator/index without their raw logs'})
    counts = dict(Counter(r['status'] for r in rows))
    save(output/'summary.json', {'source_status': status, 'record_counts': counts,
        'detailed_trials': len(selected), 'detailed_unique_tasks': len({r['task_key'] for r in selected}),
        'detailed_personas': len({r['persona'] for r in selected}), 'benchmark_passes': 30, 'benchmark_failures': 30,
        'tasks_passing_preflight': sum(r['status']=='completed' for r in preflight),
        'tasks_failing_preflight': sum(r['status']!='completed' for r in preflight),
        'complete_only_rate': 30/60, 'including_sample_timeouts_rate': 30/(60+counts['timeout']),
        'rate_scope': 'Conditional observed-record rates; no full-dataset, exact global P20, causal persona-effect, or technical-scenario claim',
        'payload_selection': 'Only 60 completed trials have detailed evidence; all 1,975 new records retain original statuses'})
    lines = ['# Completed MultiLang trials', '', '[Overview](README.md) · [All-record index](runs/'+RUN_NAME+'/results.json)', '',
        '| Task | Persona | Attempt | Outcome | Seconds / cap | Evidence |', '| --- | --- | ---: | --- | ---: | --- |']
    for r in complete_index:
        lines.append(f'| {r["instance_id"]} | {r["persona"]} | {r["attempt"]} | {r["benchmark_outcome"]} | {r["seconds"]:.2f} / {r["cap_seconds"]:.2f} | [trial](runs/{RUN_NAME}/{r["trial"]}/README.md) |')
    text(output/'CASES.md', '\n'.join(lines)+'\n')
    text(output/'README.md', overview())
    shutil.copyfile(ROOT/'scripts/swe_live_multilang/validate_tb3_delivery.py', output/'validate_delivery.py')
    save(output/'provenance/artifacts.json', origins)
    save(output/'provenance/export.json', {'created_utc': datetime.now(timezone.utc).isoformat(),
        'source_study': study.name, 'source_files': source_digests, 'exporter_sha256': sha(Path(__file__)),
        'measured_sources_verified': plan['sources'], 'selection': 'status == completed, independent of pass/fail',
        'archive_sha256': sha(archive), 'policy': 'No model calls, Docker jobs, retries or rescoring; source evidence preserved'})
    if source_digests != {n: sha(study/n) for n in source_digests}:
        raise ValueError('Experiment changed while exporting')
    secret_values=[]
    for line in (ROOT/'.env').read_text(encoding='utf-8-sig').splitlines():
        key, sep, value=line.partition('=');value=value.strip().strip('\"\'')
        if sep and len(value)>=8 and any(k in key.upper() for k in ('KEY','TOKEN','PASSWORD','SECRET')):
            secret_values.append(value.encode())
    flagged=[p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file()
             and any(value in p.read_bytes() for value in secret_values)]
    if flagged:
        raise ValueError('Credential matches in '+str(flagged))
    save(output/'export-check.json', {'passed': True, 'records': len(rows), 'detailed_trials': len(selected),
        'original_scores_crosschecked': len(selected), 'frozen_task_bundles_validated': len(selected),
        'known_credential_matches': [], 'measured_sources_unchanged': True,
        'scope': 'Format/hash/numeric/privacy checks, not independent reruns or full semantic validation'})
    files={p.relative_to(output).as_posix():sha(p) for p in sorted(output.rglob('*')) if p.is_file()}
    save(output/'files.sha256.json', files)
    text(output/'SHA256SUMS', ''.join(h+'  '+n+'\n' for n,h in files.items()))
    return {'output': str(output), 'files': len(files), 'records': len(rows), 'detailed_trials': len(selected), 'archive_sha256': sha(archive)}


def overview():
    return '''# SWE-bench-Live MultiLang: TB3-format delivery, 2026-10-03

This delivery includes **60 completed task/persona trials across 45 distinct original tasks and all ten v3 personas: 30 pass and 30 fail**. Frozen tasks/runtime are separate from execution outputs, following [the team delivery format](provenance/TB3_DELIVERY_FORMAT.md). The protocol is `swe-live-tb3-handoff-loop-v1`, a native SWE adapter with one bounded communication-format requirement per persona; it is not TB3 v6 or a validation of every persona's technical scenario.

Start with [the case table](CASES.md), [summary](summary.json), [completed cases](completed-cases.json), or [30 passing cases](successful-cases.json). A completed trial is retained regardless of outcome, matching the Python-only pilot's inclusion of unsuccessful repairs. [Peer evidence and the selection rule](provenance/team-comparison.json) are recorded.

## Layout and reproduction

- `release-candidates/multilang60/`: frozen Harbor tasks, complete per-task `tests/persona/closed_loop/`, native verifier inputs, original test/solution patches, runtime, prompts, entry points, inventory, dataset manifest and SHA locks.
- The adjacent `.tar.gz` and `.tar.gz.sha256` are the portable archive and checksum. `all-completed.json` indexes the 60 bindings; 60 bindings are **45 distinct source tasks**, not 60 different questions.
- `runs/collection-20261003/`: frozen run plan, preparation summary, all-record index, original status/timing state, six real calibration controls and 60 detailed trial directories.
- Each detailed trial includes separate persona result/checker files, visible dialogue, exact candidate patch, private structured episode, requirement judge payloads/results, call metadata and original verifier evidence. Raw coding-agent request contexts and unused provider output are excluded. Workspace paths in review copies become `<workspace>`; frozen task bytes and model patch bytes remain unchanged. Source/export hashes record transformations.
- `legacy/` indexes the previous 774 records (78 tasks, 61 original-task passes among 153 known scores). Their complete historical review package remains [here](../fullset-checkpoint-20261001/README.md). Missing new-protocol stages remain unexecuted; historical scores are not pooled with this protocol.

From this directory, `python validate_delivery.py` checks payload hashes, indexes and verdict consistency. For the standalone dataset, verify the archive checksum, extract it, install its requirements, run `python validate_release.py`, and use `python run.py --task <inventory-id> --dry-run`. Live reproduction uses its companion `run.py`/`run_batch.py`; plain `harbor run` does not enable this simulator/native verifier. Configure credentials only on the host. Never mount `tests/` or `solution/` into the coding agent. The bundled task inputs include evaluator-only upstream materials by design.

## Coverage and exclusions

The queue ended at **2026-10-03 03:51 America/Chicago**. It marked 1,077 source tasks handled: 78 historical tasks and 999 new tasks. The new queue produced **1,975 records: 60 completed, 142 sample timeouts, 1,760 infrastructure-invalid, 12 evaluation-invalid and one not-applicable**. Every record remains in `runs/collection-20261003/results.json`, with explicit `detail_included`/omission fields. Incomplete trial directories and their raw failure logs are not published. No skipped record is relabelled a model failure or success.

Only **119 of 999 new tasks passed image/gold preparation**; 880 did not. Preparation statuses were 119 completed, 164 timeout, 715 evaluation-invalid and one interrupted. Late-run Docker logs show storage exhaustion; 666 preparation records end with local image-not-found errors after attempted pulls. The worker also recorded six base-commit mismatches and six container-start disk-space failures. The runner continued after global infrastructure failure, so queue completion does **not** establish a complete valid dataset evaluation. The historical runner is preserved as executed; no disk-recovery or retry fix is claimed here.

30/60 = 50% is the completed-record pass rate. Including the 142 scored sample timeouts gives 30/202 = 14.85%. These conditional rates exclude infrastructure/protocol invalidity and inapplicability. Selection of faster tasks, randomized fallback and infrastructure loss prevent interpreting either as full-dataset performance or a causal persona effect.

## Protocol and timing boundaries

Each task draws one random persona from the original ten; a non-pass permits one distinct fallback, then the task ends. There were 23 first-attempt passes and seven fallback passes. Six live judge calibration controls passed before collection. The whole-sample clock includes applicability, agent/simulator/judge work, snapshots and independent final verification. It never resets between turns. Online censored P20 began at 212.684575 seconds and ended at 153.154008 seconds; there is no global deadline or claim of exact global fastest-20% selection. Image/gold preparation is separate, but this implementation bounded it by the same current cap, contributing to preparation loss.

Turn 1 uses a frozen persona-independent opening. Turn 2 discloses the original issue and the full R1 handoff contract. Online MET requires qualified post-disclosure visible evidence and final recheck. Original correctness and the independent structural checker remain separate. A pass requires both objective checks, final online MET and user satisfaction. Optional UX scoring was not executed. Resource, security, incident-recovery and race-condition templates are **not** covered by this communication-format checker.

The native verifier retains the recorded v2 projection that removes candidate edits to files touched by the official test patch. Original candidate patches and projection evidence are included; this is an explicit evaluator-policy difference from v1. The original native SWE dataset has no Harbor task.toml: the saved original configuration is labelled an adapter projection, not an upstream file. Project-native MIT simulation code is bundled rather than the TB3 author's unpublished User Simulation library.

## Validation

All 60 official scores match their retained verifier summaries; candidate patches and frozen task bindings are preserved. The measured source fingerprints remain unchanged. The official Harbor Packager checks collection and independent task reassembly. The delivery validator checks all record totals and detailed pass/fail artifacts; archive extraction and the bundled offline tests are checked separately. No new model episodes or original-task evaluations are run for this export. These checks do not replace human semantic review or imply paper-ready full technical-persona coverage.
'''


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--staging', type=Path, required=True)
    args=parser.parse_args()
    print(json.dumps(export(args.study,args.output,args.staging),indent=2))
