"""Host-observed verification updates after an actual manager recommendation.

A newly measured independent result is a change in verification evidence, not
proof that functional behavior changed. The same recommendation may remain valid.
"""
import copy
import hashlib
import json


def identity(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def capture(plan, snapshot, turns, action_number, evaluate, directory):
    fixture=plan['observed_change_fixture']
    previous=next((t for t in reversed(turns) if t['speaker']=='agent' and t.get('phase')=='done'),None)
    if not previous:raise ValueError('Observed-change event needs a delivered prior recommendation')
    result=evaluate(snapshot['patch'],directory)
    if result.get('patch_sha256')!=snapshot['patch_sha256'] or result.get('status') not in ('passed','failed'):
        raise ValueError('Observed event has no trustworthy matching public validation')
    record={'kind':fixture['kind'],'validation_id':fixture['validation_id'],
            'previous_state':fixture['previous_state'],'previous_recommendation':copy.deepcopy(previous),
            'after_action':action_number,'disclose_turn':previous['turn']+1,
            'patch_sha256':snapshot['patch_sha256'],'result':result,
            'meaning':'New independent public validation after the recommendation; not evidence of changed functional behavior or hidden-task success.'}
    return record | {'event_id':identity(record)}


def public_view(event):
    if not event:return None
    result=event['result']
    return {k:event[k] for k in ('event_id','meaning','previous_state','previous_recommendation','patch_sha256','disclose_turn')} | {
        'new_public_validation':{k:result[k] for k in ('record_id','status','code','patch_sha256') if k in result},
        'output_excerpt':result.get('output_tail','')[-2400:]}


def check_event(event, snapshot, turns):
    if not event or event.get('event_id')!=identity({k:v for k,v in event.items() if k!='event_id'}):
        return {'status':'unknown','errors':['Missing or inconsistent host change event.']}
    previous=event.get('previous_recommendation',{})
    if (previous not in turns or previous.get('phase')!='done'
            or event['disclose_turn']<=previous.get('turn',0)
            or event['result'].get('patch_sha256')!=event['patch_sha256']):
        return {'status':'unknown','errors':['Event recommendation, chronology or patch identity is inconsistent.']}
    if not any(t['speaker']=='agent' and t.get('turn',0)>=event['disclose_turn'] for t in turns):
        return {'status':'not_met','errors':['No recommendation was delivered after the observed event.']}
    # The original persona asks for a user-facing update, not a separate file
    # or a literal event hash. The semantic judge compares the actual reply,
    # previous recommendation and host event supplied as distinct evidence.
    return None
