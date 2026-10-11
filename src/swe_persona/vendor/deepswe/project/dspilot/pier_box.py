"""Borrow a Pier-owned environment while preserving Box's evidence semantics."""
import asyncio
import json
import shlex
import tempfile
import time
from pathlib import Path
from .sandbox import Box
from .coding_budget import bounded_timeout, suspended, active_deadline
from . import source_scope, workspace_evidence, task_workflow
from .task import BASE, SOURCE_ROOTS

class PierBox(Box):
    def __init__(self, environment, loop, cancelled):
        self.environment, self.loop, self.cancelled = environment, loop, cancelled

    def _await(self, coroutine):
        if self.cancelled.is_set():
            coroutine.close()
            raise RuntimeError('Pier trial cancelled; no further environment actions')
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result()

    def _exec(self, command, timeout=300, cwd=None, check=True):
        started=time.monotonic()
        # Merge streams inside the container, as docker exec did in the legacy path.
        r=self._await(self.environment.exec(command='{ '+command+'; } 2>&1',cwd=cwd,timeout_sec=bounded_timeout(timeout)))
        value={'code':r.return_code,'output':(r.stdout or '')+(r.stderr or ''),
               'seconds':round(time.monotonic()-started,3)}
        if check and value['code']:
            raise RuntimeError('Pier environment operation failed: '+value['output'][-1800:])
        return value

    def shell(self, script, timeout=120):
        limited = active_deadline() is not None
        timeout=bounded_timeout(timeout)
        command=shlex.join(['timeout','-k','1' if limited else '5',str(timeout),'bash','-lc',script])
        with suspended():
            return self._exec(command,timeout+(2 if limited else 15),cwd='/app',check=False)

    def put(self, path, content):
        # No credential or trusted evidence directories are mounted into the agent.
        with tempfile.TemporaryDirectory(prefix='dspilot-pier-upload-') as folder:
            source=Path(folder)/'content'
            source.write_bytes(content.encode() if isinstance(content,str) else content)
            self._await(self.environment.upload_file(source,path))

    def read(self, path):
        script='import os,sys; p=sys.argv[1]; assert not os.path.islink(p); d=open(p,"rb").read(262145); assert len(d)<=262144; sys.stdout.buffer.write(d)'
        r=self._exec(shlex.join(['python3','-c',script,path]),check=False)
        return r['output'] if r['code']==0 else None

    def code_hash(self):
        script=Path(source_scope.__file__).read_text()+"\nimport json,sys; print(source_digest('/app',json.loads(sys.argv[1])))\n"
        return self._exec(shlex.join(['python3','-c',script,json.dumps(SOURCE_ROOTS)]))['output'].strip()

    def workspace_snapshot(self):
        script=Path(workspace_evidence.__file__).read_text()+'\nimport json; print(json.dumps(capture("/app")))\n'
        return json.loads(self._exec(shlex.join(['python3','-c',script]))['output'])

    def workflow_snapshot(self):
        script=Path(task_workflow.__file__).read_text()+'\nimport json,sys; print(json.dumps(capture("/app",sys.argv[1])))\n'
        return json.loads(self._exec(shlex.join(['python3','-c',script,BASE]))['output'])

    def close(self):
        # Ownership belongs to Pier Trial, including exception cleanup.
        pass
