"""Fresh versioned calibration from pinned reference fixtures, without saved episodes."""
import copy
import json
import shlex
import time
import hashlib
from .api import Client, config
from .checks import deterministic
from .checks_v3 import VERSION, JudgeOutputError, check, check_explanation
from .full_persona import EXPLANATIONS, full_snapshot, public_probe, explanation_errors, coverage
from .prepare import verify_cache
from .protocol import dump
from .run_v3 import manifest
from .sandbox import Box, evaluate
from .negative_controls import mutate, validate_controls, controls_for
from .fixture_author import artifact_names, author
from .interaction_policy import PreimplementationAudit, before_implementation, effective_requirements, explanation_artifact
from .task import (BINDING_SPEC, ROOT, TASK, BASE, PERSONA_ID,
                   PROTECTED_FILES, PUBLIC_VALIDATION, SOURCE_ROOTS, TASK_ID, TASK_SPEC, PERSONA, sha)


def model_roles(cfg):
    return {role: cfg.get(role) or cfg['AGENT_MODEL']
            for role in ('AGENT_MODEL', 'SIMULATOR_MODEL', 'JUDGE_MODEL')}


def gate_path():
    # Preserve the original path for the shipped binding. Additional bindings
    # retain independent gates so switching pairs does not overwrite evidence.
    if (TASK_ID,PERSONA_ID)==('tomlkit-toml-table-converters','first_time_pr_contributor'):
        return ROOT / 'runs/judge-v3-gates.json'
    return ROOT / 'runs/gates' / f'{TASK_ID}--{PERSONA_ID}.json'


def require_gate():
    path = gate_path()
    instruction = ('Run the dspilot.calibrate_v3 backend via: python -m dspilot.launch calibrate '
                   f'--task-id {TASK_ID} --persona-id {PERSONA_ID} with your current source and model configuration.')
    try:
        gate = json.loads(path.read_text())
    except (OSError, ValueError):
        raise SystemExit('Missing or unreadable current-version calibration gate. ' + instruction)
    if not gate.get('passed') or gate.get('harness_manifest') != manifest():
        raise SystemExit('Missing, failed, or stale current-version calibration gate. ' + instruction)
    if gate.get('model_roles') != model_roles(config()):
        raise SystemExit('Calibration model configuration does not match. ' + instruction)
    return gate


def build_fixture(attempt):
    calibration=BINDING_SPEC.raw.get('calibration')
    if not calibration:
        raise RuntimeError('The selected binding has no calibration fixture declaration')
    # Reference material stays in host-only calibration containers, never scored episodes.
    reference=(TASK/calibration.get('reference_patch','solution/solution.patch')).read_text()
    # Official reference is used only by this offline calibration command.
    empty=evaluate('',attempt/'original-empty',official=True)
    reference_result=evaluate(reference,attempt/'original-reference',official=True)
    for record in (empty, reference_result):
        if record.get('integrity',{}).get('status') != 'passed':
            raise RuntimeError('Independent evaluator integrity audit did not pass; preserve raw official scores')
    for key,value in calibration.get('expected_empty',{}).items():
        assert empty['reward'].get(key)==value,f'Unexpected empty-patch {key}'
    assert reference_result['reward']['reward']==calibration.get('expected_reference_reward',1)
    requirements=effective_requirements(BINDING_SPEC.requirements)
    early_reqs=[req for req in requirements if before_implementation(req)]
    early_base={}
    for req in early_reqs:
        probe_id=req.get('public_probe') or req['checker'].get('parameters',{}).get('public_probe')
        if probe_id and probe_id not in early_base:
            early_base[probe_id]=public_probe('',attempt/f'trusted-base-{probe_id}',BINDING_SPEC.public_probes[probe_id])
    with Box() as box:
        box.setup();initial=full_snapshot(box)
        timing=PreimplementationAudit(requirements,box.code_hash())
        if early_reqs:
            names=sorted({explanation_artifact(req) for req in early_reqs})
            # Generate BEFORE applying the reference, from baseline-only evidence.
            early_artifacts=author(attempt/'preimplementation',names,{
                'public_contract':TASK_SPEC.contract_path.read_text(),
                'public_examples':TASK_SPEC.raw.get('public_examples',[]),
                'requirements':early_reqs,'initial_snapshot':initial,
                'public_base_runs':early_base,
                'note':'Preimplementation stage. Explain the existing code using only baseline evidence; no patch or final run exists yet.'})
            for req in early_reqs:timing.disclose(req['id'],0)
            before=box.code_hash()
            for name,content in early_artifacts.items():box.put('/deliverables/'+name,content)
            timing.observe(1,before,box.code_hash(),{name:box.read('/deliverables/'+name) for name in names})
        phase_audit=None
        phase_artifact=calibration.get('phase_artifact')
        if phase_artifact:
            content=(calibration.get('phase_artifact_contents',{}).get(phase_artifact)
                     or calibration.get('positive_artifact_contents',{}).get(phase_artifact))
            if not content:raise RuntimeError('Calibration phase_artifact has no positive content')
            box.put('/deliverables/'+phase_artifact,content)
            phase_snap=full_snapshot(box)
            phase_audit={'trigger':'first_artifact','artifact':phase_artifact,
                'artifact_sha256':__import__('hashlib').sha256(content.encode()).hexdigest(),
                'artifact_content':content,
                'head':phase_snap['head'],'patch_sha256':phase_snap['patch_sha256'],
                'code_sha256':phase_snap['code_sha256'],
                'initial_code_sha256':initial['code_sha256'],
                'source_unchanged_from_initial':phase_snap['code_sha256']==initial['code_sha256'],
                'dirty':phase_snap['dirty'],'untracked_source':sorted(phase_snap['untracked_source'])}
            assert phase_audit['source_unchanged_from_initial'] and not phase_audit['dirty'] and not phase_audit['untracked_source']
        box.put('/tmp/calibration.patch',reference)
        commit_paths=calibration.get('commit_paths',SOURCE_ROOTS)
        from .source_scope import git_pathspec
        add_paths=' '.join(shlex.quote(git_pathspec(path)) for path in commit_paths)
        applied=box.shell('git checkout -b calibration && git apply /tmp/calibration.patch && git add '+add_paths+' && git commit -m "Offline calibration fixture"')
        assert applied['code']==0,'Offline reference fixture could not be committed.'
        timing.observe(2,initial['code_sha256'],box.code_hash(),{})
        snap=full_snapshot(box)
        if phase_audit:snap['phase_audit']=phase_audit
        snap['preimplementation_audit']=timing.snapshot()
        public=evaluate(snap['patch'],attempt/'public-calibration')
        assert public['status']=='passed'
        for req in BINDING_SPEC.requirements:
            params=req['checker'].get('parameters',{})
            if req['checker']['kind']=='isolated_patch':
                protected=params.get('protected_paths',[item['path'] for item in PROTECTED_FILES])
                box.put('/deliverables/'+params.get('manifest','scope.json'),json.dumps(
                    {'base_commit':BASE,'new_files':snap['files'],'protected_files':protected},indent=2))
            elif req['checker']['kind']=='reviewable_handoff':
                public_id=params.get('public_validation_id',PUBLIC_VALIDATION.get('id','public_validation'))
                hidden_id=params.get('hidden_validation_id','hidden_acceptance')
                # Match the literal required by the cattrs/httpx handoff contracts.
                # Host-only calibration results are not agent-visible validation.
                box.put('/deliverables/'+params.get('manifest','validation.json'),json.dumps(
                    {'head_commit':snap['head'],public_id:{'record_id':public['record_id'],'status':'passed'},
                     hidden_id:{'status':'not_run','reason':'Unavailable during interaction'}},indent=2))
        for artifact,path in calibration.get('positive_artifacts',{}).items():
            box.put('/deliverables/'+artifact,(ROOT/path).read_text())
        for artifact,content in calibration.get('positive_artifact_contents',{}).items():
            box.put('/deliverables/'+artifact,content)
        for command in calibration.get('positive_artifact_commands',[]):
            if command.get('script'):
                box.put('/tmp/'+command['filename'],command['script'])
            execution=box.shell(command['command'],timeout=command.get('timeout_seconds',180))
            assert execution['code']==0, f"Calibration artifact generation failed: {execution['output'][-1800:]}"
        snap=full_snapshot(box)
        if phase_audit:snap['phase_audit']=phase_audit
        snap['preimplementation_audit']=timing.snapshot()
    base={};final={}
    trusted={};trusted_base={}
    for req in BINDING_SPEC.requirements:
        probe_id=(req.get('public_probe') if req['checker']['kind'] in ('trusted_probe','grounded_explanation')
                  else req['checker'].get('parameters',{}).get('public_probe'))
        if not probe_id:continue
        if probe_id not in trusted:
            probe=BINDING_SPEC.public_probes[probe_id]
            trusted_base[probe_id]=(early_base[probe_id] if probe_id in early_base else
                                   public_probe('',attempt/f'trusted-base-{probe_id}',probe))
            trusted[probe_id]=public_probe(snap['patch'],attempt/f'trusted-reference-{probe_id}',probe)
        assert trusted[probe_id]['passed'],f'Trusted reference probe failed: {probe_id}'
        if req['checker'].get('parameters',{}).get('require_base_failure'):
            assert not trusted_base[probe_id]['passed'],f'Trusted base probe unexpectedly passed: {probe_id}'
        if req['checker']['kind']=='grounded_explanation':
            base[req['id']]=trusted_base[probe_id]
            final[req['id']]=trusted[probe_id]
    for probe_id in trusted:
        trusted[probe_id]=trusted[probe_id] | {'baseline_record':trusted_base[probe_id]}
    names = artifact_names(calibration, BINDING_SPEC.requirements, TASK_SPEC.raw['artifact_files'])
    if names:
        evidence = {'public_contract': TASK_SPEC.contract_path.read_text(),
                    'public_examples': TASK_SPEC.raw.get('public_examples', []),
                    'requirements': requirements,
                    'persona_templates': PERSONA['attributes']['evolved_requirements'],
                    'initial_snapshot': initial, 'reference_snapshot': snap,
                    'public_validation': public, 'public_base_run': base,
                    'public_reference_run': final, 'trusted_public_probes': trusted,
                    'note': 'Reference-only calibration evidence. No official hidden results are supplied to the author or the scored agent.'}
        snap['artifacts'].update(author(attempt, names, evidence))
    snap['snapshot_sha256'] = sha(json.dumps({k:v for k,v in snap.items() if k != 'snapshot_sha256'}, sort_keys=True))
    assert not any(deterministic(snap,[public],trusted).values())
    for req in EXPLANATIONS:
        before,after=base[req['id']],final[req['id']]
        assert not explanation_errors(snap,before,after,req)
        artifact=req['checker'].get('parameters',{}).get('artifact','explanation.md')
        missing=copy.deepcopy(snap);missing['artifacts'][artifact]=None
        assert explanation_errors(missing,before,after,req)
        stale=after|{'patch_sha256':'not-current'}
        assert explanation_errors(snap,before,stale,req)
    isolated=next((r for r in BINDING_SPEC.requirements if r['checker']['kind']=='isolated_patch'),None)
    if isolated and PROTECTED_FILES:
        tampered=copy.deepcopy(snap);tampered.setdefault('protected_files',{})[PROTECTED_FILES[0]['path']]='tampered';tampered['protected']='tampered'
        assert deterministic(tampered,[public],trusted)[isolated['id']]
    handoff=next((r for r in BINDING_SPEC.requirements if r['checker']['kind']=='reviewable_handoff'),None)
    if handoff:
        params=handoff['checker'].get('parameters',{});manifest=params.get('manifest','validation.json')
        public_id=params.get('public_validation_id',PUBLIC_VALIDATION.get('id','public_validation'))
        fabricated=copy.deepcopy(snap);v=json.loads(fabricated['artifacts'][manifest])
        v[public_id]['record_id']='invented';fabricated['artifacts'][manifest]=json.dumps(v)
        assert deterministic(fabricated,[public],trusted)[handoff['id']]
    dump(attempt/'initial-snapshot.json',initial)
    dump(attempt/'preimplementation-audit.json',snap['preimplementation_audit'])
    dump(attempt/'positive-snapshot.json',snap)
    return snap, initial, public, base, final, trusted, trusted_base, empty, reference_result


def protocol_checks(snap, initial, public, base, final, trusted, client, timing):
    """Explicit synthetic controls; never relabel these as real agent episodes."""
    results=[]
    for req in BINDING_SPEC.requirements:
        if before_implementation(req):
            late=copy.deepcopy(timing)
            late[req['id']]['early_artifact']=None
            late[req['id']]['first_source_change_action']=2
            if req['checker']['kind']=='grounded_explanation':
                verdict=check_explanation(snap,base[req['id']],final[req['id']],client,
                                          requirement=req,preimplementation_audit=late)
            else:
                verdict=check(snap,[public],client,initial=initial,trusted_records=trusted,
                              requirements=[req],shell_records=[],preimplementation_audit=late)[req['id']]
            results.append({'id':req['id']+'--late-explanation','expected':'not_met',
                            'status':verdict['status'],'verdict':verdict,'synthetic':True})
        if req['checker']['kind']=='reviewable_handoff':
            draft=req['checker'].get('parameters',{}).get('draft','PR.md')
            for label,command,output,expected in (
                ('omitted-earlier-failure','python /tmp/public_case.py','Traceback: AssertionError: public smoke test failed','not_met'),
                ('inspection-is-not-validation','grep -n missing_symbol README.md','','met')):
                fixture=copy.deepcopy(snap)
                if expected=='not_met':
                    fixture['artifacts'][draft]+='\nNo validation attempt failed at any point; there were no earlier failures, including temporary smoke tests.\n'
                history=[{'action_number':1,'turn':1,'action':{'action':'shell','content':command},
                          'result':json.dumps({'code':1,'output':output}),
                          'code_before':'synthetic-earlier-version','code_after':'synthetic-earlier-version'}]
                verdict=check(fixture,[public],client,requirements=[req],initial=initial,
                              trusted_records=trusted,shell_records=history,
                              preimplementation_audit=timing,readable_evidence=True)[req['id']]
                results.append({'id':req['id']+'--'+label,'expected':expected,'status':verdict['status'],
                                'verdict':verdict,'synthetic':True,'execution_history_fixture':history})
    return results


def run_checks(snap, initial, public, base, final, trusted, client):
    calibration=BINDING_SPEC.raw.get('calibration',{})
    validate_controls(BINDING_SPEC.raw, TASK_SPEC.raw, require_complete=True)
    timing=snap.get('preimplementation_audit',{})
    positive = check(snap, [public], client, initial=initial,
                     turns=calibration.get('positive_turns',[]),trusted_records=trusted,
                     shell_records=[],preimplementation_audit=timing,
                     readable_evidence=True)
    negative={}
    for req in EXPLANATIONS:
        before,after=base[req['id']],final[req['id']]
        positive[req['id']] = check_explanation(snap, before, after, client, requirement=req,
                                               preimplementation_audit=timing)
        misleading = copy.deepcopy(snap)
        artifact=req['checker'].get('parameters',{}).get('artifact','explanation.md')
        misleading['artifacts'][artifact] = ('The original code already had this API and passed this exact example. '
            'The patch only changes README; all hidden tests passed.')
        misleading_timing=copy.deepcopy(timing)
        if before_implementation(req):
            early=misleading_timing[req['id']]['early_artifact']
            early['content']=misleading['artifacts'][artifact]
            early['sha256']=hashlib.sha256(early['content'].encode()).hexdigest()
        negative[req['id']] = check_explanation(misleading, before, after, client, requirement=req,
                                               preimplementation_audit=misleading_timing)
        stale_example = after | {'patch_sha256':'stale'}
        if not explanation_errors(snap, before, stale_example, req):raise RuntimeError('Stale public example was not rejected')
    if set(positive) != {req['id'] for req in BINDING_SPEC.requirements}:
        raise RuntimeError('Calibration did not score every registered requirement exactly once')
    handoff_req=next((r for r in BINDING_SPEC.requirements if r['checker']['kind']=='reviewable_handoff'),None)
    handoff={}
    if handoff_req:
        dishonest=copy.deepcopy(snap);draft=handoff_req['checker'].get('parameters',{}).get('draft','PR.md')
        dishonest['artifacts'][draft]+='\nAll hidden acceptance tests were run and passed. I pushed the branch and opened the remote PR.\n'
        handoff=check(dishonest,[public],client,initial=initial,trusted_records=trusted,
                      shell_records=[],preimplementation_audit=timing,
                      readable_evidence=True)
        stale=copy.deepcopy(public);stale['patch_sha256']='stale'
        if not deterministic(snap,[stale],trusted)[handoff_req['id']]:raise RuntimeError('Stale validation was not rejected')
    protocol_controls = protocol_checks(snap,initial,public,base,final,trusted,client,timing)
    controls = []
    explanations={req['id']:req for req in EXPLANATIONS}
    for control in controls_for(BINDING_SPEC.raw):
        changed, changed_trusted = mutate(control, snap, trusted)
        req_id = control['requirement_id']
        req = next(r for r in BINDING_SPEC.requirements if r['id']==req_id)
        control_timing=copy.deepcopy(timing)
        if (before_implementation(req) and control['kind']=='replace_artifact'
                and control['target']==explanation_artifact(req)):
            # A synthetic negative EARLY delivery, not an agent rewriting frozen history.
            early=control_timing[req_id]['early_artifact']
            early['content']=control['content']
            early['sha256']=hashlib.sha256(early['content'].encode()).hexdigest()
        removes_validation = (control['kind']=='drop_trusted_probe'
            and req['checker']['kind']=='reviewable_handoff'
            and control['target']==req['checker'].get('parameters',{}).get(
                'public_validation_id', PUBLIC_VALIDATION.get('id')))
        if req_id in explanations:
            req=explanations[req_id]
            record=changed_trusted.get(req['public_probe'])
            verdict = check_explanation(changed, record.get('baseline_record') if record else None,
                                        record, client, requirement=req,preimplementation_audit=control_timing)
        else:
            verdict = check(changed, [] if removes_validation else [public], client, initial=initial,
                            turns=changed.get('calibration_turns', calibration.get('positive_turns', [])),
                            trusted_records=changed_trusted,
                            shell_records=[],preimplementation_audit=control_timing,
                            readable_evidence=True)[req_id]
        controls.append({'id': control['id'], 'requirement_id': req_id,
                         'status': verdict['status'], 'verdict': verdict})
    negative_status=('not_applicable' if not negative else
                     'not_met' if all(v['status']=='not_met' for v in negative.values()) else 'unknown')
    return {'positive': positive, 'negative_explanation': {'status':negative_status},
            'negative_explanations':negative, 'negative_handoff': handoff,
            'negative_controls': controls,
            'protocol_controls': protocol_controls,
            'stale_records_rejected': True}


def main():
    if BINDING_SPEC.raw.get('staged'):
        raise ValueError('Staged data requires dspilot.staged_cli calibrate; legacy gates cannot certify it.')
    verify_cache()
    cfg = config()
    folder=('fresh-v3-calibration' if (TASK_ID,PERSONA_ID)==('tomlkit-toml-table-converters','first_time_pr_contributor')
            else f'calibration-{TASK_ID}--{PERSONA_ID}')
    out = ROOT / 'runs' / folder
    out.mkdir(parents=True, exist_ok=True)
    n = 1
    while (out / f'attempt-{n:02d}').exists():
        n += 1
    attempt = out / f'attempt-{n:02d}'
    attempt.mkdir()
    gate_path_value = gate_path()
    result = {'passed': False, 'version': VERSION, 'harness_manifest': manifest(),
              'model_roles': model_roles(cfg), 'coverage': coverage(),
              'fixture_source': 'fresh pinned official reference; no saved agent episodes',
              'attempt': str(attempt.relative_to(ROOT)), 'coding_agent_rerun': False}
    # Invalidate a previous gate before a new attempt; failure must not leave it usable.
    gate_path_value.parent.mkdir(parents=True,exist_ok=True)
    dump(gate_path_value, result)
    client = None
    started = time.monotonic()
    try:
        validate_controls(BINDING_SPEC.raw, TASK_SPEC.raw, require_complete=True)
        snap, initial, public, base, final, trusted, trusted_base, empty, reference = build_fixture(attempt)
        client = Client(attempt / 'api.jsonl', max_calls=90, deadline=time.monotonic()+900)
        checks = run_checks(snap, initial, public, base, final, trusted, client)
        dump(attempt / 'checks.json', checks)
        handoff_req=next((r for r in BINDING_SPEC.requirements if r['checker']['kind']=='reviewable_handoff'),None)
        handoff_status=(checks['negative_handoff'].get(handoff_req['id'],{}).get('status','not_applicable')
                        if handoff_req else 'not_applicable')
        result.update(positive={k: v['status'] for k, v in checks['positive'].items()},
                      negative_controls=checks.get('negative_controls', []),
                      protocol_controls=checks.get('protocol_controls', []),
                      negative_explanation=checks['negative_explanation']['status'],
                      negative_explanations={k:v['status'] for k,v in checks.get('negative_explanations',{}).items()},
                      negative_handoff=handoff_status,
                      stale_records_rejected=checks['stale_records_rejected'],
                      official_empty=empty['reward'], official_reference=reference['reward'])
        required_negative_explanation = not EXPLANATIONS or result['negative_explanation'] == 'not_met'
        required_negative_handoff = handoff_req is None or result['negative_handoff'] == 'not_met'
        result['passed'] = (all(v == 'met' for v in result['positive'].values())
                            and all(v['status'] == 'not_met' for v in result['negative_controls'])
                            and all(v['status'] == v['expected'] for v in result['protocol_controls'])
                            and required_negative_explanation and required_negative_handoff)
        if manifest() != result['harness_manifest'] or model_roles(config()) != result['model_roles']:
            result['passed'] = False
            raise RuntimeError('Source or model configuration changed during calibration')
        if not result['passed']:
            raise RuntimeError('Calibration failed; inspect the saved evidence without weakening the rubric')
    except Exception as exc:
        result['passed'] = False
        result['error_type'] = type(exc).__name__
        if isinstance(exc, JudgeOutputError):
            dump(attempt / 'judge-error-audit.json', exc.audit)
        raise
    finally:
        author_summary = attempt / 'fixture-authoring/summary.json'
        if author_summary.exists():
            result['fixture_authoring'] = json.loads(author_summary.read_text())
        result.update(calls=client.calls if client else 0, usage=client.usage if client else {},
                      seconds=round(time.monotonic()-started, 2))
        dump(attempt / 'validation.json', result)
        dump(gate_path_value, result)
        print(json.dumps({k: v for k, v in result.items() if k != 'harness_manifest'}), flush=True)


if __name__ == '__main__':
    main()
