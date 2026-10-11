"""Versioned staged binding overlays and evidence-governed state transitions.

No model, Docker or reference-solution access. Legacy bindings remain executable
under their original runner. New overlays must match their exact source binding.
"""
import copy
import hashlib
import json
import os
from pathlib import Path

VERSION = 'staged-persona-v34-frozen-runtime-preflight'
ADMISSION_POLICY = 'reviewed_configuration'
CALIBRATION_POLICY = 'optional_development_test'
PLAN_VERSION = 'staged-persona-v3'
STAGES = ('S1', 'S2', 'S3', 'S4')


def pending_check_reason(lifecycle, stage, completion_claim, final=False):
    """Shared execution rule; a disclosure event is not a completion claim."""
    if lifecycle.get('not_before_stage') and stage!=lifecycle['not_before_stage']:
        return 'Disclosed now; final task-dependent acceptance begins at S4.'
    if not lifecycle['before_implementation'] and not final and not completion_claim:
        return 'Awaiting a completion claim; no final-quality judgment on a partial patch.'
    return None


def runtime_acceptance_contract(plan):
    """Executable protocol evidence, explicitly not a live agent success claim."""
    checkpoints={}
    for stage in STAGES:
        for event,done,final in (('working_or_first_patch',False,False),
                                 ('completion_claim',True,False),('final_review',False,True)):
            checkpoints[stage+':'+event]={k:(
                'undisclosed' if life['stage']>stage else
                'pending_without_checker_call' if pending_check_reason(life,stage,done,final) else
                'run_registered_checker') for k,life in plan['requirements'].items()}
    result={'provenance':'Computed from the same pending_check_reason used by run_staged.evaluate_requirements; synthetic protocol evidence, not an agent execution or calibrated judge result.',
            'disclosure_precondition':'The runtime iterates only controller.disclosed. The table describes checks after each requirement has actually been delivered at its scheduled stage; plan-only S1 has no disclosed first-stage requirement and cannot check it. Any first-patch trace below includes the preceding plan/request exchange.',
            'completion_meaning':'Any actual completion claim for disclosed work can run completion-quality checks, including before S4. S4 also rechecks all obligations. A first_patch tool event is not a completion claim. Pending is not not_met or unknown.',
            'checkpoints':checkpoints}
    if plan.get('initial_exchange')=='plan_then_scope':
        c=Controller(plan);c.enter(1,0)
        c.decide('start_implementation','plan_response','synthetic-plan',1,1)
        pending={k:{'status':'pending','reason':pending_check_reason(plan['requirements'][k],'S1',False)} for k in c.disclosed}
        c.record(pending,'synthetic-first-patch-without-artifacts',2,2)
        result['first_patch_without_scope_artifact']={
            'fixture_kind':'synthetic_controller_transition; no artifact, checker vote or agent success invented',
            'checks':pending,
            'receipt':c.review('advance','first_patch','synthetic-first-patch-without-artifacts')}
    return result


def task_units(plan, contract=None):
    """Original-order fragments; full-at-start has one implicit unit."""
    if plan['task_disclosure_mode'] == 'full_at_start':
        return [{'id': 'full_task', 'stage': 'S1', 'text': contract}]
    return plan.get('task_units', [])


def disclosed_task(plan, contract, stage):
    return ''.join(unit['text'] for unit in task_units(plan, contract)
                   if STAGES.index(unit['stage']) <= STAGES.index(stage))


def new_task_documents(plan, contract, disclosed, previous=()):
    new = set(disclosed) - set(previous)
    # Paragraph separators are message framing, not task content. Keep the
    # lossless original units for coverage/hash checks; the simulator reproduces
    # each paragraph body, with its own surrounding newlines. This is NOT used
    # for executable probe scripts, whose bytes remain strictly checked.
    return [unit['text'].rstrip('\r\n') for unit in task_units(plan, contract) if unit['id'] in new]


def validate_disclosure(plan, contract):
    if 'policy' in plan or 'task_disclosure' in plan:
        raise ValueError('Legacy disclosure fields must be migrated to task_disclosure_mode')
    if plan.get('task_disclosure_mode') not in ('full_at_start', 'progressive'):
        raise ValueError('Unknown task disclosure mode')
    if plan.get('persona_disclosure_mode') != 'staged':
        raise ValueError('Persona disclosure must always be staged')
    units = task_units(plan, contract)
    if plan['task_disclosure_mode'] == 'full_at_start':
        if plan.get('task_units'):
            raise ValueError('Full-at-start cannot contain a partial task schedule')
    else:
        if (not isinstance(units, list) or not units or any(
                not isinstance(u, dict) or set(u) != {'id', 'stage', 'text'} or
                not isinstance(u['id'], str) or not u['id'].strip() or
                u['stage'] not in STAGES[:3] or
                not isinstance(u['text'], str) or not u['text'].strip() for u in units)):
            raise ValueError('Task units need id, stage S1-S3, and original contract text')
        if len({u['id'] for u in units}) != len(units):
            raise ValueError('Duplicate task unit')
        stages = {u['stage'] for u in units}
        if 'S1' not in stages or len(stages) < 2:
            raise ValueError('Progressive task must start at S1 and introduce content in later stages')
        if ''.join(u['text'] for u in units) != contract:
            raise ValueError('Task units must reconstruct the complete original contract exactly')
    if plan.get('implementation_authorization') not in ('S1', 'after_S1_acceptance', 'S3', 'S4'):
        raise ValueError('Unknown implementation authorization rule')


def selected_plan_path(episode):
    from .specs import ROOT
    selected=os.environ.get('DS_STAGED_PLAN_PATH')
    if selected:
        path=(ROOT/selected).resolve()
        path.relative_to((ROOT/'runs/staged-pipelines').resolve())
        return path
    return ROOT/'data/staged_bindings'/f'{episode.task.id}--{episode.persona["id"]}.json'


def load_plan(episode):
    path=selected_plan_path(episode)
    if os.environ.get('DS_STAGED_PLAN_PATH'):
        from .planning_staged import verify_prepared_plan
        verify_prepared_plan(path,episode)
    plan = json.loads(path.read_text())
    validate_plan(plan, episode)
    return plan


def validate_plan(plan, episode):
    from .fixture_repairs import validate as validate_fixture_repair
    validate_fixture_repair(plan)
    if (plan.get('schema_version'), plan.get('protocol')) != (3, PLAN_VERSION):
        raise ValueError('Unsupported staged schema/protocol')
    if (plan.get('task_id'), plan.get('persona_id')) != (episode.task.id, episode.persona['id']):
        raise ValueError('Staged identity mismatch')
    if plan.get('source_binding') != str(episode.binding.path.relative_to(episode.task.path.parents[2])):
        raise ValueError('Staged source binding path mismatch')
    if plan.get('source_binding_sha256') != hashlib.sha256(episode.binding.path.read_bytes()).hexdigest():
        raise ValueError('Staged source binding changed; review and version the adaptation')
    override=plan.get('calibration_positive_turns')
    if override is not None:
        if (not isinstance(override,dict) or set(override)!={'reason','turns'}
                or not isinstance(override['reason'],str) or not override['reason'].strip()
                or not isinstance(override['turns'],list) or not override['turns']):
            raise ValueError('Invalid calibration-only transcript override')
        for turn in override['turns']:
            if (not isinstance(turn,dict) or turn.get('speaker') not in ('agent','user')
                    or type(turn.get('turn')) is not int or turn['turn']<1
                    or not isinstance(turn.get('content'),str) or not turn['content'].strip()
                    or turn.get('fixture_only') is not True):
                raise ValueError('Calibration transcript turns must be explicitly synthetic')
    validate_disclosure(plan, episode.task.contract_path.read_text())
    if plan.get('initial_exchange') not in (None,'plan_then_scope','plan_then_requirements','plan_then_inventory','plan_then_review'):
        raise ValueError('Unsupported initial exchange')
    if plan.get('initial_exchange')=='plan_then_inventory' and (
            not plan.get('recovery_fixture') or plan['implementation_authorization']!='S3' or
            plan['stages'][0]['requirements']!=['recovery_inventory']):
        raise ValueError('Inventory plan exchange requires recovery before S3 implementation')
    if plan.get('initial_exchange')=='plan_then_review':
        if episode.persona['id']=='student_needing_explanation':
            early=[r['id'] for r in episode.requirements if r['template']=='student_error_counterexample']
            early += [r['id'] for r in plan.get('additional_requirements',[]) if r['template']=='student_error_counterexample']
            if (len(early)!=1 or plan['implementation_authorization']!='after_S1_acceptance'
                    or not plan.get('early_authorization_exchange')
                    or plan['stages'][0]['requirements']!=early
                    or plan['stages'][0]['gate']!=early
                    or not plan['requirements'][early[0]]['before_implementation']):
                raise ValueError('Student plan acceptance must disclose the early counterexample gate without implementation authorization')
        elif (episode.persona['id']!='senior_race_review_no_tutorial' or plan['implementation_authorization']!='S3' or not {'review_scope','race_findings'} <= set(plan['stages'][1]['gate']) or not all(plan['requirements'][k]['before_implementation'] for k in ('review_scope','race_findings'))):
            raise ValueError('Review conclusions must be accepted before the implementation branch')
    if plan.get('initial_exchange')=='plan_then_requirements':
        if (episode.persona['id'] not in ('laptop_data_scientist','manager_no_code','shop_owner_cannot_install') or
                plan['implementation_authorization']!='S1' or
                plan['stages'][0]['requirements']!=[({'manager_no_code':'manager_style','shop_owner_cannot_install':'owner_path'}.get(episode.persona['id'],'resource_budget'))] or
                plan['requirements'][plan['stages'][0]['requirements'][0]]['before_implementation']):
            raise ValueError('The registered first request follows a real plan and precedes implementation authorization')
        if episode.persona['id']=='laptop_data_scientist' and plan['stages'][0]['exit_event']!='first_patch':
            raise ValueError('Laptop criteria disclosure precedes a first-patch exit')
    if plan.get('initial_plan_request') and not plan.get('initial_exchange'):
        raise ValueError('An initial plan request requires an initial exchange')
    if 'initial_plan_request' in plan and (not isinstance(plan['initial_plan_request'],str) or not plan['initial_plan_request'].strip()):
        raise ValueError('An initial plan request must be nonempty text')
    if not isinstance(plan.get('opening_task_excerpts',[]),list):
        raise ValueError('Opening task excerpts must be a list')
    for excerpt in plan.get('opening_task_excerpts',[]):
        if not isinstance(excerpt,str) or not excerpt.strip() or excerpt not in disclosed_task(plan,episode.task.contract_path.read_text(),'S1'):
            raise ValueError('Opening task prerequisite must be disclosed at S1')
    if plan.get('early_authorization_exchange'):
        if (plan['implementation_authorization']!='after_S1_acceptance' or
                plan['stages'][0]['exit_event']!='first_patch' or not plan['stages'][0]['gate'] or
                any(not plan['requirements'][key]['before_implementation'] for key in plan['stages'][0]['gate'])):
            raise ValueError('Early authorization requires the accepted S1 gate followed by a first-patch exit')
    from .task_workflow import validate as validate_workflow
    validate_workflow(plan,episode.task.contract_path.read_text())
    if episode.persona['id']=='first_time_pr_contributor':
        if (plan.get('initial_exchange')!='plan_then_scope' or
                plan['implementation_authorization']!='S1' or
                plan['stages'][0]['requirements']!=[next(r['id'] for r in episode.requirements if r['template']=='first_pr_isolated_patch')]):
            raise ValueError('First-PR requires a plan response before scope disclosure and authorization in S1')
    from .specs import SUPPORTED_TRIGGERS, _identifier, _relative
    task_raw=copy.deepcopy(episode.task.raw)
    extra_artifacts=plan.get('artifact_files', [])
    for name in extra_artifacts:
        _relative(name, 'staged artifact')
        if not name or name=='.': raise ValueError('Invalid staged artifact')
    task_raw['artifact_files']=list(dict.fromkeys(task_raw.get('artifact_files',[])+extra_artifacts))
    probes = merged_probes(episode.binding.raw.get('public_probes', []), plan)
    probe_ids = {p['id'] for p in probes}
    event = plan.get('observed_change_fixture')
    if event and (episode.persona['id']!='manager_no_code' or event != {
            'kind':'independent_public_validation', 'after_stage':'S1', 'disclose_stage':'S2',
            'previous_state':'no_host_post_recommendation_validation',
            'validation_id':episode.task.raw['public_validation']['id']}):
        raise ValueError('Unsupported observed change fixture')
    overrides = plan.get('requirement_overrides', {})
    originals = {r['id']:r for r in episode.requirements}
    for key, fields in overrides.items():
        if key not in originals or not fields or not set(fields)<= {'bindings','checker','public_probe','negative_controls'}:
            raise ValueError('Unsupported staged requirement override')
        if 'checker' in fields and fields['checker']['kind']!=originals[key]['checker']['kind']:
            raise ValueError('Staged override cannot change checker kind')
        candidate = originals[key] | fields
        for pid in (candidate.get('public_probe'),candidate['checker'].get('parameters',{}).get('public_probe')):
            if pid and pid not in probe_ids:raise ValueError('Unregistered override probe')
    fixture=plan.get('recovery_fixture')
    if fixture:
        if (plan['implementation_authorization']!='S3' or episode.persona['id']!='designer_after_hard_reset'
                or fixture.get('kind')!='isolated_text_copy'):
            raise ValueError('Unsupported recovery fixture')
        if not {'recovery_inventory','authorized_recovery'}<=set(plan['stages'][1]['gate']):
            raise ValueError('Both recovery obligations must pass before S3 implementation authorization')
        _relative(fixture['tracked_path'],'recovery source')
        if fixture['snapshot_path'] not in {p['path'] for p in episode.task.protected_files}:
            raise ValueError('Recovery snapshot must be a registered protected source')
        if fixture.get('destination')!='/deliverables/recovered':raise ValueError('Unregistered recovery destination')
    from .negative_controls import validate_controls, controls_for
    templates = {t['id']: t for t in episode.persona['attributes']['evolved_requirements']}
    all_requirements = [r | overrides.get(r['id'],{}) for r in episode.requirements] + plan.get('additional_requirements', [])
    ids = {r['id'] for r in all_requirements}
    if len(ids) != len(all_requirements) or len({r['template'] for r in all_requirements}) != len(all_requirements):
        raise ValueError('Duplicate requirement or source template')
    for req in plan.get('additional_requirements', []):
        _identifier(req['id'], 'staged requirement id')
        template = templates.get(req['template'])
        if not template or req['trigger'] != template['trigger']['event']:
            raise ValueError('Added requirement must preserve source template identity/trigger')
        if req.get('runtime_trigger', req['trigger']) not in SUPPORTED_TRIGGERS:
            raise ValueError('Unsupported added runtime trigger')
        p = req['checker'].get('parameters', {})
        if req['checker']['kind'] != 'grounded_rubric' or not p.get('staged_evidence_bundle'):
            raise ValueError('Added requirement needs the staged evidence checker')
        if not req.get('bindings') or not p.get('required_evidence') or (not p.get('artifacts') and not (p.get('observed_change') and event)):
            raise ValueError('Added requirement needs bound evidence')
        if req.get('statement') != plan.get('requirements',{}).get(req['id'],{}).get('statement'):
            raise ValueError('Added requirement and lifecycle statement disagree')
        if not set(p['artifacts']) <= set(task_raw['artifact_files']):
            raise ValueError('Unregistered staged artifact')
        for name in p.get('baseline_paths', []) + p.get('allowed_changed_roots', []):
            _relative(name, 'staged evidence path')
            if name in ('', '.'):
                raise ValueError('Unbounded staged evidence path')
        for pid in (req.get('public_probe'), p.get('public_probe')):
            if pid is not None and pid not in probe_ids:
                raise ValueError('Unregistered staged probe')
    if any(r['checker'].get('parameters',{}).get('observed_change') for r in all_requirements) != bool(event):
        raise ValueError('Observed-change requirement and fixture disagree')
    combined = copy.deepcopy(episode.binding.raw)
    combined['requirements'] = all_requirements
    combined['public_probes'] = probes
    validate_controls(combined, task_raw, require_complete=True)
    controls = {c['id']: c for c in controls_for(combined)}
    adapted = set()
    for item in plan.get('calibration_control_adaptations', []):
        source = controls.get(item['source_id']); replacement = item['replacement']
        if source is None or item['source_id'] in adapted or not item.get('reason'):
            raise ValueError('Invalid calibration control adaptation provenance')
        adapted.add(item['source_id'])
        life = plan['requirements'][source['requirement_id']]
        if (not life['before_implementation'] or source['kind'] != 'drop_trusted_probe'
                or replacement['requirement_id'] != source['requirement_id']
                or replacement['kind'] != 'drop_baseline_source'
                or replacement['target'] != 'baseline_source'
                or replacement['id'] in controls):
            raise ValueError('Invalid early-evidence control replacement')
    audit = plan.get('template_audit', [])
    if len(audit) != len(templates) or {a['template_id'] for a in audit} != set(templates):
        raise ValueError('Audit must account for every source template exactly once')
    bound = {r['template']: r['id'] for r in all_requirements}
    for row in audit:
        status = row.get('status'); key = row['template_id']
        if status not in ('implemented', 'pending_fixture', 'not_applicable'):
            raise ValueError('Invalid template coverage status')
        if not row.get('reason') or not row.get('evidence') or not row.get('remaining_work'):
            raise ValueError('Template audit needs evidence and disposition')
        if row.get('source_applicability') != templates[key]['applicability']:
            raise ValueError('Template applicability drift')
        if (key in bound) != (status == 'implemented') or row.get('requirement_id') != bound.get(key):
            raise ValueError('Template audit does not match implementation')
        if status == 'implemented' and row.get('stage') != plan['requirements'][bound[key]]['stage']:
            raise ValueError('Template audit stage mismatch')
    if set(plan.get('requirements',{})) != ids:
        raise ValueError('Every bound requirement must have exactly one lifecycle')
    stages = plan.get('stages',[])
    order = [s['id'] for s in stages]
    if not order or order[0]!='S1' or order[-1]!='S4' or order!=sorted(set(order), key=STAGES.index):
        raise ValueError('Stages must be unique and ordered S1 through S4')
    if set(plan.get('omitted_stages',{})) != set(STAGES)-set(order):
        raise ValueError('Omitted stages must be explicit')
    units = task_units(plan)
    if any(u['stage'] not in order for u in units):
        raise ValueError('Task content assigned to an omitted stage')
    introductions = [s for s in stages if s['requirements']]
    if (len(introductions) < 2 or introductions[0]['id']!='S1'
            or any(s['id']=='S4' or len(s['requirements'])>1 for s in introductions)):
        raise ValueError('Persona requirements must be introduced separately across stages before S4')
    disclosed = set()
    for stage in stages:
        if stage['exit_event'] not in ('requirements_met','first_patch','completion_claim','final_delivery'):
            raise ValueError('Unsupported stage exit event')
        for key in stage['requirements']:
            if key in disclosed or key not in ids or plan['requirements'][key]['stage']!=stage['id']:
                raise ValueError('Duplicate/misplaced stage requirement')
            disclosed.add(key)
        if not set(stage['gate']) <= disclosed:
            raise ValueError('Stage cannot require undisclosed requirements')
    if disclosed!=ids or set(stages[-1]['gate'])!=ids:
        raise ValueError('Final delivery must account for every requirement')
    for key, req in plan['requirements'].items():
        dependencies = req.get('task_dependencies')
        visible = {u['id'] for u in units if STAGES.index(u['stage']) <= STAGES.index(req['stage'])}
        if (not isinstance(dependencies, list) or len(set(dependencies)) != len(dependencies)
                or not set(dependencies) <= visible):
            raise ValueError('Requirement task dependencies must already be disclosed at introduction')
        if req.get('not_before_stage') not in (None,'S4'):
            raise ValueError('Unknown deferred acceptance stage')
        if req.get('not_before_stage') and (req['before_implementation'] or req['acceptance']!='completion'):
            raise ValueError('Deferred acceptance must be a completion requirement')
        if req.get('not_before_stage') and any(key in s['gate'] for s in stages if s['id']!='S4'):
            raise ValueError('Deferred requirement cannot block its own implementation stage')
        if req['acceptance'] not in ('artifact','completion') or not req.get('statement') or not req.get('adaptation'):
            raise ValueError('Missing acceptance boundary or provenance')
        if req['before_implementation'] and req['acceptance']!='artifact':
            raise ValueError('Preimplementation requirement cannot wait for final completion')
        if type(req['persistent']) is not bool or type(req['before_implementation']) is not bool:
            raise ValueError('Lifecycle flags must be boolean')
    if not plan.get('rationale') or not plan.get('opening'):
        raise ValueError('Missing applicability rationale/opening')
    for key in ('max_actions','max_user_turns','interaction_calls','interaction_seconds','final_review_calls','final_review_seconds'):
        if type(plan['budgets'].get(key)) is not int or plan['budgets'][key]<=0:
            raise ValueError('Budgets must be finite positive integers')
    from .coding_budget import coding_seconds
    coding_seconds(plan)
    if 'pier_agent_timeout_sec' in plan:
        from .delivery_timeouts import positive_seconds
        positive_seconds(plan['pier_agent_timeout_sec'])
    if 'pier_verifier_timeout_sec' in plan:
        from .delivery_timeouts import positive_seconds
        positive_seconds(plan['pier_verifier_timeout_sec'])
    if plan['status'] not in ('requires_review','blocked_probe_repair','blocked_adaptation'):
        raise ValueError('Unknown admission status')
    if bool(plan['blocked_reasons']) != plan['status'].startswith('blocked_'):
        raise ValueError('Admission status and blockers disagree')
    review=plan.get('binding_review')
    if review is not None:
        pending=[row['template_id'] for row in audit if row['status']=='pending_fixture']
        if (review.get('version')!='staged-binding-review-v1'
                or review.get('validation_level')!='offline_specification_only'
                or review.get('requires_fresh_model_calibration') is not False
                or review.get('calibration_policy') != CALIBRATION_POLICY
                or review.get('original_evaluator_changed') is not False
                or review.get('pending_templates')!=pending):
            raise ValueError('Invalid binding review status')
        if review.get('public_contract')!=str(episode.task.contract_path.relative_to(episode.task.path.parents[2])):
            raise ValueError('Binding review contract mismatch')
        if set(review.get('requirements',{}))!=ids or not review.get('policy_reason'):
            raise ValueError('Binding review must cover every requirement')
        for req in all_requirements:
            row=review['requirements'][req['id']];params=req['checker'].get('parameters',{})
            if (row.get('source_template')!=req['template'] or row.get('stage')!=plan['requirements'][req['id']]['stage']
                    or row.get('checker')!=req['checker']['kind']
                    or row.get('public_probe')!=(req.get('public_probe') or params.get('public_probe'))
                    or not row.get('acceptance_evidence') or not isinstance(row.get('artifacts'),list)
                    or not row.get('host_evidence')):
                raise ValueError('Binding review evidence drift')
            if not set(row['artifacts'])<=set(task_raw['artifact_files']):
                raise ValueError('Binding review references an unregistered artifact')
        if pending and not plan['blocked_reasons']:
            raise ValueError('Incomplete reviewed binding must remain blocked')


def merged_probes(originals, plan):
    """Versioned public fixtures; legacy bindings and native evaluators stay intact."""
    from .specs import _relative, _identifier
    result = {p['id']:copy.deepcopy(p) for p in originals}
    seen=set()
    for item in plan.get('public_probe_overlays', []):
        probe=item['probe']; pid=probe['id']; _identifier(pid,'staged probe')
        if pid in seen or not item.get('reason'):
            raise ValueError('Duplicate/unexplained staged public probe')
        seen.add(pid)
        previous=result.get(pid)
        expected=hashlib.sha256(json.dumps(previous,sort_keys=True).encode()).hexdigest() if previous else None
        if item.get('source_sha256')!=expected:
            raise ValueError('Public probe source drift')
        for key in ('script','command','filename'):
            if not isinstance(probe.get(key),str) or not probe[key]:raise ValueError('Invalid staged probe')
        _relative(probe['filename'],'probe filename')
        if type(probe.get('timeout_seconds')) is not int or not 0<probe['timeout_seconds']<=600:
            raise ValueError('Unbounded staged probe')
        if probe.get('host_oracle') not in (None,'sqlite_semantics_v1','sqlite_cost_v1'):
            raise ValueError('Unsupported host public oracle')
        if probe.get('resource_measurement'):
            from .resource_probe import validate_protocol
            validate_protocol(probe['resource_measurement'],probe)
            if probe.get('host_oracle'):raise ValueError('Resource and SQL oracles cannot be combined')
        if probe.get('host_oracle') and not any(e.get('filename')=='persona_sqlite.py' for e in probe.get('extra_files',[])):
            raise ValueError('Missing pinned SQLite oracle')
        names={probe['filename']}
        for extra in probe.get('extra_files',[]):
            _relative(extra['filename'],'probe extra filename')
            if extra['filename'] in names or not isinstance(extra.get('script'),str):
                raise ValueError('Invalid/duplicate staged probe file')
            names.add(extra['filename'])
        result[pid]=copy.deepcopy(probe)
    return list(result.values())


def adapt_episode(episode, plan=None):
    from .specs import BindingSpec, EpisodeSpec, TaskSpec
    if plan is None:
        plan = load_plan(episode)
        selected_path=selected_plan_path(episode)
    else:
        validate_plan(plan,episode)
        selected_path=None
    raw = copy.deepcopy(episode.binding.raw)
    raw['public_probes'] = merged_probes(raw.get('public_probes', []), plan)
    for req in raw['requirements']:
        req.update(copy.deepcopy(plan.get('requirement_overrides', {}).get(req['id'], {})))
    raw['requirements'].extend(copy.deepcopy(plan.get('additional_requirements', [])))
    if plan.get('calibration_positive_turns'):
        raw['calibration']['positive_turns']=copy.deepcopy(plan['calibration_positive_turns']['turns'])
    raw['staged'] = copy.deepcopy(plan)
    if selected_path is not None:
        from .specs import ROOT
        raw['staged_plan_path']=str(selected_path.relative_to(ROOT))
    raw['opening'] = plan['opening']
    for req in raw['requirements']:
        req['statement'] = plan['requirements'][req['id']]['statement']
        if req['template']=='first_pr_reviewable_handoff':
            # The original persona permits an honest not-run report. Keep this
            # correction scoped to staged episodes; legacy protocols are unchanged.
            req['checker'].setdefault('parameters',{})['allow_public_not_run']=True
        if req['checker']['kind']=='isolated_patch':
            req['checker'].setdefault('parameters',{})['preserve_initial_workspace']=True
            notice=' Preserve all Git-visible pre-existing changed paths, including their content, file modes and staged/unstaged state. These paths are reserved: if this task needs to edit one, report the conflict rather than include or overwrite it. Deliver the task changes separately; preserved initial dirty or untracked paths are allowed. scope.json requires the disclosed field values; extra fields alone are not a failure.'
            if notice not in req['statement']:req['statement']+=notice
        if req['template'] in ('student_patch_and_boundary_reasoning','student_learning_constraints_and_check','senior_concrete_race_findings') and req['checker']['kind']=='grounded_rubric':
            req['checker'].setdefault('parameters',{})['require_probe_pass']=False
        if req['template']=='first_pr_patch_explanation':
            req['bindings']['patch_anchor']='Actual current candidate files, symbols and diff; update the explanation as disclosed work changes. First patch is the disclosure trigger, not a frozen acceptance snapshot.'
        from .check_contracts import planning_evidence_contract
        contract=planning_evidence_contract(req,plan['requirements'][req['id']])
        if contract:
            req['checker'].setdefault('parameters',{})['required_evidence']=contract['required_citations']
            review=raw['staged']['binding_review']['requirements'][req['id']]
            review['host_evidence']=contract['allowed']
            review['acceptance_evidence']=contract['provider']
        pid=req.get('public_probe') or req['checker'].get('parameters',{}).get('public_probe')
        if pid:
            req['staged_baseline_probe']={'probe_id':pid,'base_commit':episode.task.base_commit,
                'case_sha256':hashlib.sha256(next(p for p in raw['public_probes'] if p['id']==pid)['script'].encode()).hexdigest()}
            probe=next(p for p in raw['public_probes'] if p['id']==pid)
            if req['checker'].get('parameters',{}).get('execute_delivered_runbook'):
                from .runbook_probe import contract
                req['staged_runbook_probe']=contract(probe)
            if probe.get('resource_measurement'):
                from .resource_probe import identity,validate_protocol
                validate_protocol(probe['resource_measurement'],probe,episode.task.raw['environment'])
                req['staged_resource_probe']={'protocol':copy.deepcopy(probe['resource_measurement']),
                                              'probe_sha256':identity(probe),
                                              'measure_budget':req['template']=='laptop_measured_resource_budget'}
    task=episode.task
    if plan.get('artifact_files'):
        task_raw=copy.deepcopy(task.raw)
        task_raw['artifact_files']=list(dict.fromkeys(task.artifact_files+plan['artifact_files']))
        task=TaskSpec(task.id,task.path,task_raw)
    return EpisodeSpec(task, episode.persona,
                       BindingSpec(episode.task.id, episode.persona['id'],episode.binding.path,raw))


def template_coverage(plan):
    """Missing fixtures are unresolved coverage, never silently inapplicable."""
    return [copy.deepcopy(row) | {'implemented': row['status']=='implemented',
            'applicable': True if row['status']=='implemented' else
                          False if row['status']=='not_applicable' else None}
            for row in plan['template_audit']]


def calibration_controls(episode):
    from .negative_controls import controls_for
    replacements = {a['source_id']: a['replacement'] for a in
                    episode.binding.raw['staged'].get('calibration_control_adaptations', [])}
    return [copy.deepcopy(replacements.get(c['id'], c)) for c in controls_for(episode.binding.raw)]


class Controller:
    """Only fresh host evidence can authorize progression; simulator proposes."""
    def __init__(self, plan):
        self.plan=plan; self.index=0; self.disclosed=set(); self.verdicts={}
        self.events=[]; self.permanent={}; self.response_opportunities={}
        self.task_disclosed=False; self.implementation_authorized=False
        self.disclosed_task_units=set()
        self.full_task_disclosed=False
        self.end_reason=None; self.final_delivery=False; self.environment_violations=[]
        self.task_verdict=None
        self.source_scope_verdict=None

    @property
    def stage(self): return self.plan['stages'][self.index]

    @property
    def awaiting_initial_plan(self):
        return (self.plan.get('initial_exchange') in ('plan_then_scope','plan_then_requirements','plan_then_inventory','plan_then_review') and self.index==0
                and not self.disclosed)

    @property
    def initial_plan_proposal(self):
        return 'accept_plan' if self.plan.get('initial_exchange') in ('plan_then_inventory','plan_then_review') else 'start_implementation'

    @property
    def awaiting_early_authorization(self):
        return bool(self.plan.get('early_authorization_exchange') and self.index==0
                    and not self.awaiting_initial_plan and not self.implementation_authorized)

    def enter(self, turn, action):
        self.events.append({'event':'stage_enter','stage':self.stage['id'],'turn':turn,'action':action})
        new_units={u['id'] for u in task_units(self.plan) if u['stage']==self.stage['id']} - self.disclosed_task_units
        self.disclosed_task_units.update(new_units)
        self.task_disclosed=bool(self.disclosed_task_units)
        self.full_task_disclosed=self.disclosed_task_units=={u['id'] for u in task_units(self.plan)}
        if new_units:
            self.events.append({'event':'task_disclosed','stage':self.stage['id'],
                                'units':sorted(new_units),'complete':self.full_task_disclosed,
                                'turn':turn,'action':action})
        auth=self.plan['implementation_authorization']
        if self.awaiting_initial_plan:
            return
        if self.task_disclosed and (auth==self.stage['id'] or (auth=='after_S1_acceptance' and self.index>0)):
            self.authorize(turn,action)
        for key in self.stage['requirements']:
            self.disclosed.add(key); self.response_opportunities[key]=0
            self.events.append({'event':'requirement_disclosed','requirement':key,'turn':turn,'action':action})

    def authorize(self, turn, action):
        if not self.implementation_authorized:
            self.implementation_authorized=True
            self.events.append({'event':'implementation_authorized','turn':turn,'action':action})

    def observe_action(self, turn, action, source_changed=False):
        for key in self.disclosed: self.response_opportunities[key]+=1
        if source_changed and not self.implementation_authorized:
            self.end_reason='implementation_before_authorization'
            self.environment_violations.append(self.end_reason)
            self.events.append({'event':'permanent_authorization_violation','turn':turn,'action':action})

    def observe_protected(self, initial, current, turn, action):
        changed=[name for name,value in initial.items() if current.get(name)!=value]
        if changed:
            self.end_reason='protected_content_violation'
            self.environment_violations.append(self.end_reason)
            self.events.append({'event':'permanent_protected_content_violation','paths':changed,'turn':turn,'action':action})

    def record(self, checks, evidence_version, turn, action):
        if not set(checks)<=self.disclosed:
            raise ValueError('Cannot score undisclosed requirements')
        for key,value in checks.items():
            status=value['status']
            if status not in ('met','not_met','unknown','pending'):
                raise ValueError('Invalid requirement state')
            old=self.verdicts.get(key,{}).get('status')
            if value.get('repairable') is False and status=='not_met': self.permanent[key]=value
            value=self.permanent.get(key,value)
            self.verdicts[key]=copy.deepcopy(value)|{'evidence_version':evidence_version}
            self.events.append({'event':'requirement_check','requirement':key,'status':value['status'],
                                **({'verdict':value['verdict']} if 'verdict' in value else {}),
                                'previous_status':old,'turn':turn,'action':action,'evidence_version':evidence_version})
        if self.permanent: self.end_reason='irreversible_requirement_violation'

    def record_task(self, value, evidence_version):
        self.task_verdict=copy.deepcopy(value)|{'evidence_version':evidence_version}

    def record_source_scope(self, value, evidence_version):
        self.source_scope_verdict=copy.deepcopy(value)|{'evidence_version':evidence_version}

    def review(self, proposal, event, evidence_version):
        """A public, non-mutating receipt for the simulator, not an agent reply."""
        if proposal not in ('continue','advance','finish','start_implementation','accept_plan'):
            raise ValueError('Invalid simulator proposal')
        if self.awaiting_initial_plan:
            accepted=not self.end_reason and (proposal=='continue' or
                proposal==self.initial_plan_proposal and event=='plan_response')
            return {'proposal':proposal,'accepted':accepted,'stage':self.stage['id'],
                    'outcome':proposal if accepted else 'continue','blocking_requirements':[],
                    'reasons':[] if accepted else ['initial_plan_response_required'],
                    'expected_event':'plan_response','observed_event':event}
        gate=self.stage['gate']
        fresh=lambda key: (self.verdicts.get(key,{}).get('evidence_version')==evidence_version
                           or not self.plan['requirements'][key]['persistent'])
        unmet=[key for key in gate if self.verdicts.get(key,{}).get('status')!='met' or not fresh(key)]
        failed=[key for key in self.disclosed if self.verdicts.get(key,{}).get('status') in ('not_met','unknown')]
        if self.awaiting_early_authorization:
            ready=not self.end_reason and not unmet and not failed
            accepted=not self.end_reason and (proposal=='continue' or ready and proposal=='start_implementation')
            return {'proposal':proposal,'accepted':accepted,'stage':'S1','outcome':proposal if accepted else 'continue',
                    'blocking_requirements':sorted(set(unmet+failed)),
                    'reasons':[] if accepted else ['early_gate_acceptance_required_before_authorization'],
                    'expected_event':'requirements_met','observed_event':event,
                    'authorization_basis':'accepted_early_gate'}
        if self.stage['id']=='S4' and self.plan.get('task_workflow'):
            if (not self.task_verdict or self.task_verdict['status']!='met' or
                    self.task_verdict['evidence_version']!=evidence_version):
                unmet.append('task_workflow')
        if self.source_scope_verdict is not None and (
                self.source_scope_verdict['status']!='met' or
                self.source_scope_verdict['evidence_version']!=evidence_version):
            unmet.append('source_scope')
        expected=self.stage['exit_event']
        ready=(not unmet and not failed and
               (expected=='requirements_met' or event==expected))
        reasons=[]
        if self.end_reason: reasons.append('episode_already_terminated')
        if unmet: reasons.append('current_gate_not_satisfied_or_stale')
        if failed: reasons.append('disclosed_requirement_failed_or_unknown')
        if expected!='requirements_met' and event!=expected: reasons.append('required_event_not_observed')
        required_proposal='finish' if self.stage['id']=='S4' else 'advance'
        if proposal not in ('continue',required_proposal): reasons.append('proposal_not_allowed_at_this_stage')
        accepted=not self.end_reason and (proposal=='continue' or (ready and proposal==required_proposal))
        return {'proposal':proposal,'accepted':accepted,'stage':self.stage['id'],
                'outcome':proposal if accepted else 'continue',
                'blocking_requirements':sorted(set(unmet+failed)),
                'reasons':reasons,'expected_event':expected,'observed_event':event}

    def decide(self, proposal, event, evidence_version, turn, action):
        receipt=self.review(proposal,event,evidence_version)
        if self.end_reason or proposal=='continue': return False
        if not receipt['accepted']:
            self.events.append({'event':'progression_rejected','proposal':proposal,'stage':self.stage['id'],
                                'unmet':receipt['blocking_requirements'],'reasons':receipt['reasons'],
                                'turn':turn,'action':action})
            return False
        if self.awaiting_initial_plan:
            # This is a candidate state, committed only when the simulator's
            # scope/authorization message is delivered at the next user turn.
            self.events.append({'event':'plan_response_accepted','turn':turn,'action':action})
            for key in self.stage['requirements']:
                self.disclosed.add(key); self.response_opportunities[key]=0
                self.events.append({'event':'requirement_disclosed','requirement':key,'turn':turn+1,'action':action})
            if proposal=='start_implementation':self.authorize(turn+1,action)
            return True
        if self.awaiting_early_authorization:
            self.events.append({'event':'early_gate_accepted_for_authorization','turn':turn,'action':action})
            self.authorize(turn+1,action)
            return True
        if self.stage['id']=='S4':
            if proposal!='finish': return False
            self.final_delivery=True; self.end_reason='final_delivery'
            self.events.append({'event':'final_delivery','turn':turn,'action':action})
        else:
            if proposal!='advance': return False
            self.index+=1; self.enter(turn+1,action)
        return True

    def summary(self):
        states={k:self.permanent.get(k,self.verdicts.get(k,{'status':'pending' if k in self.disclosed else 'unreached'}))['status']
                for k in self.plan['requirements']}
        return {'task_disclosure_mode':self.plan['task_disclosure_mode'],
                'persona_disclosure_mode':self.plan['persona_disclosure_mode'],
                'disclosed_task_units':sorted(self.disclosed_task_units),'full_task_disclosed':self.full_task_disclosed,
                'last_stage':self.stage['id'],
                'reached_s4':self.stage['id']=='S4','task_disclosed':self.task_disclosed,
                'implementation_authorized':self.implementation_authorized,'final_delivery':self.final_delivery,
                'awaiting_initial_plan':self.awaiting_initial_plan,
                'awaiting_early_authorization':self.awaiting_early_authorization,
                'task_workflow':self.task_verdict,
                'source_scope':self.source_scope_verdict,
                'end_reason':self.end_reason,'requirements':states,'permanent_violations':sorted(self.permanent),
                'environment_violations':self.environment_violations,
                'response_opportunities':self.response_opportunities,'events':self.events}
