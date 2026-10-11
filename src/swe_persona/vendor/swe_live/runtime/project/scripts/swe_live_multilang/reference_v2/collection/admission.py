"""Validate and freeze public fit decisions, bindings and matched openings.

This module does not decide semantic applicability. Every decision needs a
public, outcome-blind fit record; an absent implementation remains unresolved.
"""
from pathlib import Path
import shutil

from support import Manifest, collection_source_hashes, file_hash, read, save
from personas import PERSONAS
from task_binding import candidate_for, load_binding, safe_relative


def artifact(root, relative):
    root = Path(root).resolve()
    path = (root / safe_relative(relative)).resolve()
    if root not in path.parents or not path.is_file():
        raise ValueError('Missing or escaping admission artifact: ' + relative)
    return path


def validate_pairs(root, manifest, rows, task_root):
    tasks = {t['key']: t for t in manifest['tasks']}
    expected = {(key, persona) for key in tasks for persona in manifest['personas']}
    actual = [(r.get('task_key'), r.get('persona')) for r in rows]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError('Admission must preserve exactly the complete task-persona matrix')
    outputs, files = [], {}
    for row in rows:
        task = tasks[row['task_key']]
        public = Manifest(Path(task_root) / task['path'])
        if row.get('instance_id') != task['instance_id']:
            raise ValueError('Admission task identity mismatch')
        fit = row.get('public_fit', {})
        quotes = fit.get('public_issue_quotes', [])
        if (fit.get('outcome_blind') is not True or not fit.get('reason')
                or not quotes or any(not isinstance(q, str) or not q or q not in public.instruction for q in quotes)):
            raise ValueError('Every fit record needs outcome-blind reasoning and exact public issue evidence')
        public.assert_no_leak(str(fit), 'public fit record')
        status = row.get('status')
        if status not in ('applicable', 'not_applicable', 'binding_required', 'needs_public_review'):
            raise ValueError('Unrecognized admission status')
        if status == 'not_applicable':
            if fit.get('decision') != 'incompatible' or fit.get('reason_code') not in (
                    'task_domain_mismatch', 'original_contract_conflict', 'required_environment_incompatible'):
                raise ValueError('Missing implementations cannot be admitted as persona incompatibility')
        elif status in ('applicable', 'binding_required') and fit.get('decision') != 'compatible':
            raise ValueError('Applicable or unimplemented pairs require a compatible public fit decision')
        record = dict(row, reason=fit['reason'])
        if status == 'applicable':
            bound = None
            if row.get('binding_path'):
                path = artifact(root, row['binding_path'])
                bound = load_binding(path, public, row['persona'])
                candidate_for(bound)
                files[row['binding_path']] = file_hash(path)
                for item in bound['evidence']:
                    relative = (Path(row['binding_path']).parent / item['path']).as_posix()
                    files[relative] = file_hash(artifact(root, relative))
                record['binding_sha256'] = files[row['binding_path']]
            elif row['persona'] not in PERSONAS:
                raise ValueError('An applicable persona is missing its executable task binding')
            if not row.get('opening_path'):
                raise ValueError('Applicable pair needs one frozen opening shared by all arms')
            path = artifact(root, row['opening_path'])
            opening = read(path)
            if (opening.get('task_id') != task['instance_id'] or opening.get('persona') != row['persona']
                    or not opening.get('vague_request')):
                raise ValueError('Frozen opening identity or content missing')
            public.assert_no_leak(opening['vague_request'], 'admission opening')
            files[row['opening_path']] = file_hash(path)
            record['opening_sha256'] = files[row['opening_path']]
        outputs.append(record)
    return outputs, files


def freeze_admission(study, manifest, source):
    study, source = Path(study), Path(source).resolve()
    target = study / 'admission'
    if target.exists():
        raise ValueError('Admission already exists; preserve it instead of overwriting decisions')
    package = read(source / 'admission-input.json')
    if package.get('dataset_revision') != manifest['dataset_revision']:
        raise ValueError('Admission dataset revision mismatch')
    rows, files = validate_pairs(source, manifest, package['pairs'], study)
    target.mkdir()
    for relative, sha256 in files.items():
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(artifact(source, relative), destination)
        if file_hash(destination) != sha256:
            raise ValueError('Admission artifact changed while copying')
    save(target / 'frozen.json', {'manifest_sha256': file_hash(study / 'manifest.json'),
         'collection_sources': collection_source_hashes(), 'pairs': rows, 'files': files,
         'input_sha256': file_hash(source / 'admission-input.json')})
    save(study / 'admission-seal.json', {'frozen_sha256': file_hash(target / 'frozen.json'),
         'manifest_sha256': file_hash(study / 'manifest.json')})
    return resolved_inventory(study, manifest)


def resolved_inventory(study, manifest):
    study = Path(study)
    path = study / 'admission' / 'frozen.json'
    if not path.exists():
        return read(study / 'inventory.json')
    seal = read(study / 'admission-seal.json')
    if (seal.get('frozen_sha256') != file_hash(path)
            or seal.get('manifest_sha256') != file_hash(study / 'manifest.json')):
        raise ValueError('Frozen admission decisions changed')
    frozen = read(path)
    if (frozen.get('manifest_sha256') != file_hash(study / 'manifest.json')
            or frozen.get('collection_sources') != collection_source_hashes()):
        raise ValueError('Admission configuration or collection code changed')
    rows, files = validate_pairs(path.parent, manifest, frozen['pairs'], study)
    if files != frozen['files']:
        raise ValueError('Frozen admission evidence changed')
    for row in rows:
        for key in ('opening_path', 'binding_path'):
            if row.get(key):
                row[key] = 'admission/' + row[key]
    return rows


def import_qualification(study, manifest, source):
    """Copy full histories, including invalid attempts, after verifying all roles."""
    from audit_sample import audit
    study, source = Path(study).resolve(), Path(source).resolve()
    if source == study:
        return
    for persona in manifest['personas']:
        receipt = read(source / 'qualification' / persona / 'qualification.json')
        sample = (source / safe_relative(receipt['sample_path'])).resolve()
        if source not in sample.parents or receipt['persona'] != persona:
            raise ValueError('Qualification identity or path mismatch')
        protocol = read(sample / 'protocol.json')
        if (receipt['sources'] != manifest['sources'] or protocol['sources'] != manifest['sources']
                or receipt.get('fixture_controls_passed') is not True
                or receipt['episode_sha256'] != file_hash(sample / 'episode.json')
                or not audit(sample)['ready_for_review']):
            raise ValueError('Qualification does not validate the current runtime: ' + persona)
    if (study / 'qualification').exists():
        raise ValueError('Qualification history already exists; do not overwrite it')
    shutil.copytree(source / 'qualification', study / 'qualification')
