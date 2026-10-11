"""Versioned, offline matrix reports. Never rewrite plans or episode evidence."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path

from . import report_policy, report_requirements, integrity

ROOT = Path(__file__).resolve().parents[1]
VERSION = 'matrix-report-v3'
ARMS = ('baseline', 'control', 'pivot')
METRICS = ('strict_pass', 'f2p_passed', 'f2p_total', 'f2p_rate',
           'p2p_passed', 'p2p_total', 'p2p_rate', 'persona_pass', 'joint_pass',
           'prs_met', 'prs_total', 'prs_unknown', 'prs_not_met', 'prs_rate')


def identifier(value):
    if not isinstance(value, str) or not value or not value.replace('-', '').replace('_', '').isalnum():
        raise ValueError(f'Unsafe identifier: {value!r}')
    return value


def read_json(path):
    return json.loads(path.read_text())


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_plan(plan, prefix):
    if plan.get('prefix') != prefix or type(plan.get('repeats')) is not int or plan['repeats'] < 1:
        raise ValueError('Invalid saved matrix identity/repeats')
    pairs = [(identifier(p['task_id']), identifier(p['persona_id'])) for p in plan['pairs']]
    if not pairs or len(pairs) != len(set(pairs)):
        raise ValueError('Empty or duplicate registered pairs')
    tasks = {task for task, _ in pairs}
    expected = {(repeat, task, persona, arm)
                for repeat in range(1, plan['repeats'] + 1)
                for task, persona in pairs for arm in ('control', 'pivot')}
    expected |= {(repeat, task, None, 'baseline')
                 for repeat in range(1, plan['repeats'] + 1) for task in tasks}
    actual, run_ids = set(), set()
    for item in plan['episodes']:
        run_id = identifier(item['run_id'])
        if item['arm'] not in ARMS or (item['task_id'], item['persona_id']) not in pairs:
            raise ValueError('Episode is outside registered task/persona pairs')
        if type(item['repeat']) is not int:
            raise ValueError('Invalid episode repeat')
        key = (item['repeat'], item['task_id'],
               None if item['arm'] == 'baseline' else item['persona_id'], item['arm'])
        if key in actual or run_id in run_ids:
            raise ValueError('Duplicate episode or run ID')
        actual.add(key); run_ids.add(run_id)
    if actual != expected or plan.get('episode_count') != len(expected):
        raise ValueError('Saved matrix is missing/adding planned conditions')
    for key, expected_count in (('task_count', len(tasks)), ('pair_count', len(pairs)),
                                ('baseline_count', len(tasks) * plan['repeats']),
                                ('control_count', len(pairs) * plan['repeats']),
                                ('pivot_count', len(pairs) * plan['repeats'])):
        if plan.get(key) != expected_count:
            raise ValueError(f'Inconsistent plan count: {key}')


def checked_reward(reward):
    if not isinstance(reward, dict):
        raise ValueError('Missing task reward')
    for kind in ('f2p', 'p2p'):
        passed, total = reward.get(kind + '_passed'), reward.get(kind + '_total')
        if type(passed) is not int or type(total) is not int or not 0 <= passed <= total:
            raise ValueError(f'Invalid {kind} counts')
    if reward['f2p_total'] <= 0:
        raise ValueError('No F2P tests in reward')
    strict = (reward['f2p_passed'] == reward['f2p_total']
              and reward['p2p_passed'] == reward['p2p_total'])
    if type(reward.get('reward')) not in (int, float) or reward['reward'] != int(strict):
        raise ValueError('Task reward disagrees with strict test counts')
    return strict


def episode_row(root, item):
    row = {key: item[key] for key in ('repeat', 'task_id', 'persona_id', 'arm', 'run_id')}
    # The baseline's launcher binding is NOT a persona treatment.
    row['persona_factor'] = None if item['arm'] == 'baseline' else item['persona_id']
    row.update(status='missing', evaluation_valid=None, issue=None,
               exclusion_reason=report_policy.exclusion_reason(item['task_id']),
               formal_task_eligible=False, formal_persona_eligible=False,
               result_sha256=None, raw=None, requirements=[], requirement_issues=[],
               requirement_artifact_sha256={}, error=None, end_reason=None,
               scheduling_audit=None, integrity_status='not_checked',
               **{key: None for key in METRICS})
    folder = root / 'runs' / item['run_id']
    path = folder / 'result.json'
    if not path.exists():
        row['issue'] = 'Missing result.json; episode has not produced a final result'
        return row
    try:
        row['result_sha256'] = file_hash(path)
        saved = read_json(path)
        if not isinstance(saved, dict):
            raise ValueError('Result is not an object')
        # Early runtime failures may omit identity/score fields. Preserve their
        # cause before validating the rest of the result schema.
        for key in ('error', 'end_reason', 'scheduling_audit'):
            row[key] = saved.get(key)
        row['evaluation_valid'] = saved.get('evaluation_valid')
        row['raw'] = {key: saved.get(key) for key in
                      ('base_pass', 'reward', 'persona_pass', 'joint_pass', 'evolved')}
        for key in ('run_id', 'task_id', 'persona_id', 'arm'):
            if saved.get(key) != item[key]:
                raise ValueError(f'Result/plan identity mismatch: {key}')
        row['protocol_version'] = saved.get('protocol_version')
        manifest = folder / 'harness-manifest.json'
        row['harness_manifest_sha256'] = file_hash(manifest) if manifest.exists() else None
        if item['arm'] == 'pivot':
            inspected = report_requirements.inspect_requirements(folder, saved, item)
            row['requirements'] = inspected['requirements']
            row['requirement_issues'] = inspected['issues']
            row['requirement_artifact_sha256'] = inspected['hashes']
        if saved.get('evaluation_valid') is not True:
            reasons = ['Episode evaluation_valid is not true']
            if row['error'] is not None:
                reasons.append(str(row['error']))
            reasons.extend(row['requirement_issues'])
            raise ValueError('; '.join(reasons))
        if row['error'] is not None:
            raise ValueError(f"Episode reports an error despite evaluation_valid=true: {row['error']}")
        if row['requirement_issues']:
            raise ValueError('; '.join(row['requirement_issues']))
        strict = checked_reward(saved.get('reward'))
        if type(saved.get('base_pass')) is not bool or saved['base_pass'] != strict:
            raise ValueError('base_pass disagrees with strict test counts')
        if item['arm'] == 'pivot':
            persona = saved.get('persona_pass')
            if type(persona) is not bool:
                raise ValueError('Pivot lacks a boolean persona_pass')
            if type(saved.get('joint_pass')) is not bool or saved['joint_pass'] != (strict and persona):
                raise ValueError('joint_pass disagrees with original-task/persona success')
            row['persona_pass'] = persona
            statuses = [req['status'] for req in row['requirements']]
            row.update(formal_persona_eligible=True,
                       prs_met=statuses.count('met'), prs_total=len(statuses),
                       prs_unknown=statuses.count('unknown'), prs_not_met=statuses.count('not_met'),
                       prs_rate=statuses.count('met') / len(statuses))
        row['status'] = 'complete'
        # Preserve official values in raw. This is OUR reporting eligibility,
        # not a changed DeepSWE verdict or a persona requirement.
        row['integrity_status'] = integrity.verify_receipt(
            folder, saved, required=saved.get('protocol_version') == 'matched-total-budget-judge-v3.3')
        if row['integrity_status'] not in ('passed', 'not_checked'):
            row['exclusion_reason'] = ('Independent integrity audit: ' + row['integrity_status']
                                       + '; raw official results retained')
        if row['exclusion_reason']:
            return row
        reward = saved['reward']
        row.update(formal_task_eligible=True, strict_pass=strict,
                   **{key: reward[key] for key in ('f2p_passed', 'f2p_total', 'p2p_passed', 'p2p_total')})
        row['f2p_rate'] = reward['f2p_passed'] / reward['f2p_total']
        row['p2p_rate'] = reward['p2p_passed'] / reward['p2p_total'] if reward['p2p_total'] else None
        row['joint_pass'] = strict and row['persona_pass'] if item['arm'] == 'pivot' else None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        issue = str(exc)
        if row['error'] is not None and str(row['error']) not in issue:
            issue += '; runtime/evaluator error: ' + str(row['error'])
        row.update(status='invalid', issue=issue, formal_task_eligible=False, formal_persona_eligible=False,
                   **{key: None for key in METRICS})
    return row


def pass_summary(values):
    values = [value for value in values if value is not None]
    return {'passed': sum(values), 'denominator': len(values),
            'rate': sum(values) / len(values) if values else None}


def summarize(rows):
    eligible = [row for row in rows if row['formal_task_eligible']]
    passed = sum(row['f2p_passed'] for row in eligible)
    total = sum(row['f2p_total'] for row in eligible)
    p2p_passed = sum(row['p2p_passed'] for row in eligible)
    p2p_total = sum(row['p2p_total'] for row in eligible)
    p2p_rates = [row['p2p_rate'] for row in eligible if row['p2p_rate'] is not None]
    persona_rows = [row for row in rows if row['formal_persona_eligible']]
    prs_met = sum(row['prs_met'] for row in persona_rows)
    prs_total = sum(row['prs_total'] for row in persona_rows)
    groups = {}
    for method in ('executable','hybrid','grounded_rubric','unspecified'):
        requirements = [r for row in persona_rows for r in row['requirements']
                        if r.get('verification_method') == method]
        groups[method] = {'met':sum(r['status']=='met' for r in requirements),
                          'total':len(requirements),
                          'unknown':sum(r['status']=='unknown' for r in requirements)}
        groups[method]['rate'] = groups[method]['met']/len(requirements) if requirements else None
    return {
        'persona_verification_groups': groups,
        'planned': len(rows),
        'complete': sum(row['status'] == 'complete' for row in rows),
        'missing': sum(row['status'] == 'missing' for row in rows),
        'invalid': sum(row['status'] == 'invalid' for row in rows),
        'policy_excluded': sum(bool(row['exclusion_reason']) for row in rows),
        'formal_task_episodes': len(eligible),
        'strict': pass_summary(row['strict_pass'] for row in rows),
        'f2p': {'passed': passed, 'total': total,
                'pooled_rate': passed / total if total else None,
                'mean_episode_rate': sum(row['f2p_rate'] for row in eligible) / len(eligible) if eligible else None},
        'p2p': {'passed': p2p_passed, 'total': p2p_total,
                'pooled_rate': p2p_passed / p2p_total if p2p_total else None,
                'mean_episode_rate': sum(p2p_rates) / len(p2p_rates) if p2p_rates else None},
        'prs': {'met': prs_met, 'total': prs_total,
                'unknown': sum(row['prs_unknown'] for row in persona_rows),
                'not_met': sum(row['prs_not_met'] for row in persona_rows),
                'episode_denominator': len(persona_rows),
                'pooled_rate': prs_met / prs_total if prs_total else None,
                'mean_episode_rate': (sum(row['prs_rate'] for row in persona_rows) / len(persona_rows)
                                      if persona_rows else None)},
        'persona': pass_summary(row['persona_pass'] for row in rows),
        'joint': pass_summary(row['joint_pass'] for row in rows),
    }


def delta(left, right, metric):
    if left[metric] is None or right[metric] is None:
        return None
    if (left['f2p_total'], left['p2p_total']) != (right['f2p_total'], right['p2p_total']):
        return None
    return float(right[metric]) - float(left[metric])


def build_report(root, prefix):
    identifier(prefix)
    plan_path = root / 'runs' / 'matrices' / f'{prefix}.json'
    plan = read_json(plan_path)
    validate_plan(plan, prefix)
    rows = [episode_row(root, item) for item in plan['episodes']]
    indexed = {(row['repeat'], row['task_id'], row['persona_factor'], row['arm']): row for row in rows}
    pairs = []
    for repeat in range(1, plan['repeats'] + 1):
        for pair in plan['pairs']:
            task, persona = pair['task_id'], pair['persona_id']
            arms = {arm: indexed[repeat, task, None if arm == 'baseline' else persona, arm] for arm in ARMS}
            record = {'repeat': repeat, **pair, 'baseline_reused_for_display_only': True,
                      'exclusion_reason': report_policy.exclusion_reason(task)}
            for arm, row in arms.items():
                for key in ('run_id', 'status', 'issue', 'error', 'end_reason', 'exclusion_reason', 'integrity_status', *METRICS):
                    record[f'{arm}_{key}'] = row[key]
                reward = (row.get('raw') or {}).get('reward') or {}
                for key in ('f2p_passed', 'f2p_total', 'p2p_passed', 'p2p_total'):
                    record[f'{arm}_raw_{key}'] = reward.get(key) if isinstance(reward, dict) else None
                record[f'{arm}_raw_strict_pass'] = (row.get('raw') or {}).get('base_pass')
            for left in ('baseline', 'control'):
                record[f'pivot_minus_{left}_strict'] = delta(arms[left], arms['pivot'], 'strict_pass')
                record[f'pivot_minus_{left}_f2p_pp'] = (
                    None if (change := delta(arms[left], arms['pivot'], 'f2p_rate')) is None else 100 * change)
            pairs.append(record)
    tasks = sorted({row['task_id'] for row in rows})
    return {
        'report_version': VERSION, 'policy_version': report_policy.VERSION, 'prefix': prefix,
        'prs_version': report_requirements.VERSION,
        'policy_exclusions': report_policy.EXCLUDED_TASKS,
        'plan_sha256': file_hash(plan_path),
        'report_source_sha256': file_hash(Path(__file__)),
        'policy_source_sha256': file_hash(Path(report_policy.__file__)),
        'prs_source_sha256': file_hash(Path(report_requirements.__file__)),
        'complete_and_valid': all(row['status'] == 'complete' for row in rows),
        'definitions': {
            'strict_pass': 'All F2P and P2P tests pass; valid and formally eligible episodes only.',
            'f2p_rate': 'Passed F2P / total F2P, not strict task completion.',
            'p2p_rate': 'Passed P2P / total P2P; zero P2P tests yields null, not 100%.',
            'prs_rate': 'Confirmed met / ALL requirements frozen in episode config.json; equal weights; pivot only. '
                        'Unknown remains in the denominator with no credit, but is not a demonstrated violation. '
                        'No rejudging and no lookup in the mutable binding registry.',
            'prs_eligibility': 'Requires a valid episode, matching frozen config/result/checker IDs and statuses, '
                               'and a valid disclosure/response-opportunity audit. Missing evidence or evaluator '
                               'errors invalidate the episode, not an ordinary agent failure. Obsidian task '
                               'exclusion does not exclude independently valid persona scores.',
            'persona_pass': 'All registered persona requirements satisfied; pivot only; independent of task eligibility.',
            'joint_pass': 'Strict task success AND persona success; eligible valid pivots only.',
            'null': 'Not applicable, missing, invalid or policy-excluded; never interpreted as failure.',
            'baseline': 'One original baseline per task/repeat; repeated display in pair rows is not an independent sample.',
            'raw': 'Unmodified historical evaluator output, audit only; never substituted into excluded formal metrics.',
            'aggregation': 'Episode-based descriptive denominators; pooled test/requirement rates and mean episode '
                           'rates are separate. PRS mean_episode_rate weights eligible episodes equally; pooled_rate '
                           'weights requirements equally. No causal claim.',
        },
        'summary': {arm: summarize([row for row in rows if row['arm'] == arm]) for arm in ARMS},
        'tasks': {task: {arm: summarize([row for row in rows if row['task_id'] == task and row['arm'] == arm])
                         for arm in ARMS} for task in tasks},
        'pairs': pairs, 'episodes': rows,
    }


def count(value):
    return f"{value['passed']}/{value['denominator']}" if value['denominator'] else 'N/A'


def mark(value):
    return 'N/A' if value is None else ('PASS' if value else 'FAIL')


def fraction(passed, total):
    return 'N/A' if passed is None or not total else f'{passed}/{total}'


def percent(value):
    return 'N/A' if value is None else f'{100 * value:.2f}%'


def cell(value):
    return str(value).replace('|', '\\|').replace('\r', ' ').replace('\n', '<br>')


def requirement_rows(report):
    rows = []
    for episode in report['episodes']:
        for req in episode['requirements']:
            rows.append({key: episode[key] for key in ('run_id', 'repeat', 'task_id', 'persona_id', 'arm')} |
                        {'episode_status': episode['status'],
                         'formal_persona_eligible': episode['formal_persona_eligible'],
                         'episode_issue': episode['issue']} | req)
    return rows


def markdown(report):
    lines = ['# Matrix results', '',
             f"Matrix: `{report['prefix']}`. Report: `{VERSION}`. Policy: `{report_policy.VERSION}`.", '',
             'Strict success = all F2P AND P2P pass. F2P progress is a separate metric.',
             'Persona and joint success apply only to pivot. N/A is not a failed episode.',
             'PRS = confirmed met / all frozen applicable requirements (equal weights). Unknown stays in the denominator '
             'with no credit; it is not a demonstrated violation. PRS is not text similarity.',
             'PRS mean weights eligible episodes equally; pooled PRS weights requirements equally. '
             'Missing scoring evidence, evaluator errors, and invalid disclosure audits are excluded, not ordinary failures.',
             'Baseline is measured once per task/repeat, reused below for comparison only.',
             'Control already has persona-specific opening text; these contrasts do not isolate persona wording.', '',
             'Obsidian is excluded from formal original-task/F2P/P2P/joint statistics in every arm. '
             'Its raw scores are audit-only; persona scores remain available. No corrected evaluator is claimed.', '',
             'Aggregates are descriptive, with unequal task/pair counts and potentially different task weights. '
             'Use within-task/pair comparisons, not pooled baseline-to-pivot rates, to assess changes.', '',
             '| Arm | Complete/planned | Invalid | Excluded by policy | Strict | F2P pooled | P2P pooled | PRS mean (episodes) | PRS pooled | Unknown reqs | Persona | Joint |',
             '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for arm, group in report['summary'].items():
        f2p = group['f2p']
        p2p, prs = group['p2p'], group['prs']
        lines.append(f"| {arm} | {group['complete']}/{group['planned']} | {group['invalid']} | "
                     f"{group['policy_excluded']} | {count(group['strict'])} | "
                     f"{fraction(f2p['passed'], f2p['total'])} | {fraction(p2p['passed'], p2p['total'])} | "
                     f"{percent(prs['mean_episode_rate'])} ({prs['episode_denominator']}) | "
                     f"{fraction(prs['met'], prs['total'])} | {fraction(prs['unknown'], prs['total'])} | "
                     f"{count(group['persona'])} | {count(group['joint'])} |")
    if not report['complete_and_valid']:
        lines += ['', 'WARNING: Incomplete or invalid matrix. Do not infer a performance effect from the available subset.']
    lines += ['', '## Persona verification methods', '',
              'These groups use the frozen checker kind. A numeric PRS does not make a rubric deterministic. '
              'Hybrid checks combine mechanical prerequisites with semantic judgment; executable probes still need anti-tamper and semantic calibration.', '',
              '| Method | Confirmed met / frozen requirements | Unknown |', '|---|---:|---:|']
    for method, group in report['summary']['pivot']['persona_verification_groups'].items():
        lines.append(f"| {method} | {fraction(group['met'],group['total'])} | {group['unknown']} |")
    lines += ['', '## Independent integrity audit', '',
              'Official rewards are retained unchanged. Failed, unknown or review-required audits exclude original-task and joint metrics under our reporting policy. '
              'Historical episodes without this audit are explicitly not_checked; they are not retrospectively certified. Passing is not proof against consistently forged reports.', '',
              '| Run | Audit | Exclusion |', '|---|---|---|']
    for episode in report['episodes']:
        lines.append('| '+ ' | '.join(cell(x) for x in (episode['run_id'],episode['integrity_status'],episode['exclusion_reason'] or 'none'))+' |')
    lines += ['', '## Every task/persona pairing', '',
              'Cells show strict success; F2P passed/total; P2P passed/total. Delta is pivot minus comparator in F2P '
              'percentage points, NOT a change in strict success. CSV/JSON also include separate strict deltas.', '',
              '| Repeat | Task | Persona | Baseline: strict; F2P; P2P | Control: strict; F2P; P2P | Pivot: strict; F2P; P2P | Pivot PRS | Unknown reqs | Pivot persona | Pivot joint | ΔF2P vs baseline (pp) | ΔF2P vs control (pp) |',
              '|---:|---|---|---|---|---|---:|---:|---|---|---:|---:|']
    for pair in report['pairs']:
        cells = []
        for arm in ARMS:
            if pair[f'{arm}_f2p_total'] is None:
                cells.append('EXCLUDED' if pair[f'{arm}_exclusion_reason'] and pair[f'{arm}_status'] == 'complete'
                             else pair[f'{arm}_status'].upper())
            else:
                cells.append(f"{mark(pair[f'{arm}_strict_pass'])}; "
                             f"{fraction(pair[f'{arm}_f2p_passed'], pair[f'{arm}_f2p_total'])}; "
                             f"{fraction(pair[f'{arm}_p2p_passed'], pair[f'{arm}_p2p_total'])}")
        differences = [pair[f'pivot_minus_{arm}_f2p_pp'] for arm in ('baseline', 'control')]
        formatted = ['N/A' if value is None else f'{value:+.2f}' for value in differences]
        lines.append('| ' + ' | '.join([str(pair['repeat']), pair['task_id'], pair['persona_id'], *cells,
                                       fraction(pair['pivot_prs_met'], pair['pivot_prs_total']),
                                       fraction(pair['pivot_prs_unknown'], pair['pivot_prs_total']),
                                       mark(pair['pivot_persona_pass']), mark(pair['pivot_joint_pass']), *formatted]) + ' |')
    lines += ['', '## Every persona requirement', '',
              'Statuses below are preserved audit evidence. Rows from invalid episodes do not contribute to PRS or success '
              'rates. Reasons include checker errors and judge votes, which may disagree. Full citations and judge audit '
              'records are available in requirements.csv and summary.json.', '',
              '| Run | Requirement | User requirement | Result status | Checker status | Scored | Reasons / episode issue |',
              '|---|---|---|---|---|---|---|']
    for req in requirement_rows(report):
        reasons = '; '.join(req['reasons'])
        if req['episode_issue']:
            reasons += ('; ' if reasons else '') + req['episode_issue']
        values = [req['run_id'], req['requirement_id'], req['statement'] or '',
                  req['status'] or 'MISSING', req['checker_status'] or 'MISSING',
                  'yes' if req['formal_persona_eligible'] else 'no', reasons or 'No textual reason recorded']
        lines.append('| ' + ' | '.join(cell(value) for value in values) + ' |')
    excluded = [row for row in report['episodes'] if row['exclusion_reason']]
    if excluded:
        lines += ['', '## Excluded raw scores — audit only', '',
                  'These values retain the known scoring defect. They are NOT formal failures or corrected scores.', '',
                  '| Run | Raw strict | Raw F2P | Raw P2P | Status |', '|---|---|---:|---:|---|']
        for row in excluded:
            raw = row['raw'] or {}; reward = raw.get('reward') or {}
            if not isinstance(reward, dict):
                reward = {}
            lines.append(f"| {row['run_id']} | {mark(raw.get('base_pass'))} | "
                         f"{reward.get('f2p_passed', 'N/A')}/{reward.get('f2p_total', '?')} | "
                         f"{reward.get('p2p_passed', 'N/A')}/{reward.get('p2p_total', '?')} | {row['status']} |")
    issues = [row for row in report['episodes'] if row['issue']]
    if issues:
        lines += ['', '## Missing or invalid episodes', '',
                  '| Run | Status | End reason | Error / evidence issue |', '|---|---|---|---|']
        for row in issues:
            lines.append('| ' + ' | '.join(cell(value) for value in
                         (row['run_id'], row['status'], row['end_reason'] or 'N/A', row['issue'])) + ' |')
    return '\n'.join(lines) + '\n'


def csv_text(rows, fields):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: json.dumps(row.get(key), ensure_ascii=False)
                         if isinstance(row.get(key), (dict, list)) else row.get(key) for key in fields})
    return stream.getvalue()


def write_report(root, prefix):
    report = build_report(root, prefix)
    folder = root / 'runs' / 'matrices' / f'{prefix}-report' / VERSION
    folder.mkdir(parents=True, exist_ok=True)
    requirements = requirement_rows(report)
    requirement_fields = ['run_id', 'repeat', 'task_id', 'persona_id', 'arm', 'episode_status',
                          'formal_persona_eligible', 'episode_issue', 'requirement_id', 'template',
                          'statement', 'trigger', 'checker_kind', 'verification_method', 'status', 'checker_status',
                          'reasons', 'checker_record']
    episode_fields = ['run_id', 'repeat', 'task_id', 'persona_id', 'persona_factor', 'arm', 'status',
                      'evaluation_valid', 'formal_task_eligible', 'formal_persona_eligible',
                      'exclusion_reason', 'integrity_status', 'issue', 'error', 'end_reason', 'scheduling_audit', *METRICS]
    outputs = {'summary.json': json.dumps(report, indent=2, ensure_ascii=False) + '\n',
               'pairs.csv': csv_text(report['pairs'], list(report['pairs'][0])),
               'episodes.csv': csv_text(report['episodes'], episode_fields),
               'requirements.csv': csv_text(requirements, requirement_fields),
               'report.md': markdown(report)}
    for name, content in outputs.items():
        temporary = folder / (name + '.tmp')
        temporary.write_text(content)
        temporary.replace(folder / name)
    return folder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', required=True)
    args = parser.parse_args()
    folder = write_report(ROOT, args.prefix)
    print(json.dumps({'report': str(folder / 'report.md'), 'summary': str(folder / 'summary.json'),
                      'pairs': str(folder / 'pairs.csv'), 'episodes': str(folder / 'episodes.csv'),
                      'requirements': str(folder / 'requirements.csv'), 'report_version': VERSION,
                      'prs_version': report_requirements.VERSION, 'policy_version': report_policy.VERSION}))


if __name__ == '__main__':
    main()
