"""Separate complete calibration success from bounded diagnostic execution."""
import hashlib
import json
import os
from pathlib import Path

POLICY = 'swe-together-calibration-gate-v1'
ROOT = Path(__file__).resolve().parent


def mode():
    value = os.environ.get('SWE_PERSONA_TOGETHER_CALIBRATION_MODE', 'bounded')
    if value not in {'strict', 'bounded'}:
        raise ValueError('SWE_PERSONA_TOGETHER_CALIBRATION_MODE must be strict or bounded')
    return value


def suite(root=ROOT):
    path = Path(root)/'calibration-suite.json'
    return json.loads(path.read_text()), hashlib.sha256(path.read_bytes()).hexdigest()


def decision(results, wire, validation, calibration_mode='bounded'):
    spec, sha = suite()
    expected = {c['id']:c for c in spec['cases']}
    bindings = {c['binding_file'] for c in spec['cases']}
    hard = []; soft = []
    if (calibration_mode not in {'strict', 'bounded'}
            or len(results) != len(expected)
            or {r.get('id') for r in results} != set(expected)
            or validation.get('suite_sha256') != sha
            or validation.get('controls') != len(expected)
            or validation.get('protocol') != spec['protocol']):
        hard.append('incomplete_or_changed_semantic_controls')
    if (len(wire) != len(bindings)
            or {r.get('binding_file') for r in wire} != bindings
            or any(r.get('passed') is not True for r in wire)):
        hard.append('simulator_transport_failed_or_incomplete')
    for row in results:
        control = expected.get(row.get('id'))
        judgment = row.get('judgment') or {}
        if (control is None or row.get('expected') != control['expected']
                or row.get('actual') not in {'met','not_met','unknown'}
                or judgment.get('status') != row.get('actual')
                or judgment.get('judge_error') or judgment.get('api_interrupted')):
            hard.append(row.get('id', 'malformed_judgment')); continue
        matched = row['actual'] == control['expected']
        if row.get('passed') is not matched:
            hard.append(row['id']); continue
        if not matched:
            if control.get('degradation_allowed') and control['expected'] == 'met':
                soft.append(row['id'])
            else:
                hard.append(row['id'])
    passed = not hard and not soft
    allowed = not hard and (passed or (calibration_mode == 'bounded'
        and len(soft) <= spec['max_positive_disagreements']))
    return {'policy':POLICY, 'mode':calibration_mode, 'passed':passed,
            'execution_allowed':allowed, 'status':'passed' if passed else 'degraded' if allowed else 'blocked',
            'score_provisional':allowed and not passed,
            'positive_disagreements':sorted(soft), 'blocking_controls':sorted(set(hard)),
            'max_positive_disagreements':spec['max_positive_disagreements'], 'suite_sha256':sha}


def calibration_allowed(report):
    if not isinstance(report, dict):
        return False
    try:
        checked = decision(report['results'], report['simulator_transport_controls'],
                           report['fixture_validation'], report['mode'])
        spec, _ = suite()
        if (report.get('controls') != len(spec['cases'])
                or set(report.get('binding_sha256s', [])) != {c['binding_sha256'] for c in spec['cases']}
                or report.get('passed_controls') != sum(r.get('passed') is True for r in report['results'])):
            return False
        return checked['execution_allowed'] and all(report.get(k) == v for k,v in checked.items())
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
