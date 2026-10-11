"""Run a fresh versioned comparison; all results stay under the local runs directory."""
import argparse
import hashlib
import json
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import run_v3, integrity, report_policy
from .api import config
from .calibrate_v3 import model_roles, require_gate
from .prepare import verify_cache
from .protocol import dump
from .task import PERSONA_ID, ROOT, TASK_ID

ORDER = [('baseline', 'control', 'pivot'), ('control', 'pivot', 'baseline'),
         ('pivot', 'baseline', 'control')]


def make_plan(prefix, repeats):
    if not prefix.replace('-', '').replace('_', '').isalnum():
        raise ValueError('Invalid prefix')
    if repeats < 1:
        raise ValueError('Repeats must be positive')
    return [{'repeat': i+1, 'arm': arm, 'run_id': f'{prefix}-r{i+1}-{arm}'}
            for i in range(repeats) for arm in ORDER[i % len(ORDER)]]


def summarize(folder):
    plan = json.loads((folder / 'plan.json').read_text())
    rows = []
    for item in plan['episodes']:
        path = ROOT / 'runs' / item['run_id'] / 'result.json'
        if path.exists():
            saved = json.loads(path.read_text())
            try:
                status = integrity.verify_receipt(path.parent, saved,
                    required=plan.get('protocol_version') == 'matched-total-budget-judge-v3.3')
                issue = report_policy.exclusion_reason(plan.get('task_id', TASK_ID))
            except (OSError, ValueError, TypeError, KeyError) as exc:
                status, issue = 'invalid', str(exc)
            eligible = status in ('passed', 'not_checked') and not issue
            rows.append(item | {'result': saved, 'integrity_status': status,
                                 'formal_task_eligible': eligible, 'audit_issue': issue})
    means = {}
    lines = ['# Fresh DeepSWE persona comparison', '',
             f'Task: `{TASK_ID}`. Persona: `{PERSONA_ID}`.', '',
             f"Completed {len(rows)} of {len(plan['episodes'])} planned episodes. All arms are newly run; no historical controls are reused.", '',
             'Model roles: ' + json.dumps(plan['model_roles']) + '.', '',
             '| Repeat | Condition | F2P | P2P | Persona | Valid | Integrity | Minutes | Stop reason |',
             '|---:|---|---:|---:|---|---|---|---:|---|']
    for row in rows:
        r = row['result']; reward = r.get('reward') or {}; evolved = r.get('evolved') or {}
        persona = (f"{sum(v == 'met' for v in evolved.values())}/{len(evolved)}"
                   if evolved else 'N/A')
        if not r.get('evaluation_valid'):
            persona = 'Evaluation invalid'
        lines.append(f"| {row['repeat']} | [{row['arm']}](../{row['run_id']}/transcript.md) | "
                     f"{reward.get('f2p_passed', 'N/A')}/{reward.get('f2p_total', '?')} | "
                     f"{reward.get('p2p_passed', 'N/A')}/{reward.get('p2p_total', '?')} | "
                     f"{persona} | {r.get('evaluation_valid')} | {row['integrity_status']} | {r.get('seconds', 0)/60:.2f} | {r.get('end_reason')} |")
    lines += ['', '| Condition | Recorded / valid | Mean F2P passed | Strict task success | Mean minutes |',
              '|---|---:|---:|---:|---:|']
    for arm in ORDER[0]:
        group = [row['result'] for row in rows if row['arm'] == arm]
        valid = [row['result'] for row in rows if row['arm'] == arm
                 and row['formal_task_eligible'] and row['result'].get('evaluation_valid')
                 and row['result'].get('reward')]
        if not valid:
            continue
        means[arm] = statistics.mean(r['reward']['f2p_passed'] for r in valid)
        lines.append(f"| {arm} | {len(group)} / {len(valid)} | {means[arm]:.2f} | "
                     f"{sum(bool(r.get('base_pass')) for r in valid)}/{len(valid)} | "
                     f"{statistics.mean(r.get('seconds', 0) for r in valid)/60:.2f} |")
    complete = len(rows) == len(plan['episodes'])
    valid_all = complete and all(r['result'].get('evaluation_valid') and r['result'].get('reward')
                                 and r['formal_task_eligible'] for r in rows)
    if valid_all:
        lines += ['', f"Observed pivot minus control mean F2P: {means['pivot']-means['control']:+.2f} tests.",
                  'This is a single-task observation, not evidence of a general causal effect.']
    else:
        lines += ['', 'Incomplete or invalid suite: retain every result and do not infer a performance effect from the valid subset.']
    lines += ['', 'Persona success and original-task success are independent. All F2P and P2P tests must pass for strict task success.',
              'Pivot adds requirements, a public example, and checker feedback. Baseline also changes initial issue disclosure.',
              'Budgets per episode: 80 actions, 8 user turns, 150 API attempts, 45-minute request deadline. No phase reserve.',
              'Raw F2P/P2P above are retained; audited exclusions are omitted from formal means. Historical not_checked records are not retroactively certified.',
              'Calibration fixtures are separate from these scored episodes. No hidden-test feedback is given to the coding agent.']
    (folder / 'report.md').write_text('\n'.join(lines) + '\n')
    dump(folder / 'summary.json', {'completed': len(rows), 'planned': len(plan['episodes']),
                                 'complete_and_valid': valid_all, 'rows': rows, 'valid_f2p_means': means})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prefix', required=True)
    parser.add_argument('--repeats', type=int, default=1,
                        help='Episodes per condition (default: 1, three episodes total).')
    parser.add_argument('--task-id',default=TASK_ID)
    parser.add_argument('--persona-id',default=PERSONA_ID)
    args = parser.parse_args()
    if (args.task_id,args.persona_id)!=(TASK_ID,PERSONA_ID):
        from .specs import compile_episode
        compile_episode(args.task_id,args.persona_id)
        env=os.environ.copy();env['DS_TASK_ID']=args.task_id;env['DS_PERSONA_ID']=args.persona_id
        os.execve(sys.executable,[sys.executable,'-m','dspilot.suite','--prefix',args.prefix,
                  '--repeats',str(args.repeats),'--task-id',args.task_id,'--persona-id',args.persona_id],env)
    episodes = make_plan(args.prefix, args.repeats)
    verify_cache()
    gate = require_gate()
    fingerprint = run_v3.manifest()
    roles = model_roles(config())
    folder = ROOT / 'runs' / args.prefix
    # Refuse to overwrite or silently reuse any pre-existing episode.
    if folder.exists() or any((ROOT / 'runs' / item['run_id']).exists() for item in episodes):
        raise SystemExit('This prefix or an episode already exists; choose a new prefix')
    folder.mkdir(parents=True)
    plan = {'created_utc': datetime.now(timezone.utc).isoformat(),
            'task_id':TASK_ID,'persona_id':PERSONA_ID,
            'protocol_version': run_v3.PROTOCOL_VERSION, 'episodes': episodes,
            'model_roles': roles, 'harness_manifest': fingerprint, 'calibration_gate': gate,
            'suite_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'policy': 'All arms newly run; fixed repetitions, rotated order, no selective reruns; stop on evaluator or infrastructure failure.'}
    dump(folder / 'plan.json', plan)
    summarize(folder)
    for item in episodes:
        if run_v3.manifest() != fingerprint or model_roles(config()) != roles:
            raise SystemExit('Source or model configuration changed during suite')
        print(json.dumps({'starting': item}), flush=True)
        try:
            result = run_v3.episode(item['run_id'], item['arm'])
        finally:
            summarize(folder)
        if not result.get('evaluation_valid'):
            raise SystemExit('Invalid episode retained; suite stopped for inspection')
        if integrity.verify_receipt(ROOT/'runs'/item['run_id'], result, required=True) != 'passed':
            raise SystemExit('Integrity review required; episode retained and suite stopped')
    print(json.dumps({'suite_complete': args.prefix, 'episodes': len(episodes),
                      'report': str(folder / 'report.md')}), flush=True)


if __name__ == '__main__':
    main()
