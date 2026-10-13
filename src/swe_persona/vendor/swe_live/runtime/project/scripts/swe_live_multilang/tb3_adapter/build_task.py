"""Freeze a Harbor-shaped SWE task plus host-only persona rules before execution."""
import bootstrap_adapter
from bootstrap_adapter import ROOT, HERE
import argparse
import hashlib
import json
from pathlib import Path
import shutil
from registry import load_persona
from support import read, file_hash, REVISION
from protocol import (PROTOCOL, requirement, digest, JUDGE_PROMPT, SIM_PROMPT, UX_PROMPT, JUDGE_SCHEMA, SIM_SCHEMA,
    JUDGE_POLICY, CALIBRATION_POLICY)

BLUR = 'I need help fixing a problem in this repository. Please start by asking what you need to know.'


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def text(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding='utf-8')


def build(source, persona_id, destination, cap):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    identity = {'source_task_sha256': file_hash(source), 'persona_id': persona_id,
                'protocol': PROTOCOL, 'cap_seconds': cap, 'builder_sha256': file_hash(Path(__file__)),
                'protocol_sha256': file_hash(HERE/'protocol.py')}
    if destination.exists():
        if read(destination/'tests/persona/build-identity.json') != identity:
            raise ValueError('Frozen task binding changed')
        validate(destination)
        return destination
    row = json.loads(source.read_text(encoding='utf-8').strip())
    # This persona-independent blur is fixed before loading any persona.
    blur = {'text': BLUR, 'original_issue_sha256': hashlib.sha256(row['problem_statement'].encode()).hexdigest(),
            'created_before_persona': True, 'method': 'fixed persona-independent request; full issue disclosed at turn 2',
            'stages': ['clarify', 'full_original_issue_and_R1', 'judge_and_follow_up', 'independent_verification']}
    persona = load_persona(persona_id); req = requirement(persona)
    destination.mkdir(parents=True)
    p = destination/'tests/persona'; closed = p/'closed_loop'
    save(p/'build-identity.json', identity)
    text(destination/'README.md', '# '+row['instance_id']+'\n\n'+row['problem_statement']+'\n')
    text(destination/'instruction.md', BLUR+'\n')
    package_name='personacodebench/'+row['instance_id']
    base = ('schema_version = "1.3"\n[task]\nname = '+json.dumps(package_name)+'\n'
        '[metadata]\nsource = "SWE-bench-Live MultiLang"\nsource_instance_id = '+json.dumps(row['instance_id'])+'\nsource_revision = '+json.dumps(REVISION)+'\n'
        '[agent]\ntimeout_sec = '+str(float(cap))+'\nnetwork_mode = "no-network"\n'
        '[environment]\ndocker_image = '+json.dumps(row['docker_image'])+'\n'
        'os = "linux"\ncpus = 4\nmemory_mb = 16384\nstorage_mb = 20480\ngpus = 0\n'
        '[verifier]\ntimeout_sec = '+str(float(cap))+'\n')
    text(p/'original/task.toml', base)
    derived = base.replace('name = '+json.dumps(package_name), 'name = '+json.dumps(package_name+'--'+persona_id), 1)
    text(destination/'task.toml', derived+'\n[metadata.persona]\nprotocol = '+json.dumps(PROTOCOL)+
         '\nentrypoint = "tests/persona/closed_loop/manifest.json"\nrunner = "companion run.py"\n')
    text(p/'original/instruction.md', row['problem_statement']+'\n')
    text(destination/'environment/Dockerfile', 'FROM '+row['docker_image']+'\nWORKDIR /testbed\n')
    save(destination/'tests/native-task.json', row)
    shutil.copyfile(source, destination/'tests/native-task.jsonl')
    text(destination/'tests/test.patch', row.get('test_patch', ''))
    text(destination/'solution/solution.patch', row.get('patch', ''))
    text(destination/'solution/solve.sh', '#!/bin/sh\nset -eu\ncd /testbed\ngit apply /solution/solution.patch\n')
    save(destination/'tests/native-verifier.json', {k:row.get(k) for k in
        ('test_cmds','rebuild_cmds','print_cmds','log_parser','FAIL_TO_PASS','PASS_TO_PASS')})
    text(destination/'tests/test.sh', '#!/bin/sh\nset -eu\n'
        '# Native SWE grading is driven by the companion host runner, which preserves the pinned image/parser.\n'
        'if [ -z "${PCB_NATIVE_VERIFIER_COMMAND:-}" ]; then\n'
        '  echo "Use companion run.py for native SWE verification; no reward is available from a plain Harbor invocation." >&2\n'
        '  exit 2\nfi\nexec sh -c "$PCB_NATIVE_VERIFIER_COMMAND"\n')
    text(destination/'tests/README.md', 'Original SWE evaluator inputs are retained verbatim in native-task.json. '
         'Use the companion host runner for the pinned native evaluator and parser. A plain Harbor run does not activate '
         'the simulator or the native evaluator adapter. Never copy tests/ or solution/ into the agent image.\n')
    save(p/'persona.json', persona); save(p/'blurred_task.json', blur)
    save(p/'evolved_requirements.json', {'requirements':[req], 'coverage':req['coverage']})
    save(p/'interaction.json', {'opening':BLUR, 'preferences':persona['attributes']['interaction_preferences'],
        'incident_policy':'Do not activate invented key leaks, resets, flaky tests, database fixtures or other unsupported scenarios',
        'senior_phase_authorization':'turn 1 review-only; turn 2 explicitly authorizes local implementation'})
    binding = {'task_id':row['instance_id'], 'persona_id':persona_id, 'requirements':[req],
        'task_sha256':identity['source_task_sha256'], 'persona_sha256':digest(persona),
        'checker':'checker.py', 'validation':'deterministic shape controls; native gold preflight required at execution',
        'not_measured':'Full technical scenario templates and subjective truth of every handoff claim'}
    save(p/'binding.json', binding)
    # Self-contained checker module: no private inputs or LLM dependency.
    shutil.copyfile(HERE/'protocol.py', p/'checker.py')
    save(p/'protocol.json', {'protocol':PROTOCOL,'whole_sample_seconds':cap,'max_user_turns':24,
        'max_simulator_decisions':64,'closed_loop':'closed_loop/manifest.json','original_verifier':'native SWE evaluator',
        'test_patch_policy':'retained v2 evaluator-only projection; original official tests unchanged'})
    save(p/'provenance.json', {**identity,'dataset_revision':REVISION,'base_commit':row['base_commit'],
        'source_format':'SWE dataset row; no original Harbor task.toml exists',
        'original_toml_status':'Explicit adapter projection of native resources and experiment sample cap, not an upstream file',
        'source_license':'Upstream benchmark/repository licensing applies; project adapter MIT'})
    save(p/'closed-loop-provenance.json', {'source_protocol':'new task binding', 'target_protocol':PROTOCOL,
        'binding_sha256':file_hash(p/'binding.json'),'historical_relabeling':False,'live_validation':'not_executed'})
    save(p/'receipts/blur/receipt.json', {'sha256':file_hash(p/'blurred_task.json'),'method':blur['method']})
    save(p/'receipts/binding/receipt.json', {'binding_sha256':file_hash(p/'binding.json'),'status':'bound_shape_requirement',
        'objective_scope':req['coverage'],'semantic_or_scenario_controls':'not_claimed'})
    save(p/'receipts/source-protocol-v5.json', {'status':'not_applicable','reason':'Native SWE source; not a TB3 v5 migration'})
    save(closed/'rubric.json', {'requirement_id':'R1','disclosure':req['disclosure'],
        'criteria':[{'criterion_id':'R1.contract','text':req['disclosure']}], 'binding_sha256':file_hash(p/'binding.json')})
    save(closed/'policy.json', {'advance_only_on':'MET','partial_unmet':'request correction','unobservable':'request visible evidence',
        'final_recheck':True,'max_user_turns':24,'max_simulator_decisions':64,'whole_sample_seconds':cap,
        'judge_policy':JUDGE_POLICY,'calibration_policy':CALIBRATION_POLICY,
        'api_schema_evidence_error':'bounded recovery; unknown model assessment; independent literal checker fallback',
        'original_resources_preserved':True,'global_deadline':None,'timing_policy':'online censored P20; not exact global rank'})
    text(closed/'judge_prompt.md', JUDGE_PROMPT); text(closed/'simulator_prompt.md', SIM_PROMPT)
    save(closed/'response_schema.json', JUDGE_SCHEMA)
    save(closed/'simulator_schema.json', SIM_SCHEMA)
    save(closed/'experience_rubric.json', {'optional':True,'items':['expressed preferences','interaction usefulness'],
        'verdicts':['satisfied','violated','unsupported'],'satisfaction_range':[1,5]})
    text(closed/'experience_prompt.md', UX_PROMPT)
    save(closed/'experience_schema.json', {'type':'object','properties':{'satisfaction':{'type':'integer','minimum':1,'maximum':5}},
        'required':['satisfaction'],'additionalProperties':False})
    text(closed/'README.md', '# Host-only requirement loop\n\nLoad the frozen binding, rubric, schemas and prompts. '
        'Judge only qualified visible messages after disclosure. Validate registered evidence IDs and criterion identity. '
        'Unavailable model assessments remain unknown; the independent checker may resolve this complete literal contract. '
        'MET permits progression; final recheck and independent original/checker verdicts remain mandatory. '
        'This adapter binds one bounded handoff-format requirement, not the persona\'s entire technical scenario.\n')
    save(closed/'manifest.json', {'protocol':PROTOCOL,'binding_sha256':file_hash(p/'binding.json'),
        'files':{f.name:file_hash(f) for f in sorted(closed.iterdir()) if f.is_file()}})
    lock = {f.relative_to(destination).as_posix():file_hash(f) for f in sorted(destination.rglob('*')) if f.is_file()}
    save(p/'task-lock.json', lock)
    save(destination/'release-lock.json', lock)
    text(destination/'SHA256SUMS', ''.join(h+'  '+name+'\n' for name,h in lock.items()))
    validate(destination)
    return destination


def validate(directory):
    directory = Path(directory)
    for name, expected in read(directory/'tests/persona/task-lock.json').items():
        if file_hash(directory/name) != expected:
            raise ValueError('Frozen payload changed: '+name)
    import tomllib
    config = tomllib.loads((directory/'task.toml').read_text(encoding='utf-8'))
    harbor_status='not_installed'
    try:
        from harbor.models.task.config import TaskConfig
        from harbor.models.task.task import Task
    except ImportError:
        pass
    else:
        TaskConfig.model_validate_toml((directory/'task.toml').read_text(encoding='utf-8'))
        if not Task.is_valid_dir(directory): raise ValueError('Harbor task structure is invalid')
        harbor_status='passed'
    p=directory/'tests/persona'
    assert read(p/'closed_loop/manifest.json')['binding_sha256'] == file_hash(p/'binding.json')
    assert read(p/'blurred_task.json')['text'].strip() == (directory/'instruction.md').read_text().strip()
    return {'structure_valid':True,'protocol':PROTOCOL,'harbor_config_and_paths':harbor_status,
            'original_task':config['metadata']['source_instance_id'],'technical_scenario_coverage':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--task',type=Path,required=True); p.add_argument('--persona',required=True)
    p.add_argument('--output',type=Path,required=True); p.add_argument('--seconds',type=float,required=True)
    a=p.parse_args(); print(build(a.task,a.persona,a.output,a.seconds))
