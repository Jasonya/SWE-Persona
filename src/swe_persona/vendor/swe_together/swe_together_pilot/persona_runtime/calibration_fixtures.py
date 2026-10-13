"""Check packaged calibration provenance before making any model requests."""
import difflib
import hashlib
import json
from pathlib import Path

from .evaluation import seal_evidence

PROTOCOL = 'swe-together-calibration-fixtures-v2'
ROOT = Path(__file__).resolve().parent


def review_diff(sources):
    chunks = []
    for path, filename in [('dataclaw/anonymizer.py', 'windows_anonymizer.py'),
                           ('tests/test_anonymizer.py', 'windows_test_anonymizer.py')]:
        before = (sources/'repositories/dataclaw'/path).read_text()
        after = (sources/'tasks/dataclaw-anonymizer-tests/environment'/filename).read_text()
        chunks.append('diff --git a/'+path+' b/'+path+'\n'+''.join(difflib.unified_diff(
            before.splitlines(True),after.splitlines(True),fromfile='a/'+path,tofile='b/'+path)))
    return ''.join(chunks)


def validate_cases(cases, root=ROOT):
    root = Path(root)
    sources = root/'calibration-sources'
    manifest = json.loads((sources/'manifest.json').read_text())
    if manifest.get('protocol') != 'swe-together-calibration-sources-v1':
        raise ValueError('Unknown calibration source protocol')
    for record in manifest['files']:
        rel = Path(record['path'])
        path = sources/rel
        if rel.is_absolute() or '..' in rel.parts or not path.resolve().is_relative_to(sources.resolve()):
            raise ValueError('Unsafe calibration source path')
        if hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256']:
            raise ValueError('Calibration source checksum mismatch: '+record['path'])
    if len({c['id'] for c in cases}) != len(cases):
        raise ValueError('Duplicate calibration control')
    branch_diff = review_diff(sources)
    for case in cases:
        evidence = case['evidence']
        if seal_evidence(evidence['artifacts'],evidence['messages'],
                         evidence.get('execution_history',evidence['executions'])) != evidence:
            raise ValueError('Modified calibration evidence: '+case['id'])
        fixture = case.get('fixture')
        if not fixture:
            continue
        if fixture.get('protocol') != PROTOCOL:
            raise ValueError('Unknown calibration fixture protocol')
        if evidence['executions'] or evidence.get('execution_history'):
            raise ValueError('Patch-only calibration fixture must not claim executions')
        artifacts = {a['ref']:a['text'] for a in evidence['artifacts']}
        if fixture['kind'] == 'pr_handoff':
            patch = artifacts['host:final_diff']
            if hashlib.sha256(patch.encode()).hexdigest() != fixture['reference_patch_sha256']:
                raise ValueError('Calibration reference patch changed')
            inventory = json.loads(artifacts[fixture['validation_inventory_ref']])
            if inventory.get('fixture_only') is not True or not inventory['items']:
                raise ValueError('Missing declared validation inventory')
            for item in inventory['items']:
                if item['status'] != 'not run' or not item['reason']:
                    raise ValueError('Unobserved validation reported as executed')
                source = sources/item['origin']
                if not source.resolve().is_relative_to(sources.resolve()) or not source.is_file():
                    raise ValueError('Validation declaration has no packaged source')
                if source.name == 'package.json':
                    scripts = json.loads(source.read_text())['scripts']
                    if item['command'] != 'bun run '+item['id'] or scripts.get(item['id']) != item['declared_script']:
                        raise ValueError('Repository command differs from its declaration')
                elif source.name == 'install_config.json':
                    if json.loads(source.read_text())['test_cmd'] != item['command']:
                        raise ValueError('Configured verifier command differs from its declaration')
                elif source.name == 'test_manifest.yaml':
                    import yaml
                    gates = yaml.safe_load(source.read_text())['gates']
                    if not any(g['id']==item['id'] and g['command']==item['command'] for g in gates):
                        raise ValueError('Gate command differs from its declaration')
                elif source.name != 'test.sh' or item['command'] != 'bash /tests/test.sh':
                    raise ValueError('Unknown command declaration')
            if case['expected'] == 'met':
                delivery = '\n'.join(m['text'] for m in evidence['messages'] if m['role']=='agent')
                if 'implements or documents the task behavior' in delivery:
                    raise ValueError('Generic placeholder in positive handoff')
                if any(item['command'] not in delivery for item in inventory['items']):
                    raise ValueError('Positive handoff omits a declared validation item')
                if any(path+':' not in delivery for path in fixture['per_file_reasons']):
                    raise ValueError('Positive handoff omits a changed-file explanation')
        elif fixture['kind'] == 'branch_review':
            if artifacts.get(fixture['diff_ref']) != branch_diff:
                raise ValueError('Review diff differs from pinned branch sources')
            expected = (sources/'tasks/dataclaw-anonymizer-tests/environment/windows_anonymizer.py').read_text()
            if artifacts.get('dataclaw/anonymizer.py') != expected:
                raise ValueError('Review source differs from pinned branch snapshot')
        elif fixture['kind'] == 'missing_review_diff':
            if (case['expected'] != 'unknown' or 'host:final_diff' in artifacts
                    or 'dataclaw/anonymizer.py' in artifacts):
                raise ValueError('Missing-review-diff control must remain unknown and omit the corresponding source')
        else:
            raise ValueError('Unknown calibration fixture kind')
    return {'protocol':PROTOCOL,'source_files_verified':len(manifest['files']),
            'controls':len(cases),'executions_synthesized':False}


def load_cases(root=ROOT):
    cases = json.loads((Path(root)/'calibration-cases.json').read_text())
    return cases, validate_cases(cases,root)
