"""Offline cohort admission inventory. Never calls models or the original evaluator."""
import json
from collections import Counter
from .specs import ROOT, compile_episode
from .staged import load_plan, adapt_episode, template_coverage, VERSION
from .negative_controls import controls_for
from .report_policy import exclusion_reason


def audit():
    rows=[]
    for path in sorted((ROOT/'data/staged_bindings').glob('*.json')):
        raw=json.loads(path.read_text())
        row={'task_id':raw['task_id'],'persona_id':raw['persona_id'],'specification_valid':False}
        try:
            episode=compile_episode(raw['task_id'],raw['persona_id'])
            plan=load_plan(episode);adapted=adapt_episode(episode)
            coverage=template_coverage(plan)
            if not plan.get('binding_review'):raise ValueError('Missing pair review')
            row.update(specification_valid=True,task_disclosure_mode=plan['task_disclosure_mode'],persona_disclosure_mode=plan['persona_disclosure_mode'],stages=[s['id'] for s in plan['stages']],
                implemented_templates=sum(x['implemented'] for x in coverage),source_templates=len(coverage),
                pending_templates=[x['template_id'] for x in coverage if x['status']=='pending_fixture'],
                blockers=plan['blocked_reasons'],calibration_admissible=not plan['blocked_reasons'],
                validation_level='offline_specification_only',review_required=True,execution_admitted=False,
                calibration_policy='optional_development_test',model_calibration_verified=False,
                scored_execution_verified=False,negative_controls=len(controls_for(adapted.binding.raw)),
                original_score_exclusion=exclusion_reason(episode.task.id))
        except (ValueError,KeyError,TypeError) as exc:
            row['error']=str(exc)
        rows.append(row)
    return {'protocol':VERSION,'specifications_valid':all(r['specification_valid'] for r in rows),
        'pairs':len(rows),'full_template_pairs':sum(r.get('implemented_templates')==r.get('source_templates',-1) for r in rows),
        'implemented_templates':sum(r.get('implemented_templates',0) for r in rows),
        'source_templates':sum(r.get('source_templates',0) for r in rows),
        'blocked_pairs':sum(bool(r.get('blockers')) for r in rows),
        'task_disclosure_modes':dict(Counter(r.get('task_disclosure_mode','invalid') for r in rows)),
        'notice':'Offline validation is not model calibration, trajectory validation, or a passed benchmark.',
        'bindings':rows}


if __name__=='__main__':
    result=audit();print(json.dumps(result,indent=2))
    if not result['specifications_valid']:raise SystemExit(1)
