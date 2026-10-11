"""SWE-Live handoff-v2 materialization and isolated runtime integration."""
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from .api import RoleAPI, role_name
from .dataset import write

PROFILE='swe-bench-live'

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def adapter(root):return root/'runtime/project/scripts/swe_live_multilang/tb3_adapter'

def materialize_live(row,root,task):
    pp=task/'tests/persona';pp.mkdir(parents=True,exist_ok=True)
    extra=json.loads(row['interaction']['config_json'])
    native_file=task/'tests/native-task.jsonl';native=json.loads(native_file.read_text())
    if (native['instance_id']!=row['source']['instance_id'] or native['problem_statement']!=row['native_task']['instruction']
        or native['base_commit']!=row['environment']['expected_base_commit'] or native['docker_image']!=row['environment']['agent']['image']):
        raise ValueError('SWE-Live native payload and typed metadata differ')
    if (len(row['requirements'])!=1 or row['budget']['max_user_turns']!=24 or row['budget']['max_agent_queries_per_turn']!=14
        or row['budget']['max_simulator_decisions']!=64):raise ValueError('Unsupported handoff-v2 protocol limits')
    r=row['requirements'][0]
    if r['schedule']['earliest_turn']!=2 or r['schedule']['trigger_event']!='original_issue_disclosed':
        raise ValueError('Handoff-v2 must disclose at the original issue event on turn 2')
    req=dict(json.loads(r['runtime_config_json']),id=r['requirement_id'],disclosure=r['disclosure'],
             depends_on=r['depends_on'],checker=json.loads(r['checker_config_json']),
             trigger={'event':r['schedule']['trigger_event'],'earliest_turn':r['schedule']['earliest_turn']})
    persona=json.loads(row['persona']['profile_json'])
    write(pp/'persona.json',persona)
    write(pp/'interaction.json',json.loads(row['persona']['interaction_contract_json']))
    write(pp/'build-identity.json',{'protocol':extra['protocol'],'source_task_sha256':sha(native_file),
       'persona_id':row['persona']['persona_id'],'cap_seconds':row['budget']['episode_timeout_sec']})
    write(pp/'blurred_task.json',{'text':row['initial_message']})
    write(pp/'evolved_requirements.json',{'requirements':[req],'coverage':req['coverage']})
    write(pp/'binding.json',{'task_id':row['source']['instance_id'],'persona_id':persona['id'],'requirements':[req],
       'task_sha256':sha(native_file),'checker':'checker.py','source_content_sha256':row['content_sha256']})
    closed=pp/'closed_loop'
    write(closed/'rubric.json',{'requirement_id':r['requirement_id'],'disclosure':r['disclosure'],
       'criteria':json.loads(r['rubric_json']),'binding_sha256':sha(pp/'binding.json')})
    # Calibration and production must consume the same versioned judge prompt.
    import runpy
    prompts=runpy.run_path(str(adapter(root)/'protocol.py'))
    (closed/'judge_prompt.md').write_text(prompts['JUDGE_PROMPT'])
    write(closed/'policy.json',extra['policy'])
    write(closed/'manifest.json',{'protocol':extra['protocol'],'binding_sha256':sha(pp/'binding.json'),
       'files':{p.name:sha(p) for p in sorted(closed.iterdir()) if p.is_file() and p.name!='manifest.json'}})
    lock={p.relative_to(task).as_posix():sha(p) for p in sorted(task.rglob('*')) if p.is_file()}
    write(pp/'task-lock.json',lock)
    write(root/'row.json',{k:v for k,v in row.items() if k!='assets'})
    write(root/'inventory.json',{'tasks':[{'task_id':row['source']['task_id']}]})
    write(root/'release-manifest.json',{'release':'swe-persona-0.2.0','task_id':row['task_id'],
        'content_sha256':row['content_sha256'],'profile':PROFILE})
    return root


def install_live(root,config,out):
    """Called in every child worker as well as calibration; no inherited patches assumed."""
    sys.path.insert(0,str(adapter(root)))
    import bootstrap_adapter
    import llm
    if config:
        # BudgetClient copies _client with the remaining timeout per request. Retain
        # that interface while selecting the real provider separately for every role.
        class RoutedLlm:
            def __init__(self,model=None,call_log=None,**kwargs):
                self.model=model or config['judge']['model'];self.log=call_log or llm.CallLog()
                self._client=RoleAPI(config,'judge').client
            def text(self,messages,role='unspecified',max_output_tokens=4096,**kwargs):
                return RoleAPI(config,role_name(role),out/'api').raw(messages,max_tokens=max_output_tokens,
                    timeout=float(self._client.timeout))['text']
            def json_obj(self,messages,schema,schema_name='payload',role='unspecified',max_output_tokens=4096,**kwargs):
                return RoleAPI(config,role_name(role),out/'api').json(messages,schema,schema_name,max_output_tokens,
                    timeout=float(self._client.timeout))
        llm.Llm=RoutedLlm
    import support
    from .harbor_bridge import install
    install(SimpleNamespace(Container=support.Runtime),out/'environments',workdir='/testbed',sample_labels=True)
    return support


def execute(root,row,config,out,mode,config_path=None,gate=None,episode_args=None):
    from build_task import validate
    task=root/'tasks'/row['source']['task_id']
    if mode=='dry-run':
        status=validate(task)
        # Import the exact Linux native evaluator dependencies without a container.
        upstream=root/'runtime/project/.local/SWE-bench-Live'
        sys.path[:0]=[str(upstream),str(upstream/'launch')]
        from launch.core.platforms.linux import LinuxRuntime
        from evaluation.evaluation import main as native_main
        from run import main as episode_main
        print(json.dumps(dict(task_id=row['task_id'],status='pass',profile=PROFILE,
            backend='harbor-docker',native_evaluator_import='pass',validation=status)))
        return 0
    if mode=='calibrate':
        from calibrate_judge import calibrate
        calibrate(out,config['judge']['model']);return 0
    if mode=='live-episode':
        from run import main as episode_main
        return episode_main(['--task',str(task/'tests/native-task.jsonl'),'--persona',row['persona']['persona_id'],
            '--gold-dir',str(episode_args.gold_dir),'--binding-dir',str(task),'--model',config['agent']['model'],
            '--sample-seconds',str(episode_args.sample_seconds),'--deadline-monotonic',str(episode_args.deadline_monotonic),
            '--output-dir',str(episode_args.output_dir)])
    # The shared CLI has already calibrated this exact profile/API/runtime fingerprint.
    if gate is None or not json.loads((gate/'gate.json').read_text()).get('passed'):
        raise ValueError('A passed unified calibration gate is required')
    import docker
    from support import Manifest
    from run_fullset import get_image
    from run_study import ensure_gold
    from run_reference import gold_proof
    from whole_sample import supervise_sample
    from run_batch import clean,trial_projection
    manifest=Manifest(task/'tests/native-task.jsonl');cap=row['budget']['episode_timeout_sec']
    source={'key':row['source']['task_id'],'path':str(manifest.task_path),'sha256':manifest.task_sha256,'instance_id':manifest.task_id}
    client=docker.from_env(timeout=30)
    try:
        image,_=get_image(client,manifest)
        if ensure_gold(out,source,image.id,{'gold_seconds':row['budget']['verifier_timeout_sec']},False) is None:
            raise ValueError('Native gold preflight failed; no coding score')
        gold=out/'gold'/source['key'];gold_proof(gold,manifest,image.id)
    finally:client.close()
    trial=out/'trials'/source['key']/'persona'
    command=[sys.executable,'-B','-m','swe_persona.worker','--root',str(root),'--config',str(config_path.resolve()),
        '--output',str(out/'episode-runtime'),'--mode','live-episode','--gold-dir',str(gold)]
    receipt=supervise_sample(command,trial,cap,cleanup=clean);trial_projection(trial,receipt)
    # Prefer the supervisor projection to any partial/late worker result.
    write(out/'persona-result.json',json.loads((trial/'persona-result.json').read_text()))
    return 0 if receipt['status'] in ('completed','timeout') else 2
