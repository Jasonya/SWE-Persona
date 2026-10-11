"""Native Terminus container plus a host-only observer for frozen snapshots.

Docker's cgroup freezer avoids SIGSTOP/SIGCHLD job-control side effects. The
observer shares only this task's PID namespace, has no host mounts or credentials,
no network, and reads the frozen root via /proc/1/root. It never runs the agent.
"""
import asyncio
import json
import shlex
import subprocess
from types import SimpleNamespace


class BoxEnvironment:
    def __init__(self, box, out):
        self.box=box;self.default_user=None;self.session_id=box.name
        self.trial_paths=SimpleNamespace(agent_dir=out)
        self.operations=[];self.paused=False;self.session=None
        self.observer=box.name+'-observer'
        self.cached_visible='';self.cached_full='';self.cached_alive=False

    def start_observer(self, image):
        from .sandbox import docker
        docker('run','-d','--name',self.observer,'--platform','linux/amd64',
               '--network','none','--pid=container:'+self.box.name,'--cap-add','SYS_PTRACE',
               '--cpus','0.5','--memory','512m','--pids-limit','64',
               '--entrypoint','sleep',image,'infinity')
        # Fail before any model call if this daemon cannot expose the frozen root.
        docker('exec',self.observer,'chroot','/proc/1/root','test','-d','/app')

    async def exec(self, command, *, timeout_sec=120, user=None, **kwargs):
        from harbor.environments.base import ExecResult
        if self.paused:raise RuntimeError('Live terminal exec attempted while task is frozen')
        args=['docker','exec','-w','/app']
        if user is not None:args+=['--user',str(user)]
        args += [self.box.name,'timeout','-k','5',str(timeout_sec or 120),'bash','-lc',command]
        def call():
            p=subprocess.run(args,capture_output=True,text=True,timeout=(timeout_sec or 120)+15)
            self.operations.append({'command':command,'return_code':p.returncode})
            return ExecResult(stdout=p.stdout,stderr=p.stderr,return_code=p.returncode)
        return await asyncio.to_thread(call)

    async def is_dir(self, path):
        return (await self.exec('test -d '+shlex.quote(str(path)))).return_code == 0

    async def freeze(self):
        if self.paused:return
        from .sandbox import docker, FROZEN_EXEC_OBSERVERS
        # Capture while the server can respond, then stop the WHOLE workload.
        # These cached observations are the last pre-freeze terminal view, not a
        # claim that an asynchronous command finished before the freeze.
        self.cached_visible=await self.session.capture_pane(False)
        self.cached_full=await self.session.capture_pane(True)
        self.cached_alive=await self.session.is_session_alive()
        await asyncio.to_thread(docker,'pause',self.box.name)
        self.paused=True
        FROZEN_EXEC_OBSERVERS[self.box.name]=self.observer

    def resume(self):
        if not self.paused:return
        from .sandbox import docker,FROZEN_EXEC_OBSERVERS
        docker('unpause',self.box.name)
        self.paused=False;FROZEN_EXEC_OBSERVERS.pop(self.box.name,None)

    def close(self):
        from .sandbox import docker,FROZEN_EXEC_OBSERVERS
        # Do not resume agent work after final evidence has been captured.
        FROZEN_EXEC_OBSERVERS.pop(self.box.name,None)
        docker('rm','-f',self.observer,check=False,timeout=20)
