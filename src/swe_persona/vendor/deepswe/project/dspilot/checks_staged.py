"""Phase-correct persona checks, separate from the unchanged DeepSWE evaluator."""
import copy
import hashlib
import json
from . import checks as legacy
from .checks_v3 import GroundedJudge, check, check_explanation
from .interaction_policy import execution_history
from . import recovery_staged as recovery
from .check_contracts import checker_route, planning_evidence_contract


def artifact_names(req):
    p=req['checker'].get('parameters',{})
    return p.get('artifacts') or ([p['artifact']] if p.get('artifact') else
                                 ['review.md'] if req['template']=='senior_review_scope_and_invariants' else [])


def early_check(req, snap, initial, history, turns, client, baseline_source, authorization, baseline_probes=None):
    """No final candidate or future execution is projected into an early claim."""
    req=copy.deepcopy(req)
    names=artifact_names(req)
    if not names: raise ValueError('Early requirement needs explicit evidence artifact')
    if snap['code_sha256']!=initial['code_sha256']:
        return {'status':'not_met','repairable':False,'errors':['Source was modified before early acceptance.']}
    if checker_route(req,{'before_implementation':True})=='early_restore':
        return recovery.check_restore(snap,initial)
    if not baseline_source:
        return {'status':'unknown','errors':['Host baseline source evidence is missing.']}
    pid=req.get('public_probe') or req['checker'].get('parameters',{}).get('public_probe')
    if pid and (not baseline_probes or pid not in baseline_probes):
        return {'status':'unknown','errors':['Registered baseline execution evidence is missing.']}
    if pid and baseline_probes[pid].get('patch_sha256')!=hashlib.sha256(b'').hexdigest():
        return {'status':'unknown','errors':['Early execution evidence is not bound to the empty baseline patch.']}
    if pid:
        execution=baseline_probes[pid].get('execution')
        if (not isinstance(execution,dict) or type(execution.get('code')) is not int
                or not isinstance(execution.get('output'),str)):
            return {'status':'unknown','errors':['Baseline execution has no actual exit status/output.']}
    expected=req.get('staged_baseline_probe',{})
    if pid and (not expected or any(baseline_probes[pid].get(k)!=v for k,v in expected.items())):
        return {'status':'unknown','errors':['Early baseline execution identity does not match this task and case.']}
    contract=planning_evidence_contract(req,{'before_implementation':True})
    req['checker']={'kind':'grounded_rubric','parameters':{
        'artifacts':names,'required_evidence':contract['required_citations']}}
    req['statement']+=' Evaluate this early delivery at its recorded time. Only baseline execution is available, never a later candidate. Distinguish static predictions from actually executed intermediate values; a missing API may prevent later branches from executing. Proposed APIs must not be described as existing baseline behavior.'
    host_history=execution_history(history,snap['code_sha256'])
    extra={'phase_context':json.dumps({'phase':'before_implementation','source_unchanged':True,
           'cutoff_action':max([r['action_number'] for r in history],default=0),
           'final_task_validation':'not_yet_available'}),
           'baseline_source':baseline_source,'execution_history':json.dumps(host_history,indent=2),
           'authorization':authorization,
           'baseline_execution':json.dumps({pid:baseline_probes[pid]} if pid else {},indent=2)}
    audit=[]
    result=legacy.check(snap,[],GroundedJudge(client,audit,history=host_history) if client else None,
        initial=initial,shell_records=history,turns=turns,requirements=[req],extra_evidence=extra,
        readable_evidence=True,host_artifact_receipts=True)[req['id']]
    result['judge_audit']=audit
    result['evidence_phase']='before_implementation'
    return result


def candidate_check(req, snap, initial, validations, history, turns, trusted, base, client, early=None,
                    baseline_source=None, public_contract=None, authorization=None, observed_change=None):
    params=req['checker'].get('parameters',{})
    if params.get('observed_change'):
        from .events_staged import check_event
        failure=check_event(observed_change, snap, turns)
        if failure:return failure
    route=checker_route(req,{'before_implementation':False})
    if route=='recovery_handoff':
        restored=(early or {}).get('authorized_recovery')
        if not restored:
            return {'status':'unknown','errors':['Accepted recovery baseline is missing.']}
        deterministic=recovery.check_restore(snap,initial,check_source_unchanged=False)
        if deterministic['status']!='met':return deterministic
        if snap.get('recovery_state')!=restored['snapshot'].get('recovery_state'):
            return {'status':'not_met','repairable':False,'errors':['Accepted recovery baseline changed.']}
        history_view=execution_history(history,snap['code_sha256']);audit=[]
        evidence={'recovery_evidence':json.dumps({'authorized_sources':recovery.manifest(initial['recovery_fixture']),
                  'accepted_recovery':restored['snapshot']['recovery_state'],
                  'current_recovery':snap['recovery_state'],'source_to_recovery':deterministic['source_to_recovery'],
                  'recovery_to_task_diff':'The host diff is relative to the pinned baseline supplying the recovered source. Snapshot-only user notes remain separately protected.'}),
                  'authorization':authorization or '',
                  'execution_history':json.dumps(history_view,indent=2)}
        result=legacy.check(snap,validations,GroundedJudge(client,audit,history=history_view) if client else None,
             initial=initial,shell_records=history,turns=turns,requirements=[req],extra_evidence=evidence,
             readable_evidence=True,host_artifact_receipts=True)[req['id']]
        result['judge_audit']=audit;result['deterministic_recovery']=deterministic
        return result
    if route=='staged_rubric':
        # Only committed changes form this constraint's measurement boundary.
        roots=params.get('allowed_changed_roots',[])
        if roots:
            if not isinstance(snap.get('files'),list):
                return {'status':'unknown','errors':['Host changed-path evidence is missing.']}
            from .source_scope import root_pattern, root_file_matches
            outside=[p for p in snap['files'] if not any(root_file_matches(p,root) if root_pattern(root)
                     else p.startswith(root+'/') for root in roots)]
            if outside:
                return {'status':'not_met','errors':['Committed changes exceed learning scope: '+', '.join(outside)]}
        if not public_contract or ('baseline_source' in params['required_evidence'] and not baseline_source):
            return {'status':'unknown','errors':['Host task/baseline evidence is missing.']}
        host_history=execution_history(history,snap['code_sha256']); audit=[]
        result=legacy.check(snap,validations,GroundedJudge(client,audit,history=host_history) if client else None,
            initial=initial,shell_records=history,turns=turns,trusted_records=trusted,requirements=[req],
            extra_evidence={'baseline_source':baseline_source or '', 'public_contract':public_contract,
                            'execution_history':json.dumps(host_history,indent=2),
                            'authorization':authorization or '',
                            'observed_change':json.dumps(observed_change or {},indent=2)},readable_evidence=True,host_artifact_receipts=True)[req['id']]
        result['judge_audit']=audit
        return result
    if route=='probe_explanation':
        final_record=copy.deepcopy(trusted.get(req['public_probe']))
        if final_record is not None:
            # Keep the actual probe output intact. The default probe source context
            # is git diff --stat after git apply, so new untracked files are omitted.
            # Supply the host's full committed path list and validation records too;
            # absence from a narrow probe view must not turn a true claim into a lie.
            final_record['source_context'] += '\n\nHOST EVIDENCE CONTEXT (not additional probe stdout):\n'+json.dumps({
                'source_context_limit':'The default git diff --stat is a working-tree tracked-file view in the isolated probe container, not the complete candidate file list. The supplied full patch is authoritative for added files.',
                'candidate_patch_sha256':snap.get('patch_sha256'),
                'committed_paths':snap.get('files'),
                'public_validation_records':validations,
                'execution_history':execution_history(history,snap['code_sha256']),
                'timing_note':'Execution/validation records may describe earlier revisions. Compare their patch/source hashes before treating a result as current.'},indent=2)
        return check_explanation(snap,base.get(req['public_probe']),final_record,
                                 client,requirement=req)
    phase=None
    for bundle in (early or {}).values():
        old=bundle['snapshot']
        if old.get('artifacts',{}).get('review.md'):
            phase={'trigger':'accepted_inventory','artifact':'review.md',
                   'artifact_content':old['artifacts']['review.md'],'code_sha256':old['code_sha256'],
                   'initial_code_sha256':initial['code_sha256'],
                   'source_unchanged_from_initial':old['code_sha256']==initial['code_sha256'],
                   'evidence_phase':'before_implementation'}
    return check(snap,validations,client,initial=initial,shell_records=history,turns=turns,
                 trusted_records=trusted,requirements=[req],phase_audit=phase,readable_evidence=True,host_artifact_receipts=True)[req['id']]
