"""Opt-in staged episodes. Select DS_PROTOCOL=staged before importing runtime."""
import copy
import hashlib
import json
import os
import time
from pathlib import Path
from .api import Client, ApiFailure, public_routes
from .coding_budget import CodingBudget, CodingBudgetExceeded, coding_seconds
# Re-export the original entry points for existing callers and transport tests.
from .coding_native import AGENT_PROTOCOL, request_native_action
from .coding_backend import create_backend, backend_metadata
from .protocol import dump
from .task import EPISODE, BINDING_SPEC, TASK_SPEC, CONTRACT, PERSONA, ROOT, BASE, SOURCE_ROOTS, RESOURCES
from .sandbox import Box, evaluate
from .full_persona import full_snapshot
from .probes_staged import public_probe
from .fingerprint import episode_manifest
from .checks_staged import early_check, candidate_check, artifact_names
from .checks_v3 import JudgeOutputError
from . import requirement_judge
from .staged import VERSION, Controller, template_coverage, disclosed_task, new_task_documents
from . import simulator_staged as user_simulator
from . import recovery_staged as recovery
from . import events_staged as observed
from .planning_staged import require_execution_admission
from .report_policy import exclusion_reason, VERSION as REPORT_POLICY_VERSION
from . import source_scope


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()



def environment(authorized):
    protected=', '.join(x['path'] for x in TASK_SPEC.protected_files)
    return (f'Repository /app, pinned baseline {BASE}. Network disabled. '
        f'Resources: {RESOURCES.get("cpus",2)} CPUs, {RESOURCES.get("memory","4g")} memory. '
        f'User-owned files must remain unchanged: {protected}. '
        f'Eventual task source scope: {", ".join(source_scope.describe_root(root,recursive=False) for root in SOURCE_ROOTS)}. '
        'Only those paths may be changed in the task patch. Documentation, tests and configuration outside those paths remain out of scope even if related to the feature. '
        'No remote operations. Only committed task changes can be scored. '
        'All requested evidence artifact filenames are relative to /deliverables, outside /app. '
        'Do not put evidence documents in the repository or include them in task commits. '
        'validate executes the registered public validation on a clean copy of your committed patch. '
        'Hidden tests and solutions are unavailable during interaction. '
        + ('Local task edits and commits are now authorized.' if authorized else
           'Repository source edits and commits are NOT authorized yet. Inspect source and write requested evidence only under /deliverables.'))


def collect_baseline_source(box):
    # A bounded public-source excerpt, not reference material. Additional read-only
    # inspection remains available and its actual execution enters the evidence.
    limits={}
    for req in EPISODE.requirements:
        params=req['checker'].get('parameters',{})
        limit=params.get('baseline_file_chars',12000)
        if type(limit) is not int or not 12000<=limit<=90000:
            raise ValueError('Baseline file evidence limit must be an integer between 12000 and 90000')
        for path in params.get('baseline_paths',[]):limits[path]=max(limits.get(path,12000),limit)
    priority=list(limits)
    script=Path(source_scope.__file__).read_text()+'\nimport pathlib,json; roots='+repr(SOURCE_ROOTS)+'; priority='+repr(priority)+'; limits='+repr(limits)+'''; result={}; used=0
files=[pathlib.Path(p) for p in priority]
files.extend(source_files(pathlib.Path('.'),roots,ordered=True))
for f in files:
 if str(f) in result or not f.is_file() or f.is_symlink() or '__pycache__' in f.parts: continue
 if used>=90000: break
 try: text=f.read_text()
 except (UnicodeError,OSError): continue
 text=text[:min(limits.get(str(f),12000),90000-used)]; result[str(f)]=text; used+=len(text)
print(json.dumps({'public_source_excerpts':result,'bounded_excerpt_not_complete_repository':True}))'''
    import shlex
    r=box.shell('python3 -c '+shlex.quote(script))
    if r['code']: raise RuntimeError('Unable to collect baseline public source')
    result=json.loads(r['output'])
    plan=EPISODE.binding.raw.get('staged')
    if plan:
        result['disclosed_public_contract']=disclosed_task(plan,TASK_SPEC.contract_path.read_text(),'S1')
    return json.dumps(result)



def collect_online_resource_evidence(controller, plan, requirements, probes, snapshot,
                                     completion_claim, trusted, probe_cache, out, execute):
    """Measure disclosed workloads before their gates, without deciding verdicts.

    The simulator delivers the public receipt on its next user turn. The online
    dialogue judge still cannot read private host records or final checker votes.
    One measurement per committed patch/probe; failures are not resampled.
    """
    from .staged import pending_check_reason
    patch_id = snapshot['patch_sha256']
    # A prior patch's receipt must not be presented as current after an edit.
    for pid in list(trusted):
        if pid in probes and probes[pid].get('resource_measurement'):
            if trusted[pid]['patch_sha256'] != patch_id:
                del trusted[pid]
    if not completion_claim or not controller.implementation_authorized:
        return []
    collected = []
    for key in sorted(controller.disclosed):
        life = plan['requirements'][key]
        if life['before_implementation'] or pending_check_reason(
                life, controller.stage['id'], completion_claim, False):
            continue
        req = requirements[key]
        pid = req.get('public_probe') or req['checker'].get('parameters', {}).get('public_probe')
        if not pid or not probes[pid].get('resource_measurement'):
            continue
        cache_key = (pid, patch_id)
        if cache_key not in probe_cache:
            probe_cache[cache_key] = execute(snapshot['patch'],
                out / f'probe-candidate-{len(probe_cache)+1:03d}', probes[pid])
        trusted[pid] = probe_cache[cache_key]
        if pid not in collected:
            collected.append(pid)
    return collected


def episode(run_id, arm, client_factory=Client, *, box_factory=None, backend_factory=None, tool_executor=None, official_evaluator=None):
    if arm not in ('baseline','control','pivot'): raise ValueError('Unknown arm')
    admission=require_execution_admission(EPISODE)
    from .runtime_backend import selected_runtime
    if selected_runtime() in ('pier','pier_native') and box_factory is None:
        raise ValueError('Pier selection requires the Pier trial entrypoint')
    plan=BINDING_SPEC.raw['staged']; pivot=arm=='pivot'; baseline=arm=='baseline'
    staged_interaction=pivot or (arm=='control' and plan['task_disclosure_mode']=='progressive')
    if plan['blocked_reasons']: raise ValueError('Staged binding blocked: '+'; '.join(plan['blocked_reasons']))
    manifest=episode_manifest(EPISODE)
    from . import delivery_capture
    delivery_blur=delivery_capture.from_plan(os.environ.get('DS_STAGED_PLAN_PATH'), EPISODE.task.id)
    out=ROOT/'runs'/run_id; out.mkdir(parents=True,exist_ok=False)
    start=time.monotonic(); budgets=plan['budgets']; timings={}
    coding_budget=CodingBudget(coding_seconds(plan))
    def coding_operation(kind, function):
        try: return coding_budget.run(kind, function)
        finally: dump(out/'coding-budget.json',coding_budget.receipt())
    client=client_factory(out/'api.jsonl',max_calls=budgets['interaction_calls'],deadline=start+budgets['interaction_seconds'])
    control_plan=copy.deepcopy(plan)
    if not pivot:
        control_plan.pop('initial_exchange',None)
        control_plan.pop('early_authorization_exchange',None)
        control_plan['requirements']={}
        control_plan['persona_disclosure_mode']='none'
        control_plan['implementation_authorization']='S1'
        for stage in control_plan['stages']:
            stage.update(requirements=[],gate=[],exit_event='final_delivery' if stage['id']=='S4' else 'completion_claim')
    controller=Controller(plan if pivot else control_plan)
    opening_controller=copy.deepcopy(controller); opening_controller.enter(1,0)
    requirements={r['id']:r for r in EPISODE.requirements}
    turns=[]; records=[]; validations=[]; trusted={}; bases={}; probe_cache={}; turn_spans=[]
    frozen={}; check_cache={}; runbook_cache={}; actions=0; tool_actions=0; agent_replies=0; turn=0
    initial=None; snapshot=None; error=None; final_error=None; reason=None; last_reply=''
    checks={}; first_patch=False; final_delivery=False; source=''; interaction_end=None; interaction_start=None
    rejected_agent_responses=[]
    simulator_audit=[]; pending_controller=None; artifact_interrupts=set(); completion_claim=False
    observed_change=None
    identity={'run_id':run_id,'task_id':EPISODE.task.id,'persona_id':PERSONA['id']}
    disclosures={}; online_judge_records=[]
    online_rules={key:requirement_judge.rubric(req,PERSONA) for key,req in requirements.items()} if pivot else {}
    dump(out/'harness-manifest.json',manifest)
    dump(out/'config.json',{'execution_admission':admission,'calibration_status':admission['calibration_status'],
        'protocol_version':VERSION,'online_judge_protocol':requirement_judge.VERSION if pivot else None,
        'binding_protocol':plan['protocol'],'coding_backend':backend_metadata(),
        'task_id':EPISODE.task.id,'persona_id':EPISODE.persona['id'],
        'arm':arm,'task_disclosure_mode':plan['task_disclosure_mode'] if staged_interaction else 'full_at_start',
        'persona_disclosure_mode':'staged' if pivot else 'none','plan':plan,'models':{k:v for k,v in client.cfg.items() if k.endswith('_MODEL')},
        'api_routes':public_routes(client.cfg),
        'resources':RESOURCES,'simulator_mode':'Model-authored user dialogue after controller review; exact public specifications preserved',
        'control_interpretation':plan['control']['interpretation'],'budgets':budgets})

    def snapshot_now(box):
        value=recovery.snapshot(box,full_snapshot(box),plan) if pivot else full_snapshot(box)
        if plan.get('task_workflow'):
            value['task_workflow']=box.workflow_snapshot()
            value['snapshot_sha256']=digest({k:v for k,v in value.items() if k!='snapshot_sha256'})
        return value

    def permissions(target):
        text=environment(target.implementation_authorized if pivot else True)
        if pivot and plan.get('recovery_fixture'):text+=' '+recovery.permission(target.stage['id'])
        return text

    def save():
        dump(out/'turns.json',turns); dump(out/'actions.json',records)
        dump(out/'stage-state.json',controller.summary() if staged_interaction else {'policy':'direct' if baseline else 'neutral_task_from_start'})
        dump(out/'early-evidence.json',frozen)
        dump(out/'observed-change.json',observed_change)
        dump(out/'simulator-dialogue.json',simulator_audit)
        if pivot:
            dump(out/'requirement-disclosures.json',{'identity':identity,'events':disclosures})
            dump(out/'online-rubrics.json',online_rules)
            dump(out/'online-judge-records.json',online_judge_records)

    def append_message(speaker,content,**metadata):
        event={**identity,'event_id':f'{run_id}:message-{len(turns)+1:06d}',
               'turn':turn,'speaker':speaker,'user_visible':True,'content':content,**metadata}
        turns.append(event)
        if speaker=='user' and pivot:
            for key in controller.disclosed:
                disclosures.setdefault(key,event['event_id'])

    def user_message(target, receipt, *, opening=False, previous=None, focus_ids=()):
        environment_permissions=permissions(target)
        remaining_budget={'user_turns':budgets['max_user_turns']-turn,
                          'actions':budgets['max_actions']-actions}
        # These facts must be delivered by the simulator, just like the task
        # specification. Missing or changed blocks fail rendering; no host fallback.
        required=[environment_permissions,
            f'At the start of user turn {turn+1}/{budgets["max_user_turns"]}, '
            f'{remaining_budget["actions"]} agent actions and '
            f'{remaining_budget["user_turns"]} user turns remain in this episode, '
            'including this turn. These are episode-wide limits.']
        if opening and pivot and delivery_blur:
            required.append(delivery_blur['blur']['opening'])
        if pivot and opening and PERSONA['id']=='first_time_pr_contributor':
            required.append('Initial workspace observations recorded before any agent action: '+
                            json.dumps(user_simulator.public_initial_workspace(initial),sort_keys=True))
        if pivot and opening and plan.get('initial_exchange') in ('plan_then_requirements','plan_then_inventory','plan_then_review'):
            required.append(plan['opening'])
        if pivot and target.awaiting_initial_plan:
            required.append(plan.get('initial_plan_request') or 'Please inspect the disclosed task and give me a short implementation plan first. Do not edit repository source or commit yet; wait for my next request before implementation.')
        task_visible=not staged_interaction or target.task_disclosed
        if staged_interaction and (opening or previous is not None):
            required.extend(new_task_documents(plan,CONTRACT,target.disclosed_task_units,
                                               previous.disclosed_task_units if previous is not None else ()))
        elif not staged_interaction and opening:
            required.append(CONTRACT)
        if staged_interaction and not target.full_task_disclosed:
            required.append('Work on the task details disclosed so far. Later user requests will supply the remaining task specifications; do not infer undisclosed acceptance criteria. A feature or format name in an API list does not require its as-yet-undisclosed behavior at this stage. You may revise internals later while preserving previously disclosed behavior.')
        if pivot:
            new_keys=target.disclosed-(previous.disclosed if previous is not None else set()) if (opening or previous is not None) else set()
            for key in sorted(new_keys):
                req=requirements[key]; required.append(req['statement'])
                pid=req.get('public_probe') or req['checker'].get('parameters',{}).get('public_probe')
                if pid:
                    probe=BINDING_SPEC.public_probes[pid]
                    required.append(json.dumps({k:probe[k] for k in ('id','filename','command','timeout_seconds','execution_scope','resource_measurement') if k in probe},sort_keys=True))
                    required.append(probe['script'])
                    for extra in probe.get('extra_files',[]):
                        required.extend([extra['filename'],extra['script']])
            if 'manager_update' in new_keys:
                required.append(json.dumps(observed.public_view(observed_change),sort_keys=True))
            if 'authorized_recovery' in new_keys and initial.get('recovery_fixture'):
                required.append(json.dumps(recovery.manifest(initial['recovery_fixture']),sort_keys=True))
        context={'operation':'render','mode':'persona' if pivot else 'neutral',
            'stage':target.stage['id'] if staged_interaction else 'task',
            'opening':plan['opening'] if pivot and opening else None,
            'preferences':user_simulator.dialogue_style(PERSONA['id']) if pivot else {},
            'requirements':{k:plan['requirements'][k]['statement'] for k in sorted(target.disclosed)} if pivot else {},
            'checks':user_simulator.public_checks(target) if pivot else {},
            'task_checks':user_simulator.public_task_checks(target),
            'public_probe_results':user_simulator.public_probe_results(trusted) if pivot else {},
            'public_baseline_probe_results':user_simulator.public_baseline_probe_results(bases,requirements,target.disclosed,BASE) if pivot else {},
            'controller_receipt':receipt,'focus_ids':list(focus_ids),
            'task_disclosed':task_visible,
            'task_disclosure_mode':plan['task_disclosure_mode'] if staged_interaction else 'full_at_start',
            'full_task_disclosed':target.full_task_disclosed if staged_interaction else True,
            'disclosed_task_content':disclosed_task(plan,CONTRACT,target.stage['id']) if staged_interaction else CONTRACT,
            'implementation_authorized':target.implementation_authorized if pivot else True,
            'awaiting_initial_plan':target.awaiting_initial_plan if pivot else False,
            'awaiting_early_authorization':target.awaiting_early_authorization if pivot else False,
            'environment_permissions':environment_permissions,
            'acceptance':{k:plan['requirements'][k]['acceptance'] for k in target.disclosed} if pivot else {},
            'deferred_until_S4':[k for k in target.disclosed if plan['requirements'][k].get('not_before_stage')=='S4'] if pivot else [],
            'required_content':required,
            'observed_change':observed.public_view(observed_change) if pivot and 'manager_update' in target.disclosed else None,
            'remaining_budget':remaining_budget,
            **user_simulator.public_observations(turns,records)}
        feedback=user_simulator.feedback_document(context)
        if feedback:required.append(feedback)
        try: return user_simulator.render(client,context,simulator_audit)
        finally: dump(out/'simulator-dialogue.json',simulator_audit)

    def evaluate_requirements(final_client=None, final=False):
        from .staged import pending_check_reason
        result={}; judge=final_client or client
        for key in sorted(controller.disclosed):
            req=requirements[key]; life=plan['requirements'][key]
            pending=pending_check_reason(life,controller.stage['id'],completion_claim,final)
            if pending:
                result[key]={'status':'pending','reason':pending}
                continue
            if not final:
                # Online judges never consume host snapshots, tools or checker results.
                # Preserve early host evidence separately for the final checker.
                if life['before_implementation']:
                    pid=req.get('public_probe') or req['checker'].get('parameters',{}).get('public_probe')
                    if pid and pid not in bases:
                        bases[pid]=public_probe('',out/f'probe-base-{pid}',BINDING_SPEC.public_probes[pid])
                try:
                    value=requirement_judge.judge(judge,online_rules[key],turns,disclosures[key],identity,
                        disclosed_task(plan,CONTRACT,controller.stage['id']),PERSONA['id'],
                        audit_callback=lambda audit: dump(out/f'online-judge-attempt-{len(online_judge_records)+1:04d}.json',audit))
                except requirement_judge.OnlineJudgeError as exc:
                    online_judge_records.append({'turn':turn,'requirement_id':key,'status':'protocol_error','audit':exc.audit})
                    raise
                else:
                    online_judge_records.append({'turn':turn,'requirement_id':key,'judgment':copy.deepcopy(value)})
                finally:
                    # Preserve earlier judgments even if a later requirement fails.
                    dump(out/'online-judge-records.json',online_judge_records)
                if value['verdict']=='MET' and life['before_implementation'] and key not in frozen:
                    frozen[key]={'snapshot':copy.deepcopy(snapshot),'history':copy.deepcopy(records),
                                 'turns':copy.deepcopy(turns),'authorization':permissions(controller)}
                result[key]=value
                continue
            if life['before_implementation']:
                pid=req.get('public_probe') or req['checker'].get('parameters',{}).get('public_probe')
                if pid and pid not in bases:
                    bases[pid]=public_probe('',out/f'probe-base-{pid}',BINDING_SPEC.public_probes[pid])
                bundle=frozen.get(key)
                if bundle is None:
                    bundle={'snapshot':copy.deepcopy(snapshot),'history':copy.deepcopy(records),
                            'turns':copy.deepcopy(turns),'authorization':permissions(controller)}
                version=digest(bundle)
                cache_key=(key,version)
                if not final and cache_key in check_cache: value=check_cache[cache_key]
                else:
                    value=early_check(req,bundle['snapshot'],initial,bundle['history'],bundle['turns'],judge,source,bundle['authorization'],bases)
                    check_cache[cache_key]=value
                if value['status']=='met' and key not in frozen: frozen[key]=bundle
            else:
                pid=req.get('public_probe') or req['checker'].get('parameters',{}).get('public_probe')
                if pid:
                    probe=BINDING_SPEC.public_probes[pid]
                    if pid not in bases:
                        bases[pid]=public_probe('',out/f'probe-base-{pid}',probe)
                    cache_key=(pid,snapshot['patch_sha256'])
                    if cache_key not in probe_cache:
                        probe_cache[cache_key]=public_probe(snapshot['patch'],out/f'probe-candidate-{len(probe_cache)+1:03d}',probe)
                    trusted[pid]=probe_cache[cache_key]|{'baseline_record':bases[pid]}
                if req.get('staged_runbook_probe'):
                    from . import runbook_probe
                    bound=req['staged_runbook_probe']
                    cache_key=(pid,snapshot['patch_sha256'],digest(snapshot['artifacts'].get(bound['artifact'])))
                    if cache_key not in runbook_cache:
                        runbook_cache[cache_key]=runbook_probe.execute(snapshot,out/f'runbook-reproduction-{len(runbook_cache)+1:03d}',probe)
                    trusted[runbook_probe.key(bound)]=runbook_cache[cache_key]
                cache_key=(key,digest([snapshot,records,turns]))
                if not final and cache_key in check_cache: value=check_cache[cache_key]
                else:
                    value=candidate_check(req,snapshot,initial,validations,records,turns,trusted,bases,judge,early=frozen,
                                          baseline_source=source,public_contract=disclosed_task(plan,CONTRACT,controller.stage['id']),
                                          authorization=environment(controller.implementation_authorized),observed_change=observed_change)
                    check_cache[cache_key]=value
            result[key]=copy.deepcopy(value)
        return result

    backend=(backend_factory or create_backend)(client,out,rejected_agent_responses)
    message=None
    phase='working'
    try:
        with (box_factory or Box)(**getattr(backend,'box_kwargs',{})) as box, backend.lifecycle(box):
            setup_start=time.monotonic(); box.setup(); initial=snapshot_now(box)
            if pivot and plan.get('recovery_fixture'):
                initial['recovery_fixture']=recovery.capture(box,plan,BASE,initial)
                initial['snapshot_sha256']=digest({k:v for k,v in initial.items() if k!='snapshot_sha256'})
            snapshot=copy.deepcopy(initial)
            source=collect_baseline_source(box); dump(out/'initial-snapshot.json',initial)
            (out/'baseline-source.json').write_text(source)
            timings['setup_seconds']=time.monotonic()-setup_start
            try:
                opening_start=time.monotonic()
                try:
                    message,reused_opening=delivery_capture.opening_for_run(
                        os.environ.get('DS_STAGED_PLAN_PATH'), task_id=EPISODE.task.id,
                        persona_id=PERSONA['id'], arm=arm, plan=plan,
                        generate=lambda: user_message(opening_controller,{'proposal':None,'accepted':True,'outcome':'opening','reasons':[]},opening=True))
                finally:
                    timings['opening_simulator_seconds']=time.monotonic()-opening_start
                pending_controller=opening_controller
                if delivery_blur:
                    delivery_capture.freeze_opening(out,message,delivery_blur,
                        task_id=EPISODE.task.id,persona_id=PERSONA['id'],arm=arm,plan=plan,initial=initial,
                        models={k:v for k,v in client.cfg.items() if k.endswith('_MODEL')},
                        reused_from=reused_opening)
                for turn in range(1,budgets['max_user_turns']+1):
                    if actions>=budgets['max_actions']: reason='action_budget_exhausted'; break
                    if pending_controller is not None:
                        controller=pending_controller; pending_controller=None
                    delivered_at=time.monotonic()
                    if interaction_start is None: interaction_start=delivered_at
                    turn_spans.append({'turn':turn,'stage':controller.stage['id'] if staged_interaction else 'task',
                                       'start_seconds':delivered_at-start})
                    append_message('user',message,source='user_simulator')
                    if turn == 1 and delivery_blur:
                        delivery_capture.verify_opening(out/'delivery',task_id=EPISODE.task.id,
                            persona_id=PERSONA['id'],arm=arm,plan=plan,delivered_message=message)
                    backend.deliver_user(message); phase='working'; last_reply=''; completion_claim=False
                    save()
                    for _ in range(budgets['max_actions'] if baseline else 30):
                        if actions>=budgets['max_actions']: reason='action_budget_exhausted'; break
                        actions+=1
                        action,call_id,response=coding_operation('coding_model',lambda:backend.next_action(actions))
                        kind=action['action']; phase=action['phase']; before=box.code_hash(); new_first_patch=False
                        completion_claim=kind=='reply' and phase=='done'
                        if kind=='reply':
                            agent_replies+=1; last_reply=action['content']; value={'delivered':True}
                            append_message('agent',last_reply,phase=phase)
                        else:
                            tool_actions+=1
                            def execute_tool():
                                if kind=='validate':
                                    snap=full_snapshot(box)
                                    value=evaluate(snap['patch'],out/f'public-{len(validations)+1:03d}')
                                    validations.append(value)
                                    return value
                                if kind=='terminal': return backend.execute_terminal()
                                return box.shell(action['content'])
                            value=coding_operation('tool_'+kind,lambda:tool_executor(kind,execute_tool) if tool_executor else execute_tool())
                            after=box.code_hash()
                            records.append({'action_number':actions,'turn':turn,'action':action,'result':json.dumps(value),'code_before':before,'code_after':after})
                            new_first_patch=before!=after and not first_patch
                            first_patch=first_patch or new_first_patch
                        backend.observe(call_id,value)
                        if pivot:
                            premature_commit=(controller.awaiting_initial_plan and kind!='reply'
                                              and box.snapshot()['head']!=initial['head'])
                            controller.observe_action(turn,actions,kind!='reply' and (before!=after or premature_commit))
                            if kind!='reply':
                                protected=initial.get('protected_files',{})
                                controller.observe_protected(protected,{p:box.read('/app/'+p) for p in protected},turn,actions)
                                if any(r['checker'].get('parameters',{}).get('preserve_initial_workspace') for r in requirements.values()):
                                    from .workspace_evidence import preservation_errors
                                    violations=preservation_errors(initial.get('workspace_identity'),box.workspace_snapshot(),final=False)
                                    if violations:
                                        controller.end_reason='preexisting_workspace_violation'
                                        controller.environment_violations.extend(violations)
                                        controller.events.append({'event':'permanent_workspace_violation','errors':violations,'turn':turn,'action':actions})
                            if plan.get('recovery_fixture'):
                                state=recovery.observe(box)
                                violation=recovery.boundary_violation(state,controller.disclosed,frozen)
                                if violation:
                                    controller.end_reason=violation;controller.environment_violations.append(violation)
                                    controller.events.append({'event':'permanent_recovery_violation','reason':violation,'turn':turn,'action':actions})
                            if controller.end_reason: reason=controller.end_reason; break
                        save()
                        if kind=='reply' and (phase in ('question','done') or controller.awaiting_initial_plan): break
                        if pivot and controller.stage['exit_event']=='first_patch' and new_first_patch: break
                        if pivot and controller.stage['exit_event']=='requirements_met' and kind!='reply':
                            artifacts={k:{n:box.read('/deliverables/'+n) for n in artifact_names(requirements[k])} for k in controller.stage['gate']}
                            signature=(controller.stage['id'],digest(artifacts))
                            if (all(all(values.values()) for values in artifacts.values())
                                    and signature not in artifact_interrupts):
                                artifact_interrupts.add(signature);break
                    snapshot=snapshot_now(box); dump(out/f'snapshot-turn-{turn}.json',snapshot)
                    if reason: break
                    if not staged_interaction:
                        if completion_claim: reason='agent_done'; final_delivery=True; break
                        if baseline: reason='single_turn_ended_without_completion'; break
                        if turn>=budgets['max_user_turns'] or actions>=budgets['max_actions']:
                            reason='user_turn_budget_exhausted' if turn>=budgets['max_user_turns'] else 'action_budget_exhausted';break
                        message=user_message(controller,{'proposal':None,'accepted':True,'outcome':'continue','reasons':[]})
                        continue
                    if pivot:
                        probe_started = time.monotonic()
                        collected = collect_online_resource_evidence(controller, plan, requirements,
                            BINDING_SPEC.public_probes, snapshot, completion_claim,
                            trusted, probe_cache, out, public_probe)
                        timings['online_resource_probe_seconds'] = timings.get('online_resource_probe_seconds', 0) + time.monotonic() - probe_started
                        if collected:
                            dump(out/f'online-resource-evidence-turn-{turn}.json', {
                                'stage': controller.stage['id'], 'turn': turn,
                                'patch_sha256': snapshot['patch_sha256'],
                                'delivery': 'next simulator user message; not an online verdict',
                                'public_probe_results': user_simulator.public_probe_results(
                                    {pid: trusted[pid] for pid in collected})})
                    checks=evaluate_requirements() if pivot else {}; controller.record(checks,snapshot['snapshot_sha256'],turn,actions)
                    scope_check=source_scope.check_candidate_scope(snapshot,SOURCE_ROOTS)
                    controller.record_source_scope(scope_check,snapshot['snapshot_sha256'])
                    dump(out/f'source-scope-turn-{turn}.json',scope_check)
                    if controller.stage['id']=='S4' and plan.get('task_workflow') and completion_claim:
                        from .task_workflow import check as check_workflow
                        controller.record_task(check_workflow(initial,snapshot,BASE),snapshot['snapshot_sha256'])
                    dump(out/f'checks-turn-{turn}.json',checks)
                    if controller.end_reason: reason=controller.end_reason; break
                    if controller.stage['id']!='S4' and (turn>=budgets['max_user_turns'] or actions>=budgets['max_actions']):
                        # Do not record next-stage disclosure when there is no
                        # remaining user message/action in which it can occur.
                        reason='user_turn_budget_exhausted' if turn>=budgets['max_user_turns'] else 'action_budget_exhausted'
                        break
                    event=('plan_response' if controller.awaiting_initial_plan and last_reply.strip() else
                           'final_delivery' if controller.stage['id']=='S4' and completion_claim else
                           'first_patch' if controller.stage['exit_event']=='first_patch' and first_patch else
                           'completion_claim' if completion_claim else 'working')
                    view=user_simulator.state_view(controller,event,snapshot['snapshot_sha256'],turns,records)
                    try:
                        decision=user_simulator.propose(client,view,user_simulator.dialogue_style(PERSONA['id']) if pivot else {},simulator_audit)
                    finally:
                        dump(out/'simulator-dialogue.json',simulator_audit)
                    receipt=controller.review(decision['proposal'],event,snapshot['snapshot_sha256'])
                    simulator_audit.append({'operation':'proposal','turn':turn,'input':view,'output':decision,'controller_receipt':receipt})
                    dump(out/f'simulator-turn-{turn}.json',decision)
                    dump(out/f'controller-turn-{turn}.json',receipt)
                    candidate=copy.deepcopy(controller)
                    advanced=candidate.decide(decision['proposal'],event,snapshot['snapshot_sha256'],turn,actions)
                    if candidate.end_reason:
                        controller=candidate;reason=controller.end_reason;break
                    if turn>=budgets['max_user_turns'] or actions>=budgets['max_actions']:
                        reason='user_turn_budget_exhausted' if turn>=budgets['max_user_turns'] else 'action_budget_exhausted';break
                    # Render after review; a failed render must not disclose a new
                    # requirement or authorize implementation in the live ledger.
                    if (advanced and candidate.stage['id']=='S2' and plan.get('observed_change_fixture')
                            and observed_change is None):
                        observed_change=observed.capture(plan,snapshot,turns,actions,evaluate,out/'observed-public-change')
                    message=user_message(candidate,receipt,previous=controller if advanced else None,
                                         focus_ids=decision['focus_ids'])
                    if advanced: pending_controller=candidate
                    else: controller=candidate
                    save()
                reason=reason or 'user_turn_budget_exhausted'
            finally:
                interaction_end=time.monotonic()
                snapshot=snapshot_now(box); dump(out/'recovery-snapshot.json',snapshot)
    except Exception as exc:
        if isinstance(exc,(JudgeOutputError,requirement_judge.OnlineJudgeError)): dump(out/'judge-error-audit.json',exc.audit)
        error=client.redact(f'{type(exc).__name__}: {exc}')
        reason='interaction_budget_exhausted' if isinstance(exc,ApiFailure) and 'budget exhausted' in str(exc) else 'interaction_error'
        if isinstance(exc,user_simulator.SimulatorMessageError): reason='simulator_message_error'
        if isinstance(exc,CodingBudgetExceeded): reason='coding_time_budget_exhausted'; error=None
        if reason=='interaction_budget_exhausted': error=None
    if interaction_end is None: interaction_end=time.monotonic()
    timings['coding_execution_seconds']=coding_budget.spent
    dump(out/'coding-budget.json',coding_budget.receipt())
    timings['version']='staged-execution-timing-v1'
    timings['execution_status']='started' if interaction_start is not None else 'not_started'
    timings['staged_execution_seconds']=interaction_end-interaction_start if interaction_start is not None else None
    timings['interaction_seconds']=timings['staged_execution_seconds']
    timings['execution_scope']='first_simulator_message_delivered_to_interaction_end; excludes setup, opening generation, cleanup and final evaluation'
    stage_seconds={}
    for i,span in enumerate(turn_spans):
        end=turn_spans[i+1]['start_seconds'] if i+1<len(turn_spans) else interaction_end-start
        span['seconds']=end-span['start_seconds']
        stage_seconds[span['stage']]=stage_seconds.get(span['stage'],0)+span['seconds']
    timings['stage_seconds']=stage_seconds
    if snapshot is not None:
        dump(out/'final-snapshot.json',snapshot); (out/'model.patch').write_text(snapshot['patch'])
    official=None
    if snapshot is not None:
        t=time.monotonic()
        try: official=(official_evaluator or evaluate)(snapshot['patch'],out/'official',official=True)
        except Exception as exc: final_error=client.redact(f'Official evaluation: {type(exc).__name__}: {exc}')
        timings['official_seconds']=time.monotonic()-t
    online=copy.deepcopy(checks)
    if pivot and snapshot is not None:
        t=time.monotonic()
        # Separate finite final-review client: no feedback or extra agent work.
        try:
            if getattr(client, 'execution_cancelled', False) is True:
                dump(out/'final-review-status.json', {'status':'not_run', 'reason':'pier_trial_cancelled'})
                raise RuntimeError('Final review not run because Pier trial was cancelled')
            reviewer=client_factory(out/'final-review-api.jsonl',max_calls=budgets['final_review_calls'],deadline=t+budgets['final_review_seconds'])
            checks=evaluate_requirements(reviewer,final=True)
            for k,v in controller.permanent.items(): checks[k]=v
            dump(out/'final-review-status.json', {'status':'completed'})
        except Exception as exc:
            if isinstance(exc,JudgeOutputError): dump(out/'final-judge-error-audit.json',exc.audit)
            if not (out/'final-review-status.json').exists():
                dump(out/'final-review-status.json', {'status':'failed', 'error_type':type(exc).__name__})
            final_error=(final_error+'; ' if final_error else '')+client.redact(f'Final review: {type(exc).__name__}: {exc}')
            checks={k:{'status':'unknown','reason':'Final review failed; online verdict not substituted.'} for k in controller.disclosed}
        for k,v in controller.permanent.items(): checks[k]=v
        timings['final_review_seconds']=time.monotonic()-t
    states={k:checks.get(k,{'status':'unreached'})['status'] for k in requirements} if pivot else {}
    reward=official.get('reward') if official else None
    excluded=exclusion_reason(EPISODE.task.id)
    original_valid=reward is not None and official.get('integrity',{}).get('status')=='passed'
    original_eligible=original_valid and excluded is None
    persona_valid=pivot and snapshot is not None and not (final_error and 'Final review:' in final_error)
    base_pass=(reward.get('reward')==1) if reward is not None else None
    task_workflow=None
    if plan.get('task_workflow'):
        from .task_workflow import check as check_workflow
        task_workflow=check_workflow(initial,snapshot,BASE)
    persona_pass=None
    if persona_valid:
        if any(v in ('not_met','unreached','pending') for v in states.values()) or controller.permanent or controller.environment_violations:
            persona_pass=False
        elif all(v=='met' for v in states.values()): persona_pass=True
    persona_exclusion=admission.get('persona_evaluation_exclusion')
    joint=(base_pass and persona_pass) if pivot and original_eligible and persona_valid and not persona_exclusion else None
    coverage=template_coverage(plan)
    full_coverage=all(row['implemented'] for row in coverage)
    controller.end_reason=reason
    timings['end_to_end_seconds']=time.monotonic()-start
    # API logs retain all role attempts, including retries and unknown usage.
    by_role={}
    for log in (out/'api.jsonl',out/'final-review-api.jsonl'):
        if not log.exists(): continue
        for line in log.read_text().splitlines():
            v=json.loads(line); role=('final_review' if log.name.startswith('final') else v.get('role','unknown'))
            entry=by_role.setdefault(role,{'attempts':0,'seconds':0,'usage_unknown':0})
            entry['attempts']+=1; entry['seconds']+=v.get('seconds',0); entry['usage_unknown']+=bool(v.get('usage_unknown'))
            for name,count in v.get('usage',{}).items():
                if isinstance(count,(int,float)): entry[name]=entry.get(name,0)+count
            entry['cached_input_tokens']=entry.get('cached_input_tokens',0)+v.get('usage',{}).get('input_tokens_details',{}).get('cached_tokens',0)
    result={'execution_admission':admission,'coding_backend':backend.metadata,
        **{k:admission[k] for k in ('admission_policy','calibration_policy','calibration_status','model_calibrated')},
        'review_policy':admission.get('review_policy'),
        'preparation_warnings':admission.get('preparation_warnings',[]),
        'persona_evaluation_exclusion':persona_exclusion,
        'run_id':run_id,'protocol_version':VERSION,'task_id':EPISODE.task.id,'persona_id':EPISODE.persona['id'],
        'arm':arm,'task_disclosure_mode':plan['task_disclosure_mode'] if staged_interaction else 'full_at_start',
        'persona_disclosure_mode':'staged' if pivot else 'none',
        'coding_budget':coding_budget.receipt(),
        'coding_time_budget_exhausted':coding_budget.exhausted,
        'reward':reward,'base_pass':base_pass,'evolved':states,'persona_pass':persona_pass,'joint_pass':joint,
        'online_judge_protocol':requirement_judge.VERSION if pivot else None,
        'online_requirement_judgments':online if pivot else {},
        'task_workflow':task_workflow,
        'task_workflow_pass':(task_workflow['status']=='met' if task_workflow['status']!='unknown' else None) if task_workflow else None,
        'task_acceptance_scope':'base_pass/joint_pass retain original evaluator semantics; task_workflow separately reports registered local Git delivery evidence',
        'persona_pass_scope':'bound_requirements_only' if pivot else None,
        'full_persona_coverage':full_coverage if pivot else None,
        'full_persona_pass':persona_pass if pivot and full_coverage and not persona_exclusion else None,
        'full_joint_pass':joint if pivot and full_coverage else None,
        'original_evaluation_valid':original_valid,'persona_evaluation_valid':persona_valid if pivot else None,
        'report_policy':REPORT_POLICY_VERSION,'original_exclusion_reason':excluded,'original_report_eligible':original_eligible,
        'evaluation_valid':original_valid and (persona_valid if pivot else True) and error is None,
        'integrity':official.get('integrity') if official else None,'end_reason':reason,
        'budget_exhausted':'budget_exhausted' in (reason or ''),'error':error,'final_error':final_error,
        'stage':controller.summary() if staged_interaction else None,'final_delivery':controller.final_delivery if staged_interaction else final_delivery,
        'actions':actions,'tool_actions':tool_actions,'agent_replies':agent_replies,
        'rejected_agent_responses':len(rejected_agent_responses),
        'agent_response_retry_limit':backend.metadata.get('max_transport_retries_per_episode',2),
        'agent_response_retries_per_action':backend.metadata.get('max_transport_retries_per_action'),
        'user_message_source':'user_simulator',
        'simulator_message_attempts':sum(r['operation']=='render' for r in simulator_audit),
        'controller_rejections':sum(e['event']=='progression_rejected' for e in controller.events) if pivot else 0,
        'persona_measurement':({'met':sum(v=='met' for v in states.values()),'total_bound':len(states),
            'source_templates':len(coverage),'pending_templates':sum(r['status']=='pending_fixture' for r in coverage),
            'met_fraction':sum(v=='met' for v in states.values())/len(states),
            'unknown':sum(v=='unknown' for v in states.values()),'unreached':sum(v=='unreached' for v in states.values())} if pivot else None),
        'user_turns':len([t for t in turns if t['speaker']=='user']),
        'seconds':timings['staged_execution_seconds'],'timings':timings,'turn_spans':turn_spans,'role_costs':by_role,
        'template_coverage':coverage if pivot else [],'control_interpretation':plan['control']['interpretation'],
        'online_final_disagreement':[k for k in checks if k in online and checks[k]['status']!=online[k]['status']]}
    save(); dump(out/'checks.json',checks); dump(out/'result.json',result)
    (out/'transcript.md').write_text('\n\n'.join(f'## Turn {t["turn"]} — {t["speaker"]}\n\n{t["content"]}' for t in turns))
    (out/'report.md').write_text('# Staged episode\n\n```json\n'+json.dumps(result,indent=2,ensure_ascii=False)+'\n```\n')
    if delivery_blur:
        from .delivery_experience import evaluate as evaluate_experience
        evaluate_experience(out, PERSONA, enabled=os.environ.get('DS_DELIVERY_EXPERIENCE') == '1')
    return result
