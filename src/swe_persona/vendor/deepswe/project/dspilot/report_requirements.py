"""Offline persona scoring from frozen episode inputs, never the live registry."""
from __future__ import annotations

import hashlib
import json

VERSION = 'persona-requirement-satisfaction-v1'
STATUSES = ('met', 'not_met', 'unknown', 'not_disclosed')


def verification_method(kind):
    if kind == 'trusted_probe':
        return 'executable'
    if kind in ('isolated_patch', 'reviewable_handoff', 'grounded_explanation'):
        return 'hybrid'
    return 'grounded_rubric' if kind == 'grounded_rubric' else 'unspecified'


def inspect_requirements(folder, saved, item):
    """Return auditable requirement rows and integrity errors, including on failed runs.

    Unknown means no confirmed satisfaction, not a demonstrated violation. It stays
    in the fixed denominator. Missing/malformed evaluation artifacts are errors,
    not unknown judgments. This function performs no new judging or model calls.
    """
    rows, issues, hashes = [], [], {}

    def read(name):
        path = folder / name
        try:
            content = path.read_bytes()
            hashes[name] = hashlib.sha256(content).hexdigest()
            value = json.loads(content)
            if not isinstance(value, dict):
                raise ValueError('Expected an object')
            return value
        except (OSError, ValueError) as exc:
            issues.append(f'{name}: {exc}')
            return {}

    config, checks = read('config.json'), read('checks.json')
    for field, expected in (('arm', 'pivot'), ('task_id', item['task_id']),
                            ('persona', item['persona_id'])):
        if config.get(field) != expected:
            issues.append(f'Frozen config {field} does not match episode')
    requirements = config.get('requirements')
    if not isinstance(requirements, list) or not requirements:
        issues.append('Missing or empty frozen requirements; cannot establish PRS denominator')
        return {'requirements': rows, 'issues': issues, 'hashes': hashes}
    ids = []
    for req in requirements:
        key = req.get('id') if isinstance(req, dict) else None
        if not isinstance(key, str) or not key or key in ids:
            issues.append('Frozen requirement IDs must be nonempty and unique')
            return {'requirements': rows, 'issues': issues, 'hashes': hashes}
        ids.append(key)
    evolved = saved.get('evolved')
    if not isinstance(evolved, dict):
        evolved = {}
        issues.append('Missing evolved requirement statuses')
    if set(evolved) != set(ids):
        issues.append('Result requirement IDs differ from frozen requirements')
    if set(checks) != set(ids):
        issues.append('Checker requirement IDs differ from frozen requirements')
    audit = saved.get('scheduling_audit')
    if (not isinstance(audit, dict) or audit.get('valid') is not True
            or audit.get('all_disclosed') is not True
            or audit.get('missing') != [] or audit.get('without_response_opportunity') != []):
        issues.append('Missing or failed scheduling audit: requirements need disclosure and response opportunity')
    for req in requirements:
        key = req['id']
        status = evolved.get(key)
        check = checks.get(key)
        if not isinstance(check, dict):
            check = {}
            issues.append(f'{key}: missing or malformed checker record')
        checked = check.get('status')
        if status not in STATUSES:
            issues.append(f'{key}: unsupported or missing result status {status!r}')
        if checked not in STATUSES[:3]:
            issues.append(f'{key}: unsupported or missing checker status {checked!r}')
        if status == 'not_disclosed':
            issues.append(f'{key}: requirement not disclosed')
        elif status != checked:
            issues.append(f'{key}: checker/result status mismatch')
        reasons = []
        errors = check.get('errors', [])
        if not isinstance(errors, list) or any(not isinstance(x, str) for x in errors):
            issues.append(f'{key}: malformed deterministic errors')
        else:
            reasons.extend(errors)
        if checked == 'met' and errors:
            issues.append(f'{key}: met verdict contradicts deterministic errors')
        votes = check.get('votes', [])
        if not isinstance(votes, list) or any(not isinstance(v, dict) for v in votes):
            issues.append(f'{key}: malformed judge votes')
            votes = []
        reasons.extend(v['reason'] for v in votes if isinstance(v.get('reason'), str))
        if isinstance(check.get('reason'), str):
            reasons.append(check['reason'])
        checker = req.get('checker')
        rows.append({'requirement_id': key, 'template': req.get('template'),
                     'statement': req.get('statement'),
                     'trigger': req.get('runtime_trigger', req.get('trigger')),
                     'checker_kind': checker.get('kind') if isinstance(checker, dict) else None,
                     'verification_method': verification_method(checker.get('kind') if isinstance(checker, dict) else None),
                     'status': status, 'checker_status': checked,
                     'reasons': list(dict.fromkeys(reasons)),
                     # Includes source citations, votes and any recovered judge errors.
                     'checker_record': check})
    all_met = all(evolved.get(key) == 'met' for key in ids)
    if isinstance(audit, dict) and audit.get('valid') is True:
        if type(saved.get('persona_pass')) is not bool or saved['persona_pass'] != all_met:
            issues.append('persona_pass disagrees with frozen requirement statuses')
    return {'requirements': rows, 'issues': issues, 'hashes': hashes}
