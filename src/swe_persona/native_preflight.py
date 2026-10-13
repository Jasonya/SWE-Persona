"""Diagnostics for host-only SWE-Live reference verification."""
import json
from pathlib import Path


def native_preflight_report(directory):
    directory = Path(directory)
    attempts = []
    for root in sorted(directory.glob('attempt-*')):
        summary = root / 'evaluation/results.json'
        value = json.loads(summary.read_text()) if summary.is_file() else {}
        failures = {}
        for path in sorted((root / 'evaluation').glob('*/report.json')):
            report = json.loads(path.read_text())
            failures[report.get('instance_id', path.parent.name)] = {
                group: report[group].get('failure', [])
                for group in ('PASS_TO_PASS', 'FAIL_TO_PASS') if group in report}
        receipt=root/'attempt.json'
        exit_code=json.loads(receipt.read_text()).get('exit_code') if receipt.is_file() else None
        attempts.append({'directory':str(root), 'exit_code':exit_code,
                         'summary':value, 'failed_tests':failures})
    passed = bool(attempts) and all(
        a['exit_code']==0 and not any(tests for groups in a['failed_tests'].values() for tests in groups.values()) and
        all(a['summary'].get(k) == v for k, v in
            {'submitted':1, 'success':1, 'error':0, 'incomplete':0}.items())
        for a in attempts)
    return {'policy':'native-reference-stability-v1', 'passed':passed,
            'attempts':attempts, 'coding_score_produced':False,
            'note':'Reference verification only. Any failed repetition blocks the stability check; no best-attempt selection.'}
