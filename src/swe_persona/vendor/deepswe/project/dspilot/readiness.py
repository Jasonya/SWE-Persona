"""Read-only coverage/release evidence inventory. No Docker, API or credentials.

Registration, current-source calibration and recorded execution are different
claims. Missing evidence never becomes a pass. Human audit is not inferred.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from . import specs
from .negative_controls import validate_controls, controls_for
from . import report_policy
from .interaction_policy import VERSION as PROTOCOL_VERSION
from .fingerprint import episode_manifest

VERSION = 'release-evidence-v1'


def read_object(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def current_manifest(root, recorded, expected):
    """Verify every recorded file, and require the new integrity module.

    This is evidence freshness, not runtime gate authorization. require_gate()
    additionally checks the exact manifest and model roles at execution time.
    """
    if not isinstance(recorded, dict) or recorded != expected:
        return False
    for name, expected in recorded.items():
        path = root/name
        try:
            path.resolve().relative_to(root.resolve())
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                return False
        except (OSError, ValueError):
            return False
    return True


def inventory(root=None):
    root = Path(root or specs.ROOT)
    if root.resolve() != specs.ROOT.resolve():
        raise ValueError('Run from the source checkout being audited; do not silently mix registries')
    personas = specs.persona_catalog()
    pairs, failures = [], []
    results = []
    for path in sorted((root/'runs').glob('*/result.json')):
        saved = read_object(path)
        if saved.get('arm') in ('baseline', 'control', 'pivot'):
            results.append((path, saved))
    for task, persona in sorted(specs.binding_paths()):
        ep = specs.compile_episode(task, persona)
        expected = episode_manifest(ep)
        reqs = ep.requirements
        missing = sorted(validate_controls(ep.binding.raw, ep.task.raw))
        controls = controls_for(ep.binding.raw)
        gate_file = ('runs/judge-v3-gates.json' if (task,persona)==
                     ('tomlkit-toml-table-converters','first_time_pr_contributor') else
                     f'runs/gates/{task}--{persona}.json')
        gate = read_object(root/gate_file)
        calibration = ('not_recorded' if not gate else 'failed' if gate.get('passed') is not True else
                       'recorded_source_passed' if current_manifest(root,gate.get('harness_manifest'),expected) else 'stale')
        executions = []
        for path, saved in results:
            if (saved.get('task_id'),saved.get('persona_id')) != (task,persona):
                continue
            manifest = read_object(path.parent/'harness-manifest.json')
            current = (saved.get('protocol_version') == PROTOCOL_VERSION and current_manifest(root,manifest,expected))
            # A receipt alone is not proof of valid scoring; use the same frozen
            # evidence checks as the matrix report, without making new scores.
            from .matrix_report import episode_row
            item = {k:saved.get(k) for k in ('run_id','task_id','persona_id','arm')}
            item['repeat'] = 1
            if not isinstance(item['run_id'],str) or item['run_id'] != path.parent.name:
                continue
            row = episode_row(root,item)
            executions.append({'run_id':saved['run_id'],'arm':saved['arm'],
                               'current_source':current,'status':row['status'],
                               'formal_task_eligible':row['formal_task_eligible'],
                               'formal_persona_eligible':row['formal_persona_eligible'],
                               'integrity_status':row['integrity_status']})
        template_ids = {r['template'] for r in reqs}
        pairs.append({'task_id':task,'persona_id':persona,'registered':True,
                      'requirements':len(reqs),'templates':sorted(template_ids),
                      'omitted_templates':sorted(t['id'] for t in personas[persona]['attributes']['evolved_requirements']
                                                 if t['id'] not in template_ids),
                      'checker_kinds':dict(Counter(r['checker']['kind'] for r in reqs)),
                      'negative_controls':len(controls),'missing_negative_controls':missing,
                      'semantic_control_requirements':sorted({c['requirement_id'] for c in controls
                                                             if c.get('purpose')=='semantic_counterexample'}),
                      'calibration':calibration,'gate_file':gate_file,
                      'model_roles':gate.get('model_roles'), 'executions':executions,
                      'human_audited':False,
                      'human_audit_note':'No release-level human audit receipt has been assessed.',
                      'task_exclusion':report_policy.exclusion_reason(task)})
        if missing:
            failures.append(f'{task}/{persona}: missing negative controls')
    rows=[]
    for key, persona in personas.items():
        selected=[p for p in pairs if p['persona_id']==key]
        used={t for p in selected for t in p['templates']}
        rows.append({'persona_id':key,'registered_pairs':len(selected),
                     'templates_bound':len(used),'templates_total':len(persona['attributes']['evolved_requirements']),
                     'calibrated_pairs':sum(p['calibration']=='recorded_source_passed' for p in selected),
                     'executed_pairs':sum(any(e['current_source'] and e['arm']=='pivot' and e['status']=='complete'
                                              for e in p['executions']) for p in selected),
                     'audited_pairs':0})
    return {'version':VERSION,'protocol':PROTOCOL_VERSION,'release_ready':False,
            'release_status':'experimental; not certified for a public leaderboard',
            'release_blockers': failures + [
                'Read-only verifier inputs and consistency checks do not isolate candidate code from the test/report process.',
                'Fresh source/model-specific calibration and repeated multi-model execution evidence are required.',
                'Semantic code counterexamples for executable probes and independent human judge audits remain required.',
                'Founder revocation binding is quarantined; Obsidian original-task metrics remain excluded.',
            ],
            'task_count':len(specs.task_paths()),'persona_count':len(personas),'pair_count':len(pairs),
            'active_personas':sum(r['registered_pairs']>0 for r in rows),
            'requirement_count':sum(p['requirements'] for p in pairs),
            'templates_bound':len({t for p in pairs for t in p['templates']}),
            'pairing_statuses':dict(Counter(c['status'] for c in specs.pairing_plan())),
            'retired_bindings':[p.name for p in sorted((root/'data/retired_bindings').glob('*.json'))],
            'personas':rows,'pairs':pairs}


def markdown(data):
    lines=['# Benchmark readiness and coverage','',
           f"Protocol: `{data['protocol']}`. **{data['release_status']}**.",'',
           'Registration is not calibration; calibration is not an agent run. Counts below never infer a pass from missing artifacts.','',
           f"{data['task_count']} task records; {data['persona_count']} personas in the unchanged pool; "
           f"{data['active_personas']} with active bindings; {data['pair_count']} pairs; "
           f"{data['requirement_count']} requirements; {data['templates_bound']}/30 templates instantiated somewhere.",'',
           '| Persona | Registered pairs | Templates bound | Calibration receipts matching source | Current valid pivot runs (pairs) | Human-audited pairs |',
           '|---|---:|---:|---:|---:|---:|']
    for r in data['personas']:
        lines.append(f"| {r['persona_id']} | {r['registered_pairs']} | {r['templates_bound']}/{r['templates_total']} | {r['calibrated_pairs']} | {r['executed_pairs']} | {r['audited_pairs']} |")
    lines += ['','## Release blockers',''] + ['- '+x for x in data['release_blockers']]
    lines += ['','## Every registered pair','',
              '| Task | Persona | Requirements | Registered negative controls | Calibration | Saved episodes |',
              '|---|---|---:|---:|---|---:|']
    for p in data['pairs']:
        lines.append(f"| {p['task_id']} | {p['persona_id']} | {p['requirements']} | {p['negative_controls']} | {p['calibration']} | {len(p['executions'])} |")
    return '\n'.join(lines)+'\n'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,help='New local output directory; omit for read-only JSON on stdout')
    args=parser.parse_args();data=inventory()
    if args.out:
        args.out.mkdir(parents=True,exist_ok=False)
        (args.out/'readiness.json').write_text(json.dumps(data,indent=2)+'\n')
        (args.out/'readiness.md').write_text(markdown(data))
        print(args.out/'readiness.md')
    else: print(json.dumps(data,indent=2))


if __name__=='__main__':main()
