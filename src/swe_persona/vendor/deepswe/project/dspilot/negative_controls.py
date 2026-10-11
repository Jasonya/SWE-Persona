"""Bounded evidence mutations for candidate checker calibration, never agent runs."""
import copy


def mutate_public_record(control, public, validation_id):
    """Drop the validation record consumed by handoff, not just the probe map."""
    if control['kind']=='drop_trusted_probe' and control['target']==validation_id:
        return None
    return public


def controls_for(binding):
    """Execute both binding-level and requirement-local registered controls."""
    controls = list(binding.get('calibration', {}).get('negative_controls', []))
    for req in binding['requirements']:
        for control in req.get('negative_controls', []):
            if control.get('requirement_id') != req['id']:
                raise ValueError('Requirement-local negative control targets another requirement')
            controls.append(control)
    return controls


def validate_controls(binding, task, *, require_complete=False):
    requirements = {req['id']: req for req in binding['requirements']}
    controls = controls_for(binding)
    covered, ids = set(), set()
    artifacts = set(task.get('artifact_files', []))
    protected = {item['path'] for item in task.get('protected_files', [])}
    probes = {item['id'] for item in binding.get('public_probes', [])}
    for control in controls:
        name, requirement = control['id'], control['requirement_id']
        if not isinstance(name, str) or not name.strip() or name in ids or requirement not in requirements:
            raise ValueError('Invalid/duplicate negative control identity')
        ids.add(name); covered.add(requirement)
        kind, target = control['kind'], control['target']
        req = requirements[requirement]
        # Older drafts called the independent public validation a trusted probe.
        # Accept that spelling only for the handoff's registered validation ID;
        # calibration must actually remove its validation record, not just a probe.
        validation_ids = set()
        if req['checker']['kind'] == 'reviewable_handoff':
            registered = task.get('public_validation', {}).get('id')
            selected = req['checker'].get('parameters', {}).get('public_validation_id', registered)
            if registered and selected == registered:
                validation_ids.add(registered)
        allowed = {'remove_artifact': artifacts, 'replace_artifact': artifacts,
                   'change_protected_file': protected, 'drop_trusted_probe': probes | validation_ids,
                   'replace_turns': {'transcript'} if req['checker']['kind'] == 'grounded_rubric' else set()}
        if req['checker'].get('parameters',{}).get('execute_delivered_runbook'):
            runbook_key='runbook-'+req['checker']['parameters']['public_probe']
            allowed['drop_trusted_probe']=allowed['drop_trusted_probe']|{runbook_key}
            allowed['mutate_runbook_record']={runbook_key}
            allowed['append_runbook_claim']={'runbook.md'}
        if kind=='mutate_runbook_record' and control.get('mutation') not in ('stale_artifact','wrong_patch','failed_execution','missing_input'):
            raise ValueError('Invalid runbook counterexample')
        if req['checker'].get('parameters',{}).get('observed_change'):
            allowed.update(remove_event_reply={'transcript'},replace_event_reply={'transcript'})
        if req['checker'].get('parameters',{}).get('allowed_changed_roots'):
            allowed['add_changed_path']={'snapshot.files'}
        if req.get('template')=='student_learning_constraints_and_check' and not req['checker'].get('parameters',{}).get('allowed_changed_roots'):
            allowed['replace_learning_diff']={'snapshot.patch'}
        if kind=='replace_learning_diff':
            if control.get('violation') not in ('new_dependency','hardcoded_case') or not isinstance(control.get('content'),str) or not control['content'].startswith('diff --git ') or not isinstance(control.get('artifact_content'),str):
                raise ValueError('Learning counterexample needs a source diff and truthful self-check artifact')
        if kind=='add_changed_path':
            path=control.get('path')
            roots=req['checker']['parameters']['allowed_changed_roots']
            if (not isinstance(path,str) or not path or path.startswith('/') or '..' in path.split('/')
                    or any(path.startswith(root+'/') for root in roots)):
                raise ValueError('Changed-path control must identify an outside-scope relative path')
        measured={p['id'] for p in binding.get('public_probes',[]) if p.get('resource_measurement')}
        if req.get('public_probe') in measured:
            allowed['mutate_resource_measurement']={req['public_probe']}
        if kind=='mutate_resource_measurement' and control.get('mutation') not in ('missing','over_limit','oom','partial','wrong_workload'):
            raise ValueError('Invalid resource counterexample')
        if kind not in allowed or target not in allowed[kind]:
            raise ValueError(f'Invalid negative control mutation: {name}')
        if kind in ('replace_artifact', 'change_protected_file', 'replace_event_reply','append_runbook_claim') and not isinstance(control.get('content'), str):
            raise ValueError(f'Negative control needs content: {name}')
        if kind == 'replace_turns':
            turns = control.get('turns')
            if not isinstance(turns, list) or not turns or any(
                    not isinstance(t, dict) or t.get('speaker') != 'agent'
                    or not isinstance(t.get('content'), str) for t in turns):
                raise ValueError(f'Negative control needs agent transcript entries: {name}')
    missing = set(requirements) - covered
    if require_complete and missing:
        raise ValueError(f'Negative controls missing for {sorted(missing)}')
    for req in requirements.values():
        if req['checker'].get('parameters',{}).get('execute_delivered_runbook') and not any(
                c['requirement_id']==req['id'] and c['kind']=='append_runbook_claim' for c in controls):
            raise ValueError('Delivered-runbook calibration requires a semantic negative with valid replay evidence')
    return missing


def mutate(control, snapshot, trusted):
    snapshot, trusted = copy.deepcopy(snapshot), copy.deepcopy(trusted)
    kind, target = control['kind'], control['target']
    if kind == 'remove_artifact':
        snapshot['artifacts'][target] = None
    elif kind == 'replace_artifact':
        snapshot['artifacts'][target] = control['content']
    elif kind == 'append_runbook_claim':
        if not snapshot['artifacts'].get(target):raise ValueError('Semantic negative requires a positive runbook')
        snapshot['artifacts'][target]+='\n\n'+control['content']+'\n'
        # Existing proof is now stale. Only the calibration orchestrator may
        # replace it, after a fresh execution of the delivered command.
        for key in list(trusted):
            if trusted[key].get('version')=='delivered-runbook-v1':trusted.pop(key)
    elif kind == 'change_protected_file':
        snapshot['protected_files'][target] = control['content']
    elif kind == 'drop_trusted_probe':
        trusted.pop(target, None)
    elif kind == 'mutate_runbook_record':
        record=trusted[target];variant=control['mutation']
        if variant=='stale_artifact':record['artifact_sha256']='0'*64
        elif variant=='wrong_patch':record['patch_sha256']='0'*64
        elif variant=='missing_input':record['contract']['files']=[]
        elif variant=='failed_execution':
            run=record['execution_record'];run['passed']=False;run['execution']['code']=1
            if run.get('resource_measurement'):
                run['resource_measurement']['runs'][-1]['execution']['code']=1
    elif kind == 'replace_learning_diff':
        import hashlib
        snapshot['patch']=control['content']
        snapshot['patch_sha256']=hashlib.sha256(control['content'].encode()).hexdigest()
        snapshot['working_tree_patch']=snapshot['patch']
        snapshot['files']=[line.split(' b/',1)[1] for line in control['content'].splitlines() if line.startswith('diff --git ') ]
        snapshot['artifacts']['self-check.md']=control['artifact_content']
        snapshot['calibration_learning_violation']=control['violation']
        trusted={} # No observed execution is claimed for the synthetic source mutation.
    elif kind == 'replace_turns':
        snapshot['calibration_turns'] = copy.deepcopy(control['turns'])
    elif kind == 'add_changed_path':
        snapshot['files']=list(dict.fromkeys(snapshot.get('files',[])+[control['path']]))
    elif kind == 'mutate_resource_measurement':
        record=trusted[target];measurement=record['resource_measurement']
        variant=control['mutation']
        if variant=='missing':record.pop('resource_measurement')
        elif variant=='over_limit':
            value=measurement['runs'][-1]['measurement'];value['peak_bytes']=value['limit_bytes']+1
        elif variant=='oom':measurement['runs'][-1]['measurement']['events']['oom_kill']+=1
        elif variant=='partial':measurement['runs'][-1]['execution']['code']=1
        elif variant=='wrong_workload':measurement['probe_sha256']='wrong-workload'
        else:raise ValueError('Unsupported resource mutation')
        # Leave passed=True and other functional evidence untouched: the checker
        # must recompute acceptance from the actual measurement fields.
    else:
        raise ValueError(f'Unsupported negative control: {kind}')
    return snapshot, trusted
