"""Per-task entrypoint for existing DeepSWE checks on a trusted host evidence bundle.

No shell/probe is executed here. Supply actual host snapshots/probe records, not
candidate-authored substitutes. LLM checks require explicit --allow-llm; without
it the existing engine keeps unverified semantic judgments unknown.
"""
import argparse
import json
import os
from pathlib import Path


def evaluate(requirement, lifecycle, evidence, client=None):
    from .checks_staged import early_check, candidate_check
    for field in ('snapshot', 'initial', 'history', 'turns'):
        if field not in evidence: raise ValueError('Missing host evidence field: ' + field)
    if lifecycle['before_implementation']:
        if evidence.get('phase') != 'before_implementation':
            raise ValueError('Early checks require evidence frozen before implementation')
        result = early_check(requirement, evidence['snapshot'], evidence['initial'],
            evidence['history'], evidence['turns'], client, evidence.get('baseline_source'),
            evidence.get('authorization'), evidence.get('base', {}))
    else:
        result = candidate_check(requirement, evidence['snapshot'], evidence['initial'],
            evidence.get('validations', []), evidence['history'], evidence['turns'],
            evidence.get('trusted', {}), evidence.get('base', {}), client,
            early=evidence.get('early'), baseline_source=evidence.get('baseline_source'),
            public_contract=evidence.get('public_contract'), authorization=evidence.get('authorization'),
            observed_change=evidence.get('observed_change'))
    return {'requirement_id': requirement['id'], 'legacy_verdict': result['status'],
            'result': result, 'llm_enabled': client is not None,
            'independent_objective_checker': False, 'protocol': 'DeepSWE staged checker',
            'scope': 'Existing mixed-checker evaluation over caller-supplied trusted host evidence'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--requirement'); p.add_argument('--evidence', type=Path)
    p.add_argument('--describe', action='store_true')
    p.add_argument('--allow-llm', action='store_true'); p.add_argument('--env-file', type=Path)
    a = p.parse_args()
    from .task import EPISODE
    from .check_contracts import checker_route, planning_evidence_contract
    plan = EPISODE.binding.raw['staged']; requirements = {r['id']: r for r in EPISODE.requirements}
    if a.describe:
        value = {'task_id': EPISODE.task.id, 'persona_id': EPISODE.persona['id'],
            'requirements': [{'requirement_id': r['id'], 'route': checker_route(r, plan['requirements'][r['id']]),
                              'evidence_contract': planning_evidence_contract(r, plan['requirements'][r['id']])}
                             for r in requirements.values()], 'evidence_schema': {
            'required': ['snapshot', 'initial', 'history', 'turns'],
            'phase': 'before_implementation required for early checks',
            'optional_by_route': ['validations', 'trusted', 'base', 'early', 'baseline_source',
                                  'public_contract', 'authorization', 'observed_change']}}
    else:
        if a.requirement not in requirements or not a.evidence: p.error('Provide a known --requirement and --evidence')
        client = None
        if a.allow_llm:
            if not a.env_file: p.error('--allow-llm requires --env-file')
            os.environ['DS_PERSONA_ENV_FILE'] = str(a.env_file.resolve())
            import time
            from .api import Client
            client = Client(max_calls=plan['budgets']['final_review_calls'],
                            deadline=time.monotonic() + plan['budgets']['final_review_seconds'])
        value = evaluate(requirements[a.requirement], plan['requirements'][a.requirement],
                         json.loads(a.evidence.read_text()), client)
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == '__main__': main()
