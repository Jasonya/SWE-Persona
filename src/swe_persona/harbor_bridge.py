"""Use Harbor to own SWE-Pro event-profile task container lifecycles.

The source controller retains its command/snapshot/scoring semantics. There is
one Harbor-managed main container per trial; scoring snapshots stay isolated.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import uuid
import yaml

def install(dockerx, cache, workdir='/app', sample_labels=False):
    from harbor.environments.docker.docker import DockerEnvironment
    from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
    from harbor.models.trial.paths import TrialPaths
    original_remove=dockerx.Container.remove

    def start(self):
        root=Path(cache)/('harbor-'+uuid.uuid4().hex[:12]);root.mkdir(parents=True)
        envdir=root/'environment';envdir.mkdir()
        # All mounts are explicit source-controller assets. Never mount row.json,
        # model configuration, the entire dataset, or host credentials.
        volumes=[f'{m.source}:{m.target}'+(':ro' if m.read_only else '') for m in self.mounts]
        compose={'services':{'main':{'image':self.image,'container_name':self.name,
            'entrypoint':['/bin/bash','-c'],'command':['sleep infinity'],'working_dir':workdir,'volumes':volumes}}}
        if sample_labels:
            compose['services']['main']['network_mode']='none'
            compose['services']['main']['security_opt']=['no-new-privileges:true']
            owner=os.environ.get('PERSONACODE_SAMPLE_OWNER')
            if owner:
                compose['services']['main']['labels']={'personacode.sample':owner}
                compose['services']['harbor-docker-egress-control-sidecar']={
                    'labels':{'personacode.sample':owner,'personacode.fixture':'true'}}
        (envdir/'docker-compose.yaml').write_text(yaml.safe_dump(compose))
        paths=TrialPaths(root/'trial');paths.mkdir()
        # Harbor enforces egress policy; SWE-Live also declares literal network_mode: none.
        policy=NetworkPolicy(network_mode=NetworkMode.NO_NETWORK)
        self._harbor_loop=asyncio.new_event_loop()
        self._harbor_env=DockerEnvironment(environment_dir=envdir,environment_name='sp-'+root.name,
            session_id='sp-'+root.name,trial_paths=paths,task_env_config=EnvironmentConfig(
                docker_image=self.image,cpus=int(self.cpus),memory_mb=_memory_mb(self.memory)),
            network_policy=policy,phase_network_policies=[policy])
        try:
            launch=self._harbor_env.start(force_build=False)
            deadline=getattr(self,'deadline',None)
            if deadline:launch=asyncio.wait_for(launch,timeout=deadline.remaining())
            self._harbor_loop.run_until_complete(launch)
        except BaseException:
            try:self._harbor_loop.run_until_complete(self._harbor_env.stop(delete=False))
            finally:self._harbor_loop.close()
            raise
        return subprocess.check_output(['docker','inspect','--format','{{.Id}}',self.name],text=True).strip()

    def remove(self):
        if not hasattr(self,'_harbor_env'):return original_remove(self)
        if self._harbor_loop.is_closed():return
        try:self._harbor_loop.run_until_complete(self._harbor_env.stop(delete=False))
        finally:self._harbor_loop.close()
    dockerx.Container.start=start;dockerx.Container.remove=remove

def _memory_mb(value):
    value=str(value).lower()
    if value.endswith('g'):return int(float(value[:-1])*1024)
    if value.endswith('m'):return int(float(value[:-1]))
    return max(1,int(value)//(1024*1024))
