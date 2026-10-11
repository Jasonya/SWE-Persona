"""Opt-in real Pier Trial. Imported only after selecting and verifying Pier."""
import asyncio
import json
import threading
from pathlib import Path
from pier.agents.base import BaseAgent
from pier.environments.docker.docker import DockerEnvironment
from pier.models.trial.config import TrialConfig
from pier.models.trajectories.trajectory import Trajectory
from pier.trial.trial import Trial
from .pier_box import PierBox
from .pier_trace import Trace, RecordingClient, RecordingBackend, export
from .runtime_backend import runtime_metadata

class PrivateDockerEnvironment(DockerEnvironment):
    def _default_log_mounts(self):
        # Host-owned dialogue, source identities and judgement must never be agent-writable.
        return []

class BenchmarkAgent(BaseAgent):
    SUPPORTS_ATIF=True
    @staticmethod
    def name():return 'dspilot-native-act'
    def version(self):return 'pier-benchmark-v1'
    def __init__(self,*args,benchmark_run_id,benchmark_arm,**kwargs):
        super().__init__(*args,**kwargs)
        self.run_id,self.arm=benchmark_run_id,benchmark_arm
    async def setup(self,environment):pass
    async def run(self,instruction,environment,context):
        from .api import Client
        from .coding_backend import create_backend
        from . import run_staged
        loop=asyncio.get_running_loop();cancelled=threading.Event()
        self.logs_dir.mkdir(parents=True,exist_ok=True)
        trace=Trace(self.logs_dir/'benchmark-events.jsonl')
        box=PierBox(environment,loop,cancelled)
        class BoundedClient(RecordingClient):
            def create(self,messages,**kwargs):
                if cancelled.is_set():raise RuntimeError('Pier cancellation forbids further model calls')
                return super().create(messages,**kwargs)
        def execute():
            return run_staged.episode(self.run_id,self.arm,
                client_factory=lambda *a,**k:BoundedClient(Client(*a,**k),trace),
                box_factory=lambda **k:box,
                backend_factory=lambda *a,**k:RecordingBackend(create_backend(*a,**k),trace))
        worker=asyncio.create_task(asyncio.to_thread(execute))
        result=None
        try:
            result=await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled.set()
            # Do not remove an environment while an in-flight host call still uses it.
            # Existing request/tool deadlines bound that call; no next model/tool call starts.
            try:await worker
            except Exception:pass
            raise
        finally:
            context.metadata={'benchmark_run_id':self.run_id,'arm':self.arm,
                'timing_scope':'Pier agent_execution includes the benchmark simulator/checks; use benchmark role timings for coding-only time',
                'grading':'Original benchmark evaluator remains authoritative; Pier verifier disabled to avoid double scoring'}
            if result is not None:
                context.metadata.update(benchmark_result=str(run_staged.ROOT/'runs'/self.run_id/'result.json'),
                                        evaluation_valid=result['evaluation_valid'])
            if any(e['kind'] in ('user','response') and (e['kind']=='user' or e['role']=='agent') for e in trace.events):
                trajectory=Trajectory.model_validate(export(trace.events,self.run_id,self.model_name))
                (self.logs_dir/'trajectory.json').write_text(trajectory.model_dump_json(indent=2,exclude_none=True))
        if result is not None and not result['evaluation_valid']:
            raise RuntimeError('Benchmark outcome invalid; original evidence retained')

def task_bundle(root, image, resources, total_timeout):
    """Generate only an orchestration shell; the full task never bypasses disclosure."""
    import toml
    root=Path(root);(root/'environment').mkdir(parents=True,exist_ok=False)
    (root/'tests').mkdir()
    (root/'instruction.md').write_text('Benchmark instructions are delivered only by the staged simulator.\n')
    # Pier checks for a verifier script even when verifier.disable=True. This script
    # deliberately cannot produce a score; actual unmodified grading stays host-owned.
    (root/'tests/test.sh').write_text('#!/bin/sh\necho "Use the host benchmark grader" >&2\nexit 64\n')
    memory=str(resources.get('memory','4g')).lower()
    factors={'g':1024,'m':1}
    if memory[-1:] not in factors:raise ValueError('Unsupported task memory unit')
    cfg={'version':'1.0','agent':{'timeout_sec':total_timeout},
         'verifier':{'timeout_sec':1},'environment':{'docker_image':image,'cpus':resources.get('cpus',2),
            'memory_mb':int(float(memory[:-1])*factors[memory[-1]]),'allow_internet':False,'workdir':'/app'}}
    (root/'task.toml').write_text(toml.dumps(cfg))
    main={'platform':'linux/amd64','pids_limit':resources.get('pids_limit',256),
          'deploy':{'resources':{'limits':{'pids':resources.get('pids_limit',256)}}},
          'logging':{'driver':'local','options':{'max-size':'10m','max-file':'2'}}}
    if resources.get('nofile_limit'):
        main['ulimits']={'nofile':{'soft':resources['nofile_limit'],'hard':resources['nofile_limit']}}
    # JSON is valid YAML. No host binds, secrets, reference solution or hidden tests.
    (root/'environment/docker-compose.yaml').write_text(json.dumps({'services':{'main':main}},indent=2))
    return root

async def run_trial(run_id,arm):
    from .task import ROOT,IMAGE,RESOURCES,EPISODE
    from .planning_staged import require_execution_admission
    from .api import config
    metadata=runtime_metadata();require_execution_admission(EPISODE)
    if (ROOT/'runs'/run_id).exists():raise ValueError('Benchmark run already exists')
    outer=ROOT/'runs/pier-trials'/run_id
    if outer.exists():raise ValueError('Pier trial already exists')
    # Controller budgets are unchanged. Pier's outer bound must cover setup,
    # public checks, original grading and final review, not silently truncate them.
    budget=EPISODE.binding.raw['staged']['budgets']
    timeout=budget['interaction_seconds']+budget.get('final_review_seconds',600)+7200
    bundle=task_bundle(ROOT/'runs/pier-tasks'/run_id,IMAGE,RESOURCES,timeout)
    cfg=config()
    trial=await Trial.create(TrialConfig.model_validate({
        'task':{'path':bundle},'trials_dir':ROOT/'runs/pier-trials','trial_name':run_id,
        'agent':{'import_path':'dspilot.pier_runtime:BenchmarkAgent','model_name':cfg['AGENT_MODEL'],
                 'kwargs':{'benchmark_run_id':run_id,'benchmark_arm':arm}},
        'environment':{'import_path':'dspilot.pier_runtime:PrivateDockerEnvironment','delete':True,
                       'kwargs':{'mounts_json':[]}},'verifier':{'disable':True}}))
    (trial.trial_dir/'runtime-identity.json').write_text(json.dumps(metadata,indent=2))
    result=await trial.run()
    if result.exception_info is not None:raise RuntimeError('Pier trial did not complete; inspect its retained exception and benchmark evidence')
    return json.loads((ROOT/'runs'/run_id/'result.json').read_text())

def run(run_id,arm):return asyncio.run(run_trial(run_id,arm))
