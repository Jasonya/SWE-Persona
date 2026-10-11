"""Independent audit of unmodified DeepSWE output; never a replacement grader.

Passing these checks is NOT proof against arbitrary malicious candidate code.
In particular, matching forged raw/native reports remain a residual risk.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from .source_scope import source_matches

VERSION = 'integrity-audit-v2-root-scope'
LIMITATIONS = [
    'Candidate code and the official test process are not mutually isolated.',
    'Consistently forged native reports and reward files may evade this audit.',
    'Patch pattern detection requires review; it does not prove malicious intent.',
]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def tree_manifest(directory):
    result = {}
    for path in sorted(Path(directory).rglob('*')):
        if path.is_symlink():
            raise ValueError('Verifier input must not contain symlinks')
        if path.is_file():
            result[path.relative_to(directory).as_posix()] = digest(path.read_bytes())
    if not result:
        raise ValueError('Empty verifier input')
    return result


def audit_patch(patch, source_roots):
    """Audit existing pilot scope, not an invented DeepSWE acceptance rule."""
    reasons, paths = [], []
    if patch.strip():
        # Git invoked inside a repository subdirectory can silently omit paths
        # outside that prefix. Parse in an empty unrelated directory so scope
        # audit is independent of the host harness checkout's working directory.
        with tempfile.TemporaryDirectory(prefix='dspilot-patch-paths-') as folder:
            parsed = subprocess.run(['git', 'apply', '--numstat', '-z'], input=patch.encode(),
                                    cwd=folder, capture_output=True, timeout=15)
        if parsed.returncode:
            return {'status': 'unknown', 'paths': [], 'reasons': ['Patch paths could not be parsed']}
        chunks = iter(parsed.stdout.decode(errors='strict').split('\0'))
        try:
            for chunk in chunks:
                if not chunk:
                    continue
                name = chunk.split('\t', 2)[2]
                paths.extend([name] if name else [next(chunks), next(chunks)])
        except (IndexError, StopIteration):
            return {'status': 'unknown', 'paths': [], 'reasons': ['Malformed patch path metadata']}
    for name in sorted(set(paths)):
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts:
            reasons.append('Unsafe patch path: ' + name)
        if not source_matches(name,source_roots):
            reasons.append('Outside the disclosed pilot source scope: ' + name)
        if path.name in {'conftest.py', 'sitecustomize.py', 'pytest.ini', 'tox.ini'}:
            reasons.append('Test/import hook requires review: ' + name)
    if re.search(r'^(?:new file|new|old) mode 120000$', patch, re.M):
        reasons.append('Symlink change requires review')
    added = '\n'.join(line[1:] for line in patch.splitlines()
                      if line.startswith('+') and not line.startswith('+++'))
    if re.search(r'/logs/verifier|/tests/grader|pytest_runtest_makereport|pytest_sessionfinish|PYTEST_CURRENT_TEST', added):
        reasons.append('Evaluation-specific code requires review')
    return {'status': 'review_required' if reasons else 'passed',
            'paths': sorted(set(paths)), 'reasons': reasons}


def audit_reward(config, reward, ctrf, exit_code):
    """Check identity/count consistency against HOST-owned pinned whitelist."""
    reasons = []
    if not isinstance(reward, dict):
        return {'status': 'unknown', 'reasons': ['Missing or malformed official reward']}
    expected = {kind: list(dict.fromkeys(str(x).strip() for x in config.get(kind + '_node_ids', [])
                                        if str(x).strip())) for kind in ('f2p', 'p2p')}
    for kind, ids in expected.items():
        passed, total = reward.get(kind + '_passed'), reward.get(kind + '_total')
        if type(total) is not int or total != len(ids):
            reasons.append(kind + ' total differs from pinned whitelist')
        if type(passed) is not int or not 0 <= passed <= len(ids):
            reasons.append(kind + ' passed count is invalid')
    if exit_code != 0:
        reasons.append('Official command ended with nonzero exit status')
    if not expected['f2p']:
        reasons.append('No pinned F2P evidence')
    if reward.get('apply_failed') == 1:
        if reward.get('reward') != 0 or any(reward.get(k + '_passed') != 0 for k in expected):
            reasons.append('Apply-failed reward contains passing credit')
        return {'status': 'failed' if reasons else 'passed', 'reasons': reasons,
                'note': 'Patch rejection is a valid task failure; no test execution is claimed.'}
    if not isinstance(ctrf, dict):
        return {'status': 'failed' if reasons else 'unknown',
                'reasons': reasons + ['Missing or malformed official CTRF report']}
    results = ctrf.get('results')
    rows = results.get('tests') if isinstance(results, dict) else None
    by_name = {}
    if not isinstance(rows, list):
        rows = []
        reasons.append('Malformed CTRF test list')
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('name'), str):
            reasons.append('Malformed CTRF test entry')
            continue
        if row['name'] in by_name:
            reasons.append('Duplicate CTRF identity: ' + row['name'])
        if row.get('status') not in ('passed', 'failed', 'skipped', 'pending', 'other'):
            reasons.append('Invalid CTRF test status: ' + row['name'])
        by_name[row['name']] = row.get('status')
    names = {f'[{kind}] {name}' for kind, ids in expected.items() for name in ids}
    if set(by_name) != names:
        reasons.append('CTRF identities differ from pinned F2P/P2P whitelist')
    for kind, ids in expected.items():
        passed = sum(by_name.get(f'[{kind}] {name}') == 'passed' for name in ids)
        if passed != reward.get(kind + '_passed'):
            reasons.append(kind + ' reward disagrees with CTRF outcomes')
    strict = bool(expected['f2p']) and all(reward.get(k + '_passed') == len(v) for k, v in expected.items())
    if type(reward.get('reward')) not in (int, float) or reward['reward'] != int(strict):
        reasons.append('Binary reward disagrees with pinned test counts')
    return {'status': 'failed' if reasons else 'passed', 'reasons': reasons}


def audit(patch, roots, config, reward, ctrf, exit_code, before, after):
    patch_check = audit_patch(patch, roots)
    outputs = audit_reward(config, reward, ctrf, exit_code)
    inputs = {'status': 'passed' if before == after and before else 'failed',
              'reasons': [] if before == after and before else ['Pinned verifier inputs changed']}
    checks = {'patch': patch_check, 'official_outputs': outputs, 'verifier_inputs': inputs}
    statuses = {c['status'] for c in checks.values()}
    status = next((s for s in ('failed', 'unknown', 'review_required') if s in statuses), 'passed')
    return {'version': VERSION, 'status': status, 'checks': checks,
            'patch_sha256': digest(patch.encode()), 'verifier_manifest': before,
            'official_reward_sha256': digest(json.dumps(reward, sort_keys=True).encode()),
            'limitations': LIMITATIONS, 'official_evaluator_modified': False}


def verify_receipt(folder, saved, *, required=False):
    """Check host evidence identity; this is not a cryptographic attestation."""
    receipt = saved.get('integrity')
    if receipt is None and not required:
        return 'not_checked'
    if not isinstance(receipt, dict) or receipt.get('version') != VERSION:
        raise ValueError('Missing or unsupported independent integrity audit')
    if json.loads((folder/'official/integrity.json').read_text()) != receipt:
        raise ValueError('Integrity evidence differs from saved result')
    if receipt.get('official_reward_sha256') != digest(json.dumps(saved.get('reward'), sort_keys=True).encode()):
        raise ValueError('Integrity audit refers to another official reward')
    if receipt.get('patch_sha256') != digest((folder/'model.patch').read_bytes()):
        raise ValueError('Integrity audit refers to another patch')
    checks = receipt.get('checks')
    if not isinstance(checks, dict) or set(checks) != {'patch', 'official_outputs', 'verifier_inputs'}:
        raise ValueError('Incomplete integrity checks')
    if any(not isinstance(c, dict) or c.get('status') not in
           ('passed', 'failed', 'unknown', 'review_required') for c in checks.values()):
        raise ValueError('Malformed integrity check status')
    statuses = {c['status'] for c in checks.values()}
    expected = next((s for s in ('failed', 'unknown', 'review_required') if s in statuses), 'passed')
    if receipt.get('status') != expected:
        raise ValueError('Integrity status disagrees with its checks')
    return expected
