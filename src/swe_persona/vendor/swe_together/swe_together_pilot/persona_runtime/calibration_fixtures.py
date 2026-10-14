"""Check packaged calibration provenance before making any model requests."""
import difflib
import hashlib
import json
from pathlib import Path
from collections import Counter

from .evaluation import seal_evidence

PROTOCOL = 'swe-together-calibration-fixtures-v3'
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
    suite_path = root/'calibration-suite.json'
    suite = json.loads(suite_path.read_text())
    controls = {c['id']:c for c in suite['cases']}
    if (suite.get('protocol') != PROTOCOL or len(controls) != 37
            or len(suite['cases']) != 37 or len(cases) != 37
            or {c['id'] for c in cases} != set(controls)
            or Counter(c['expected'] for c in cases) != {'met':18,'not_met':18,'unknown':1}):
        raise ValueError('Incomplete or changed calibration control suite')
    manifest = json.loads((sources/'manifest.json').read_text())
    if manifest.get('protocol') != 'swe-together-calibration-sources-v1':
        raise ValueError('Unknown calibration source protocol')
    declared_sources = {r['path'] for r in manifest['files']}
    if len(declared_sources) != len(manifest['files']):
        raise ValueError('Duplicate calibration source')
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
        control = controls[case['id']]
        if case['expected'] != control['expected'] or case['binding_file'] != control['binding_file']:
            raise ValueError('Changed calibration label or binding: '+case['id'])
        binding = json.loads((root.parent/case['binding_file']).read_text())
        from .binding import validate
        validate(binding)
        if binding['binding_sha256'] != control['binding_sha256']:
            raise ValueError('Changed calibration label or binding: '+case['id'])
        evidence = case['evidence']
        if seal_evidence(evidence['artifacts'],evidence['messages'],
                         evidence.get('execution_history',evidence['executions'])) != evidence:
            raise ValueError('Modified calibration evidence: '+case['id'])
        fixture = case.get('fixture')
        if not fixture:
            raise ValueError('Missing calibration fixture provenance')
        if fixture.get('protocol') != PROTOCOL:
            raise ValueError('Unknown calibration fixture protocol')
        if fixture.get('kind') != control['fixture_kind']:
            raise ValueError('Changed calibration fixture kind')
        if hashlib.sha256(json.dumps(fixture,sort_keys=True).encode()).hexdigest() != control['fixture_sha256']:
            raise ValueError('Changed calibration fixture provenance')
        if fixture['kind'] != 'static_learning' and (evidence['executions'] or evidence.get('execution_history')):
            raise ValueError('Patch-only calibration fixture must not claim executions')
        artifacts = {a['ref']:a['text'] for a in evidence['artifacts']}
        if fixture['kind'] == 'pr_handoff':
            patch = artifacts['host:final_diff']
            if hashlib.sha256(patch.encode()).hexdigest() != fixture['reference_patch_sha256']:
                raise ValueError('Calibration reference patch changed')
            inventory = json.loads(artifacts[fixture['validation_inventory_ref']])
            if inventory.get('fixture_only') is not True or not inventory['items']:
                raise ValueError('Missing declared validation inventory')
            # Completeness is checked against pinned declarations, not the
            # potentially shortened inventory or its delivery message.
            import yaml
            origins = set(control['inventory_origins'])
            expected_items = []
            for origin in origins:
                source = sources/origin
                if origin not in declared_sources:
                    raise ValueError('Unregistered validation declaration source')
                if source.name == 'package.json':
                    expected_items += [(origin,k,'bun run '+k) for k in json.loads(source.read_text())['scripts']]
                elif source.name == 'test_manifest.yaml':
                    expected_items += [(origin,g['id'],g['command']) for g in yaml.safe_load(source.read_text())['gates']]
                elif source.name == 'install_config.json':
                    expected_items.append((origin,'configured-test-command',json.loads(source.read_text())['test_cmd']))
                elif source.name == 'test.sh':
                    expected_items.append((origin,'native-verifier-entrypoint','bash /tests/test.sh'))
            if Counter((i['origin'],i['id'],i['command']) for i in inventory['items']) != Counter(expected_items):
                raise ValueError('Incomplete declared validation inventory')
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
        elif fixture['kind'] in {'static_learning','old_path','windows_boundary','bounded_snapshot'}:
            if fixture['kind'] == 'static_learning':
                history = evidence.get('execution_history', [])
                if (len(history) != 3 or any(r.get('execution_origin') != 'packaged_fixture_replay'
                        or r.get('returncode') != 0 or r.get('timed_out') is not False
                        or r.get('matches_current_snapshot') is not True for r in history)):
                    raise ValueError('Missing actual fixture static-check receipts')
            if fixture['kind'] == 'windows_boundary' and case['expected'] == 'met':
                delivery = '\n'.join(m['text'] for m in evidence['messages'] if m['role']=='agent')
                if not all(text in delivery for text in ('user_HASH/myproject\\file.py',
                        'user_HASH/work\\file.py','myproject/file.py','unchanged return condition')):
                    raise ValueError('Positive Windows walkthrough omits actual boundary outputs')
        else:
            raise ValueError('Unknown calibration fixture kind')
        expected_artifacts = {a['ref']:a for a in control['artifacts']}
        if len(artifacts) != len(evidence['artifacts']) or set(artifacts) != set(expected_artifacts):
            raise ValueError('Missing or unexpected calibration source artifact: '+case['id'])
        for ref, record in expected_artifacts.items():
            if hashlib.sha256(artifacts[ref].encode()).hexdigest() != record['sha256']:
                raise ValueError('Calibration artifact differs from reviewed source: '+ref)
            if record.get('source'):
                if record['source'] not in declared_sources or artifacts[ref] != (sources/record['source']).read_text():
                    raise ValueError('Calibration artifact differs from pinned source: '+ref)
        history = evidence.get('execution_history', [])
        if hashlib.sha256(json.dumps(history,sort_keys=True).encode()).hexdigest() != control['execution_history_sha256']:
            raise ValueError('Changed calibration execution receipts')
        if (type(case['activation_turn']) is not int or case['activation_turn'] < 0
                or not any(m['role']=='agent' and m['turn']>case['activation_turn'] for m in evidence['messages'])):
            raise ValueError('Calibration answer must follow requirement activation')
    return {'protocol':PROTOCOL,'source_files_verified':len(manifest['files']),
            'controls':len(cases),'executions_synthesized':False,
            'fixture_execution_records':sum(len(c['evidence'].get('execution_history', [])) for c in cases),
            'bounded_synthetic_snapshot_controls':sum(c['fixture']['kind']=='bounded_snapshot' for c in cases),
            'suite_sha256':hashlib.sha256(suite_path.read_bytes()).hexdigest()}


def load_cases(root=ROOT):
    cases = json.loads((Path(root)/'calibration-cases.json').read_text())
    return cases, validate_cases(cases,root)
