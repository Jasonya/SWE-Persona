"""Portable, standard-library reader for offline DeepSWE delivery adapters.

This validates the documented adapter contract, not the official TB3/Harbor schema.
It never imports the experiment runtime or starts a model/container.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile
import tomllib

VERSION = 'deepswe-delivery-adapter-v1'


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def local(root, relative):
    p = PurePosixPath(relative)
    if not relative or p.is_absolute() or '..' in p.parts or '\\' in relative:
        raise ValueError('Unsafe relative path: ' + relative)
    path = root.joinpath(*p.parts)
    if any(x.is_symlink() for x in (path, *path.parents)) or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Unsafe package path: ' + relative)
    return path


def read(root, relative):
    return json.loads(local(root, relative).read_text())


def inventory(root):
    standalone = (root / 'inventory.json').is_file()
    value = read(root, 'inventory.json' if standalone else 'dataset/inventory.json')
    if value['schema_version'] != VERSION:
        raise ValueError('Unsupported delivery schema')
    rows = value['pairs']
    if value.get('trial_path_base') == 'results_package' and not standalone:
        rows = [{**p, 'trials': [{**t, 'trial_directory': 'results/' + t['trial_directory']}
                                for t in p.get('trials', [])]} for p in rows]
    if value.get('task_path_base') == 'dataset':
        return [{**p, 'task_directory': ('' if standalone else 'dataset/') + p['task_directory']} for p in rows]
    if standalone:
        return [{**p, 'task_directory': p['task_directory'].removeprefix('dataset/')} for p in rows]
    return rows


def result_links(root, results_dir=None):
    """Resolve a bound external result package without guessing parent paths."""
    root = Path(root).resolve()
    dataset = root / 'dataset' if (root / 'dataset/inventory.json').is_file() else root
    binding = read(dataset, 'release-manifest.json').get('results_reference')
    if binding is None:
        return {'status': 'legacy_layout', 'resolved': False}
    rows = read(dataset, 'inventory.json')['pairs']
    trials = [(p, t) for p in rows for t in p.get('trials', [])]
    if binding['status'] == 'not_run':
        if trials: raise ValueError('Unexecuted dataset contains trial links')
        return {'status': 'not_run', 'resolved': True, 'trials': 0}
    if results_dir is None and dataset != root:
        results_dir = root / 'results'
    if results_dir is None:
        return {'status': 'external_not_provided', 'resolved': False, 'trials': len(trials),
                'instruction': 'Supply --results-dir /path/to/results; results are separate from the frozen dataset.'}
    results = Path(results_dir).resolve()
    for name, key in [('results.json', 'index_sha256'), ('export-manifest.json', 'manifest_sha256')]:
        if digest(local(results, name)) != binding[key]:
            raise ValueError('External result package identity mismatch: ' + name)
    for name, expected in read(results, 'export-manifest.json')['files'].items():
        if digest(local(results, name)) != expected:
            raise ValueError('External result artifact checksum mismatch: ' + name)
    entries = read(results, 'results.json')['entries']
    by_run = {e['run_id']: e for e in entries}
    if len(by_run) != len(entries) or sorted(t['run_id'] for _, t in trials) != sorted(by_run):
        raise ValueError('External result run set mismatch')
    for pair, trial in trials:
        entry = by_run[trial['run_id']]
        if (any(pair[k] != entry[k] for k in ('task_id', 'persona_id'))
                or trial['arm'] != entry['arm'] or trial['trial_directory'] != entry['trial_directory']):
            raise ValueError('External result pair or directory mismatch')
        if not local(results, trial['trial_directory']).is_dir():
            raise ValueError('Missing external trial directory')
    return {'status': 'resolved', 'resolved': True, 'trials': len(trials), 'results_dir': str(results)}


def validate(root, results_dir=None):
    root = Path(root).resolve()
    lock = read(root, 'release-lock.json')
    expected = lock['files']
    paths = list(root.rglob('*'))
    if any(p.is_symlink() for p in paths):
        raise ValueError('Symlinks are not permitted')
    actual = {p.relative_to(root).as_posix() for p in paths if p.is_file()}
    if actual != set(expected) | {'release-lock.json', 'SHA256SUMS'}:
        raise ValueError('Package file inventory differs from release lock')
    for name, h in expected.items():
        if digest(local(root, name)) != h:
            raise ValueError('Checksum mismatch: ' + name)
    sums = ''.join(f'{h}  {name}\n' for name, h in sorted({
        **expected, 'release-lock.json': digest(root / 'release-lock.json')}.items()))
    if (root / 'SHA256SUMS').read_text() != sums:
        raise ValueError('SHA256SUMS differs from release lock')
    for path in paths:
        if path.suffix == '.json':
            json.loads(path.read_text())
        elif path.suffix == '.jsonl':
            for line in path.read_text().splitlines():
                if line.strip():
                    json.loads(line)
        elif path.suffix == '.toml':
            tomllib.loads(path.read_text())
    if (root / 'inventory.json').is_file():
        return {'readable': True, 'adapter_validation': 'passed', 'files_verified': len(expected),
                'scope': 'Standalone dataset checksums and external result links',
                'results': result_links(root, results_dir)}
    links = result_links(root, results_dir)
    pairs = inventory(root)
    ids = [p['delivery_id'] for p in pairs]
    identities = [(p['task_id'], p['persona_id']) for p in pairs]
    if len(set(ids)) != len(ids) or len(set(identities)) != len(identities):
        raise ValueError('Pair identity collision')
    results = read(root, 'results/results.json')['entries']
    by_run = {e['run_id']: e for e in results}
    if len(by_run) != len(results):
        raise ValueError('Duplicate result run ID')
    linked = []
    requirement_count = 0
    for pair in pairs:
        base = pair['task_directory'] + '/tests/persona/'
        persona = read(root, base + 'persona.json')
        binding = read(root, base + 'binding.json')
        requirements = read(root, base + 'evolved_requirements.json')['requirements']
        req_ids = [r['requirement_id'] for r in requirements]
        requirement_count += len(req_ids)
        if len(req_ids) != len(set(req_ids)):
            raise ValueError('Duplicate requirement ID')
        if persona['id'] != pair['persona_id'] or any(binding[k] != pair[k] for k in ('task_id', 'persona_id')):
            raise ValueError('Dataset pair identity mismatch')
        provenance = read(root, base + 'provenance.json')
        for name, h in provenance['original_files'].items():
            target = base + 'original/' + name if name in ('instruction.md', 'task.toml') else pair['task_directory'] + '/' + name
            if digest(local(root, target)) != h:
                raise ValueError('Original task changed: ' + target)
        for trial in pair['trials']:
            entry = by_run[trial['run_id']]
            linked.append(entry['run_id'])
            if any(entry[k] != pair[k] for k in ('task_id', 'persona_id')) or trial['arm'] != entry['arm']:
                raise ValueError('Result-to-dataset identity mismatch')
            if trial['trial_directory'] != 'results/' + entry['trial_directory']:
                raise ValueError('Result directory link mismatch')
            for field in ('persona_result', 'persona_check', 'episode', 'trace_index'):
                if field in entry:
                    value = read(root, 'results/' + entry[field])
                    if any(value[k] != entry[k] for k in ('task_id', 'persona_id', 'arm', 'run_id')):
                        raise ValueError('Result document identity mismatch')
                    if field == 'persona_result':
                        if value['compatibility']['tb3_protocol_executed']:
                            raise ValueError('Historical result mislabeled as TB3')
                        if value.get('schema_version') in ('deepswe-result-adapter-v2', 'deepswe-result-adapter-v3'):
                            assessment = value.get('benchmark_assessment', {})
                            if (assessment.get('policy') != 'deepswe-staged-outcome-v1'
                                    or value['benchmark_outcome'] not in ('pass', 'fail', 'invalid', 'infrastructure_error')
                                    or assessment.get('status') != value['benchmark_outcome']
                                    or entry.get('benchmark_outcome') != value['benchmark_outcome']
                                    or entry['status'] != value['benchmark_outcome']):
                                raise ValueError('DeepSWE result outcome mismatch')
                        elif value.get('schema_version') != 'deepswe-tb3-result-adapter-v1' or value['benchmark_outcome'] is not None:
                            raise ValueError('Unknown or mislabeled result schema')
    if sorted(linked) != sorted(by_run):
        raise ValueError('Unlinked or multiply linked results')
    gap_report = read(root, 'delivery-status.json')
    if gap_report['tb3_protocol_compliant'] is not False:
        raise ValueError('Historical package incorrectly claims TB3 compliance')
    harbor_status = gap_report.get('official_harbor_validation', 'not_performed')
    if harbor_status == 'passed_local':
        report = read(root, 'dataset/provenance/harbor-validation.json')
        if not report['passed'] or report['task_count'] != len(pairs) or report['manifest_sha256'] != digest(root / 'dataset/dataset.toml'):
            raise ValueError('Harbor validation receipt does not match this package')
    if gap_report['experiment_runnable']:
        if gap_report.get('execution_protocol') != 'DeepSWE staged':
            raise ValueError('Unsupported execution protocol')
        for name in ('run.py', 'run_batch.py', 'validate_release.py', 'runtime/project/dspilot/run_staged.py'):
            if not local(root, 'dataset/' + name).is_file(): raise ValueError('Missing execution entrypoint')
    # The runtime attachment must faithfully reproduce the offline reader payload.
    with tarfile.open(root / 'dataset/runtime_bundle.tar.gz', 'r:gz') as archive:
        members = archive.getmembers()
        if len({m.name for m in members}) != len(members):
            raise ValueError('Duplicate runtime attachment member')
        for member in members:
            if member.isdir():
                local(root / 'dataset', member.name)
                continue
            if not member.isfile():
                raise ValueError('Unexpected runtime attachment member')
            target = local(root / 'dataset', member.name)
            if hashlib.sha256(archive.extractfile(member).read()).hexdigest() != digest(target):
                raise ValueError('Runtime attachment mismatch')
    return {'schema_version': VERSION, 'readable': True, 'adapter_validation': 'passed',
            'results': links,
            'pairs': len(pairs), 'original_tasks': len({p['task_id'] for p in pairs}),
            'trials': len(results), 'requirements': requirement_count,
            'files_verified': len(expected), 'legacy_outcomes': dict(Counter(e['status'] for e in results)),
            'tb3_protocol_compliant': False, 'official_harbor_validation': harbor_status,
            'missing_item_counts': gap_report['missing_item_counts']}


def inspect_pair(root, delivery_id, results_dir=None):
    rows = [p for p in inventory(root) if p['delivery_id'] == delivery_id]
    if len(rows) != 1:
        raise ValueError('Unknown delivery pair: ' + delivery_id)
    pair = rows[0]
    base = pair['task_directory'] + '/tests/persona/'
    return {'identity': pair, 'results': result_links(root, results_dir), 'persona': read(root, base + 'persona.json'),
            'requirements': read(root, base + 'evolved_requirements.json'),
            'missing_items': read(root, base + 'delivery-status.json')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--pair', help='Inspect a delivery_id from dataset/inventory.json')
    parser.add_argument('--results-dir', type=Path, help='Separate result package for a standalone dataset')
    args = parser.parse_args()
    value = inspect_pair(args.root.resolve(), args.pair, args.results_dir) if args.pair else validate(args.root, args.results_dir)
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
