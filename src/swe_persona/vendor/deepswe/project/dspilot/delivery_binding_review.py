"""Attach actual static review evidence without claiming external certification."""
import hashlib
import json


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def inputs(binding, plan, persona):
    # Freeze refreshes advisory binding_review without changing executable rules.
    return {'binding_sha256':digest(binding), 'persona_sha256':digest(persona),
            'executable_plan_sha256':digest({k:v for k,v in plan.items() if k!='binding_review'})}


def attach(record, review, binding, plan, persona):
    if review is None:
        return record
    current=inputs(binding,plan,persona)
    rows=review.get('requirements',[])
    valid=(review.get('task_id')==record['task_id'] and review.get('persona_id')==record['persona_id']
           and review.get('inputs')==current and review.get('review_kind')=='agent_static_review'
           and len(rows)==len(plan['requirements'])
           and {r.get('requirement_id') for r in rows}==set(plan['requirements'])
           and all(r.get('finding') and r.get('remaining_semantic_checks') is not None for r in rows))
    return {**record, 'status':'reviewed_with_limitations' if valid else 'stale_or_incomplete_review',
            'review_evidence':review, 'review_evidence_sha256':digest(review),
            'review_inputs_match':valid, 'semantic_approval_claimed':False,
            'independent_certification':False,
            'reason':'Recorded agent static review; no live positive/negative certification is inferred.' if valid
                     else 'Review does not cover current inputs; re-review required. No status upgrade.'}
