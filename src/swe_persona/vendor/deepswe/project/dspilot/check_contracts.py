"""Shared runtime/planner evidence interface; not a model-authored claim."""
EXPLANATION_EVIDENCE = {
    'explanation':'Actual delivered explanation artifact.',
    'patch':'Current committed patch and mechanically extracted added lines.',
    'base_run':'Registered baseline probe, executed script, output and source context.',
    'final_run':'Registered current-patch probe, executed script, output and host evidence context.'}
OTHER_EVIDENCE = {
    'scope':'Host-read scope.json artifact; canonical judge evidence key.',
    'snapshot':'Current host snapshot, workspace identities and artifact receipts.',
    'PR':'Host-read PR draft; canonical alias for artifact:PR.md.',
    'validation':'Host-read validation manifest; canonical alias for artifact:validation.json.',
    'executions':'Actual registered public validation records, or an explicit empty-record observation.'}


def explanation_evidence(artifact, patch, baseline, candidate):
    return dict(zip(EXPLANATION_EVIDENCE,(artifact,patch,baseline,candidate)))


def checker_route(requirement, lifecycle):
    """The same dispatch selector is consumed by runtime and preparation."""
    params=requirement['checker'].get('parameters',{})
    if lifecycle['before_implementation']:
        return 'early_restore' if params.get('recovery_check')=='restore' else 'early_rubric'
    if params.get('recovery_check')=='handoff':return 'recovery_handoff'
    if params.get('staged_evidence_bundle'):return 'staged_rubric'
    if requirement['checker']['kind']=='grounded_explanation':return 'probe_explanation'
    return 'candidate_'+requirement['checker']['kind']


def probe_pass_required(requirement, lifecycle):
    route=checker_route(requirement,lifecycle)
    if route not in ('candidate_trusted_probe','candidate_grounded_rubric','staged_rubric'):return False
    params=requirement['checker'].get('parameters',{})
    return requirement['checker']['kind']=='trusted_probe' or bool(params.get('public_probe')) and params.get('require_probe_pass',True)


def evidence_contract(keys, provider, notes, required=None):
    keys=list(dict.fromkeys(keys));required=keys if required is None else required
    return {'allowed':keys,'required':required,'required_citations':required,
            'provider':provider,'notes':notes}


def planning_evidence_contract(requirement, lifecycle):
    params=requirement['checker'].get('parameters',{})
    route=checker_route(requirement,lifecycle)
    if route=='early_restore':
        return evidence_contract(['initial_snapshot','snapshot','artifact:recovery-manifest.json'],
            'checks_staged.early_check -> recovery_staged.check_restore',
            'Deterministic source/destination hashes and manifest comparison. No semantic judge or candidate probe.')
    if route=='early_rubric':
        names=params.get('artifacts') or ([params['artifact']] if params.get('artifact') else
            ['review.md'] if requirement['template']=='senior_review_scope_and_invariants' else [])
        keys=['artifact:'+name for name in names]+['initial_snapshot','phase_context','baseline_source']
        if requirement.get('public_probe') or params.get('public_probe'):keys+=['baseline_execution']
        return evidence_contract(keys,'checks_staged.early_check -> checks.check',
            'Frozen preimplementation evidence; baseline failure is allowed. No current candidate, base_run/final_run aliases or future validation.')
    kind=requirement['checker']['kind']
    if kind=='isolated_patch':
        return {'allowed':['initial_snapshot','authorization','diff','scope','snapshot'],
                'required':['initial_snapshot','authorization','diff','scope','snapshot'],
                'required_citations':['scope','diff','snapshot'],
                'provider':'checks.check readable_evidence isolated_patch branch',
                'notes':'scope is the host-read scope artifact. Current workspace_identity is in snapshot, compared to initial_snapshot. Dialogue and execution_history are not inputs to this branch. Required fields are validated; extra scope fields are permitted.'}
    if kind=='reviewable_handoff':
        keys=['PR','diff','executions','validation','snapshot','execution_history']
        return {'allowed':keys,'required':keys,'required_citations':keys,
                'provider':'checks.check -> checks_v3.GroundedJudge (including actual historical failures)',
                'notes':'PR and validation are canonical host artifact aliases. GroundedJudge also requires each dynamic execution_failure:action-N source when present. No run may be invented or omitted; no-record cases are explicitly observed. These aliases do not change the public filenames.'}
    if requirement['checker']['kind']=='grounded_explanation' and not lifecycle['before_implementation']:
        return {'allowed':list(EXPLANATION_EVIDENCE),'required':list(EXPLANATION_EVIDENCE),
                'required_citations':list(EXPLANATION_EVIDENCE),
                'provider':'full_persona.check_explanation -> check_contracts.explanation_evidence',
                'notes':'These are direct checker inputs. Probe execution output is in base_run/final_run. Dialogue transcript and baseline_source are not direct inputs. A probe failure can support an honest explanation; it is not automatically an explanation failure.'}
    if kind=='trusted_probe':
        pid=requirement.get('public_probe') or params.get('public_probe')
        measured=requirement.get('staged_resource_probe')
        return evidence_contract(['snapshot','trusted_probe:'+pid],'checks._trusted_probe_errors',
            ('Matching workload/base/patch identity and full cold/warm execution, plus measured memory limits and events.'
             if measured and measured['measure_budget'] else
             'Matching workload/base/patch identity and full cold/warm functional execution; memory outcome is separate.'
             if measured else 'Deterministic matching candidate patch and passing probe.')+' No transcript or prose judge.')
    if kind=='grounded_rubric':
        common=['initial_snapshot','authorization','diff','snapshot','executions','execution_history',
                'transcript','phase_audit','action_audit','observed_shell_checks']
        common+=['artifact:'+name for name in params.get('artifacts',[])]
        if route=='staged_rubric':common+=['baseline_source','public_contract','observed_change']
        if route=='recovery_handoff':common+=['recovery_evidence']
        pid=requirement.get('public_probe') or params.get('public_probe')
        if pid:common+=['trusted_probe:'+pid]
        if params.get('execute_delivered_runbook') and pid:common+=['trusted_probe:runbook-'+pid]
        required=params.get('required_evidence',[]) if kind=='grounded_rubric' else ['trusted_probe:'+pid]
        common += [key for key in required if key.startswith(('artifact:','trusted_probe:'))]
        return evidence_contract(common,'checks_staged.candidate_check['+route+'] -> checks.check',
            'Required citations are distinct from deterministic preconditions. Probe-pass policy is separate from evidence identity.',required=required)
    return None
