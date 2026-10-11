"""Export a portable, unrescored one-pass checkpoint without model or Docker calls."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REVIEW_FILES = (
    'protocol.json', 'persona.json', 'original-persona.json', 'task.public.json',
    'applicability.json', 'episode.json', 'transcript.json', 'transcript.md',
    'dialogue-rating.json', 'official-check.json', 'release_check.json',
    'initial_baseline.json', 'evaluation/results.json', 'evaluation/runtime-summary.json',
)


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
        for prefix in (str(ROOT), ROOT.as_posix()):
            value = value.replace(prefix, '<workspace>')
    return value


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(portable(value), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def within(path):
    path = Path(path).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError('Source path is outside this repository')
    return path


def export(study, output):
    study, output = within(study), within(output)
    if output == study or output.is_relative_to(study) or output.exists():
        raise ValueError('Use a new output directory outside the source study')
    rows, summary, manifest = [read(study / name) for name in ('results.json', 'summary.json', 'manifest.json')]
    source_digests = {name: sha(study / name) for name in ('results.json', 'summary.json', 'manifest.json')}
    assert len(rows) == len({r['pair_id'] for r in rows}) == summary['processed_pairs']
    assert dict(Counter(r['status'] for r in rows)) == summary['counts']
    tasks = {t['key']: t for t in manifest['tasks']}
    forbidden = []
    env = ROOT / '.env'
    if env.exists():
        for line in env.read_text(encoding='utf-8-sig').splitlines():
            key, sep, value = line.partition('=')
            value = value.strip().strip('\"\'')
            if sep and len(value) >= 8 and any(x in key.upper() for x in ('KEY', 'TOKEN', 'PASSWORD', 'SECRET')):
                forbidden.append(value.encode())
    output.mkdir(parents=True)
    (output / '.gitattributes').write_text('* -text\n', encoding='utf-8')
    origins = {}

    def copy(source, target):
        source = within(source)
        if not source.is_file():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix == '.json':
            save(target, read(source))
        else:
            target.write_bytes(portable(source.read_bytes().decode('utf-8')).encode('utf-8'))
        origins[target.relative_to(output).as_posix()] = {
            'source': source.relative_to(ROOT).as_posix(), 'source_sha256': sha(source),
            'export_sha256': sha(target), 'transformation': 'JSON formatting and workspace-path projection only',
        }
        return True

    cohorts = {}
    for name in sorted({r['source_study'] for r in rows}):
        source = ROOT / '.local/multilang-roleplay' / name
        copy(source / 'manifest.json', output / 'provenance' / name / 'manifest.json')
        copy(source / 'online-budget.json', output / 'provenance' / name / 'online-budget.json')
        group = [r for r in rows if r['source_study'] == name]
        cohorts[name] = {'processed': len(group), 'counts': dict(Counter(r['status'] for r in group)),
            'original_task_passes': sum(r['base_task_success'] == 1 and r['status'] in ('completed', 'historical_rescored') for r in group),
            'source_protocols': sorted({r['source_protocol'] for r in group})}
    for key in sorted({r['pair_id'].split('/')[0] for r in rows}):
        task = tasks[key]
        path = study / task['path']
        assert sha(path) == task['sha256']
        data = json.loads(path.read_text(encoding='utf-8').strip())
        public = {k: data.get(k) for k in ('instance_id', 'repo', 'base_commit', 'problem_statement', 'language')}
        public.update(task_key=key, task_sha256=task['sha256'], dataset_revision=manifest['dataset_revision'])
        save(output / 'tasks' / key / 'source.json', public)
        for field in ('patch', 'test_patch'):
            text = data.get(field)
            if isinstance(text, str) and len(text) >= 32:
                forbidden += [text.encode(), json.dumps(text, ensure_ascii=False)[1:-1].encode()]
    copy(ROOT / 'human-interactive-coding-personas.v3.json', output / 'personas.json')
    review = []
    for row in rows:
        key, persona = row['pair_id'].split('/')
        src = within(row['source_result'])
        assert sha(src) == row['result_sha256'], row['pair_id']
        receipt = read(src)
        assert all(receipt.get(k) == row.get(k) for k in ('status', 'base_task_success', 'seconds', 'cap_seconds'))
        dest = output / 'records' / key / persona
        worker = src.parent / 'worker'
        copy(src, dest / 'result.json')
        copy(src.parent / 'started.json', dest / 'started.json')
        copy(src.parent / 'cleanup-recovery.json', dest / 'cleanup-recovery.json')
        for name in REVIEW_FILES:
            copy(worker / name, dest / 'evidence' / name)
        prediction = worker / 'predictions.json'
        if prediction.exists():
            original_patch = read(prediction)[tasks[key]['instance_id']]['model_patch']
            patchfile = dest / 'evidence/model.patch'
            patchfile.parent.mkdir(parents=True, exist_ok=True)
            patchfile.write_bytes(original_patch.encode('utf-8'))
            origins[patchfile.relative_to(output).as_posix()] = {'source': prediction.relative_to(ROOT).as_posix(),
                'source_sha256': sha(prediction), 'export_sha256': sha(patchfile),
                'transformation': 'Exact model_patch UTF-8 bytes extracted from predictions.json'}
        for path in sorted((worker / 'evaluation/patch-projection').glob('*')):
            if path.suffix in ('.json', '.diff'):
                copy(path, dest / 'evidence/evaluation/patch-projection' / path.name)
        if row['status'] == 'historical_rescored':
            historical = within(receipt['source_worker'])
            for name in ('protocol.json', 'persona.json', 'task.public.json', 'applicability.json', 'dialogue-rating.json'):
                copy(historical / name, dest / 'historical-generation' / name)
        if row['base_task_success'] is not None:
            report = read(worker / 'evaluation/results.json')
            assert row['base_task_success'] == int(report['success'] == 1), row['pair_id']
        record_path = dest.relative_to(output).as_posix()
        item = {k: v for k, v in row.items() if k != 'source_result'}
        item.update(instance_id=tasks[key]['instance_id'], language=tasks[key]['language'],
                    record=record_path + '/README.md', result=record_path + '/result.json',
                    transcript=record_path + '/evidence/transcript.md' if (dest / 'evidence/transcript.md').exists() else None,
                    joint_success=None, persona_measurement='Separate subjective dialogue ratings, when available')
        save(dest / 'provenance.json', item)
        links = [f'# {row["pair_id"]}', '', f'Original task: `{item["instance_id"]}`. Source: `{row["source_study"]}` / `{row["source_protocol"]}`.', '',
            f'Status: `{row["status"]}`. Original task correctness: `{row["base_task_success"]}`. Whole-sample seconds / cap: `{row["seconds"]}` / `{row["cap_seconds"]}`.', '',
            '[Result receipt](result.json) · [Provenance](provenance.json) · [Public task](../../../tasks/' + key + '/source.json)', '',
            'Available evidence (absence is not a passing result):', '']
        for path in sorted(dest.rglob('*')):
            if path.is_file() and path.name not in ('README.md', 'result.json', 'provenance.json'):
                rel = path.relative_to(dest).as_posix()
                links.append(f'- [{rel}]({rel})')
        (dest / 'README.md').write_text('\n'.join(links) + '\n', encoding='utf-8')
        review.append(item)
    successes = [r for r in review if r['base_task_success'] == 1 and r['status'] in ('completed', 'historical_rescored')]
    assert len(successes) == summary['successful_pairs']
    save(output / 'results.json', review)
    save(output / 'successful-cases.json', successes)
    save(output / 'summary.json', {**summary, 'unique_tasks_observed': len({r['instance_id'] for r in review}), 'cohorts': cohorts})
    table = ['# MultiLang checkpoint: all processed pairs', '', '[Overview](README.md) · [Summary](summary.json) · [Successful cases](successful-cases.json)', '',
        '| # | Task | Persona | Cohort | Status | Original pass | Seconds | Record |', '| ---: | --- | --- | --- | --- | ---: | ---: | --- |']
    for i, r in enumerate(review, 1):
        seconds = f'{r["seconds"]:.2f}' if r['seconds'] is not None else 'unknown'
        passed = 'unknown' if r['base_task_success'] is None else str(r['base_task_success'])
        table.append(f'| {i} | {r["instance_id"]} | {r["pair_id"].split("/")[1]} | {r["source_study"]} | {r["status"]} | {passed} | {seconds} | [record]({r["record"]}) |')
    (output / 'STATUS.md').write_text('\n'.join(table) + '\n', encoding='utf-8')
    (output / 'README.md').write_text(
        '# SWE-bench-Live MultiLang: 2026-10-01 checkpoint\n\n'
        f'{len(rows)} of {summary["candidate_pairs"]} task/persona pairs have been processed across {len({r["instance_id"] for r in review})} tasks. '
        f'{len(successes)} retained pairs passed the original task checks. This is an incomplete collection checkpoint, not a finalized benchmark result.\n\n'
        'Start with [STATUS.md](STATUS.md), then a record\'s result, public task, visible transcript, model patch and official report. '
        '[Machine-readable results](results.json), [summary by source cohort](summary.json), [successful-case index](successful-cases.json), '
        '[original ten-persona pool](personas.json) and [runner instructions](../../../scripts/swe_live_multilang/README.md) are included.\n\n'
        'All outcomes count toward one-pass coverage; timeouts, skips and errors remain visible and are excluded from the successful-case index. '
        'A completed worker is not necessarily a passing repair. Unknown correctness stays null. '
        'The 479 carried pairs retain their original cohort; 12 historical rescoring receipts measure evaluation time only. '
        'The one-pass collector does not retry, replay or regrade these records. Do not pool the differing cohorts into one protocol success rate.\n\n'
        'Current roleplay reuses the original ten identities and stable attributes, screens explicit task contradictions, and discloses the full issue before the second agent turn. '
        'Subjective dialogue ratings are separate from original-task correctness. There is no task-bound technical persona oracle or joint-success claim in this collection. '
        'This differs from the earlier 20-task evolved-requirement study and teammates\' task-bound protocols; review structure is aligned, measurement is not silently equated.\n\n'
        f'The next whole-sample cap at this snapshot is {summary["next_cap_seconds"]:.6f} seconds. '
        'Online P20 updates apply only to future eligible samples and do not prove exact global fastest-20% selection. '
        'The initial shared run used a 900-second external estimate; the one-pass continuation inherited 506.25 seconds. '
        'Stored per-sample caps and cohort timing states are authoritative.\n\n'
        'Roleplay v2 uses an evaluator-only patch projection that omits model changes to files touched by the official test patch. '
        'Original model patches and available projection receipts are included; hidden tests remain unchanged. This is an evaluator policy amendment from v1.\n\n'
        'The export contains only explicitly allowed public/review artifacts. Private dataset rows, gold/test patches, API request logs, agent thought history and credentials are excluded. '
        'Workspace paths in review JSON/text become `<workspace>`; numerical outcomes are unchanged. Model patches preserve their exact bytes. '
        'Source and exported hashes are in [provenance/artifacts.json](provenance/artifacts.json); [files.sha256.json](files.sha256.json) covers this package. '
        'Missing artifacts after errors/timeouts are retained as missing, not synthesized. Human semantic review is pending.\n', encoding='utf-8')
    assert source_digests == {name: sha(study / name) for name in source_digests}, 'Study changed during export; create another snapshot'
    save(output / 'provenance/artifacts.json', origins)
    save(output / 'provenance/export.json', {'created_utc': datetime.now(timezone.utc).isoformat(),
        'source_study': study.name, 'source_files': source_digests, 'exporter_sha256': sha(Path(__file__)),
        'policy': 'Snapshot only; no model calls, Docker jobs, retries or rescoring'})
    flagged = []
    for path in output.rglob('*'):
        if path.is_file():
            data = path.read_bytes()
            if any(marker in data for marker in forbidden):
                flagged.append(path.relative_to(output).as_posix())
    missing = [r[k] for r in review for k in ('record', 'result', 'transcript') if r[k] and not (output / r[k]).is_file()]
    check = {'passed': not (flagged or missing), 'processed_pairs': len(rows), 'successful_pairs': len(successes),
        'numeric_receipts_verified': len(rows), 'known_scores_verified_against_reports': sum(r['base_task_success'] is not None for r in rows),
        'flagged_files': flagged, 'missing_links': missing,
        'scope': 'Known literal credential/private-patch scan, source hashes, numerical preservation and index links; human review pending'}
    save(output / 'export-check.json', check)
    save(output / 'files.sha256.json', {p.relative_to(output).as_posix(): sha(p) for p in sorted(output.rglob('*')) if p.is_file()})
    if not check['passed']:
        raise ValueError('Export checks failed; inspect export-check.json before publication')
    return check


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('study', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    print(json.dumps(export(args.study, args.output), indent=2))
