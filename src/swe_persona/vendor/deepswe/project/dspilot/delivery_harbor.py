"""Local validation using pinned, unmodified official Harbor models and Packager.

No publishing, registry credentials, Docker or model calls. Dependencies live in
a separate validation environment, never the pinned Pier execution environment.
"""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import warnings

from . import delivery_runtime as rt


def official():
    vendor = Path(__file__).parent / 'vendor/harbor_packaging'
    lock = rt.read(vendor / 'source-lock.json')
    for rel, digest in lock['files'].items():
        if rt.sha(rt.safe(vendor, rel)) != digest:
            raise ValueError('Official Harbor source snapshot changed: ' + rel)
    sys.path.insert(0, str(vendor))
    from harbor.models.dataset.manifest import DatasetManifest
    from harbor.models.task.task import Task
    from harbor.publisher.packager import Packager
    for cls in (DatasetManifest, Task, Packager):
        if not Path(sys.modules[cls.__module__].__file__).resolve().is_relative_to(vendor.resolve()):
            raise ValueError('Another Harbor version was already imported; use a fresh validation process')
    return DatasetManifest, Task, Packager, lock


def write_manifest(dataset, attachments):
    Manifest, Task, Packager, _ = official()
    rows = rt.pairs(dataset)
    tasks = []
    for row in rows:
        path = rt.task_path(dataset, row)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            task = Task(path)
        digest, _ = Packager.compute_content_hash(path)
        tasks.append({'name': task.name, 'digest': 'sha256:' + digest})
    value = Manifest.model_validate({'schema_version': '1.0', 'dataset': {
        'name': f'MatrAIx-ai/deepswe-persona-{len(rows)}pairs',
        'description': 'DeepSWE staged Persona pairs; use the supplied Persona runner. Local release candidate.'},
        'tasks': tasks, 'files': [{'path': name, 'digest': 'sha256:' + rt.sha(dataset / name)} for name in attachments]})
    (dataset / 'dataset.toml').write_text(value.to_toml())


def extract_bundle(archive_path, target):
    with tarfile.open(archive_path, 'r:gz') as archive:
        names = set()
        for member in archive.getmembers():
            if member.name in names: raise ValueError('Duplicate runtime bundle member')
            names.add(member.name)
            dest = rt.safe(target, member.name)
            if member.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                if dest.exists(): raise ValueError('Runtime bundle overwrites existing content')
                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as inp, dest.open('wb') as out:
                    shutil.copyfileobj(inp, out)
                dest.chmod(member.mode & 0o777)
            else:
                raise ValueError('Unsupported runtime bundle member')


def validate(dataset, *, rebuild=True):
    dataset = Path(dataset).resolve()
    Manifest, Task, Packager, lock = official()
    manifest = Manifest.from_toml_file(dataset / 'dataset.toml')
    rows = rt.pairs(dataset)
    refs = {t.name: t.digest for t in manifest.tasks}
    if len(refs) != len(rows) or len(manifest.tasks) != len(rows):
        raise ValueError('Harbor task identity collision or inventory mismatch')
    collected = []; notices = set()
    for row in rows:
        path = rt.task_path(dataset, row)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            task = Task(path)
        notices.update(str(w.message) for w in caught)
        digest, files = Packager.compute_content_hash(path)
        if refs.get(task.name) != 'sha256:' + digest:
            raise ValueError('Official task digest mismatch: ' + row['delivery_id'])
        actual = {p.relative_to(path).as_posix() for p in path.rglob('*') if p.is_file()}
        selected = {p.relative_to(path).as_posix() for p in files}
        if actual != selected:
            raise ValueError('Official Packager drops task files: ' + str(sorted(actual - selected)))
        collected.append({'pair_id': row['delivery_id'], 'name': task.name,
                          'digest': 'sha256:' + digest, 'files': {r: rt.sha(path / r) for r in sorted(selected)}})
    attachments = {f.path: f.digest for f in manifest.files}
    if len(attachments) != len(manifest.files) or not {'README.md', 'runtime_bundle.tar.gz'} <= attachments.keys():
        raise ValueError('Missing or duplicate Harbor attachments')
    for name, digest in attachments.items():
        if 'sha256:' + rt.sha(rt.safe(dataset, name)) != digest:
            raise ValueError('Official attachment digest mismatch: ' + name)
    result = {'passed': True, 'scope': 'Official local schema/Packager and offline reconstruction; no registry upload or live execution',
              'upstream': lock, 'dataset_digest': 'sha256:' + manifest.compute_content_hash(),
              'manifest_sha256': rt.sha(dataset / 'dataset.toml'), 'tasks': collected,
              'attachments': attachments, 'task_count': len(rows), 'warnings': sorted(notices),
              'model_calls': 0, 'docker_calls': 0, 'registry_uploads': 0,
              'dependencies': {n: importlib.metadata.version(n) for n in ('pydantic', 'pathspec', 'toml')}}
    if rebuild:
        with tempfile.TemporaryDirectory(prefix='deepswe-harbor-reassemble-') as tmp:
            target = Path(tmp) / 'dataset'; target.mkdir()
            # Reproduce the official collected task files + declared dataset attachments.
            shutil.copy2(dataset / 'dataset.toml', target / 'dataset.toml')
            for entry in collected:
                source = dataset / 'tasks' / entry['pair_id']
                for rel in entry['files']:
                    dest = target / 'tasks' / entry['pair_id'] / rel
                    dest.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source / rel, dest)
            for name in attachments: shutil.copy2(dataset / name, target / name)
            extract_bundle(target / 'runtime_bundle.tar.gz', target)
            for p in target.rglob('*'):
                if p.is_file() and rt.sha(p) != rt.sha(dataset / p.relative_to(target)):
                    raise ValueError('Reassembled file differs: ' + str(p.relative_to(target)))
            # The original lock contains the packaging report itself. Seal the independently
            # reconstructed payload only after all official digests have been checked.
            from .export_tb3_delivery import seal
            seal(target)
            result['reassembled'] = rt.verify_dataset(target)
            plans = rt.command(target / 'runtime/project', rows[0], 'legacy', 'validate', arguments=('--pairs-json', json.dumps(rows)))
            result['reassembled']['plan_checks'] = len(plans)
            if not all(p['passed'] for p in plans): raise ValueError('Reassembled plan failed')
            result['reassembled']['official_manifest_and_tasks'] = validate(target, rebuild=False)['passed']
    return result


def main(dataset):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='Fresh report path outside the dataset')
    parser.add_argument('--initialize-lock', action='store_true',
        help='After Harbor download and runtime extraction, validate official digests and create missing local locks; never overwrite existing locks')
    args = parser.parse_args()
    dataset = Path(dataset).resolve()
    output = rt.work_root(Path(dataset).resolve(), args.output)
    if output.exists(): parser.error('Report already exists; choose a fresh path')
    if args.initialize_lock and any((dataset / name).exists() for name in ('release-lock.json', 'SHA256SUMS')):
        parser.error('Local locks already exist; refusing to replace a frozen release')
    result = validate(dataset)
    if args.initialize_lock:
        # Only the verified official file set and attachment payload may be sealed.
        allowed = {'dataset.toml', *result['attachments']}
        for task in result['tasks']:
            allowed.update('tasks/' + task['pair_id'] + '/' + rel for rel in task['files'])
        with tarfile.open(dataset / 'runtime_bundle.tar.gz') as bundle:
            allowed.update(m.name for m in bundle.getmembers() if m.isfile())
        actual = {p.relative_to(dataset).as_posix() for p in dataset.rglob('*') if p.is_file()}
        if actual != allowed: raise ValueError('Reconstructed directory contains missing/extra files; cannot initialize lock')
        from .export_tb3_delivery import seal
        rt.dump(dataset / 'provenance/harbor-validation.json', result)
        seal(dataset)
        rt.verify_dataset(dataset)
        result['initialized_local_lock'] = True
    rt.dump(output, result)
    print(json.dumps({'passed': True, 'tasks': result['task_count'], 'report': str(output), 'upstream_commit': result['upstream']['commit']}))
