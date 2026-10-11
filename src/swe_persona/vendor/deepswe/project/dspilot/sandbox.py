"""Isolated Docker execution; host owns credentials, tests and trustworthy records."""
import json
from contextlib import nullcontext
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from .task import (ARTIFACT_FILES, BASE, IMAGE, OFFICIAL_EVALUATION,
                   PROTECTED, PROTECTED_FILES, PROTECTED_TEXT,
                   PUBLIC_VALIDATION, RESOURCES, SOURCE_ROOTS, TASK, sha)
from .coding_budget import bounded_timeout, suspended, active_deadline
from . import integrity
from . import source_scope

def command(args, *, timeout=300, data=None):
    started = time.monotonic()
    try:
        p = subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=bounded_timeout(timeout))
        return {'code':p.returncode, 'output':p.stdout.decode(errors='replace'), 'seconds':round(time.monotonic()-started,3)}
    except subprocess.TimeoutExpired:
        return {'code':124, 'output':'Command timed out; container process will be terminated.', 'seconds':round(time.monotonic()-started,3)}

# Populated only by the optional native Terminus lifecycle. Other containers,
# including public/official evaluation, retain the original execution path.
FROZEN_EXEC_OBSERVERS = {}


def frozen_exec_args(args):
    if not args or args[0]!='exec':return args
    index=1;stdin=False;cwd='/'
    while index<len(args) and str(args[index]).startswith('-'):
        if args[index]=='-i':stdin=True;index+=1
        elif args[index]=='-w':cwd=str(args[index+1]);index+=2
        else:return args
    if index>=len(args) or args[index] not in FROZEN_EXEC_OBSERVERS:return args
    observer=FROZEN_EXEC_OBSERVERS[args[index]]
    return ('exec',*(['-i'] if stdin else []),observer,'chroot','/proc/1/root',
            '/bin/bash','-c','cd "$1" && shift && exec "$@"','observer',cwd,*args[index+1:])


def docker(*args, timeout=300, data=None, check=True):
    args=frozen_exec_args(args)
    r = command(['docker', *map(str,args)], timeout=timeout, data=data)
    if check and r['code']:
        raise RuntimeError(f'Docker operation failed ({r["code"]}): {r["output"][-1800:]}')
    return r

class Box:
    def __init__(self, verifier_tests=None, *, image=None):
        self.name = 'deepswe-persona-' + uuid.uuid4().hex[:12]
        from .runtime_backend import selected_runtime
        if selected_runtime() == 'harbor':
            from .harbor_runtime import start
            start(self,verifier_tests,image or IMAGE,RESOURCES)
            return
        args=['run','-d','--runtime=runc','-e','NVIDIA_VISIBLE_DEVICES=void','-e','CUDA_VISIBLE_DEVICES=','--name',self.name,'--platform','linux/amd64','--network','none',
              '--cpus',str(RESOURCES.get('cpus',2)),'--memory',str(RESOURCES.get('memory','4g')),
              '--pids-limit',str(RESOURCES.get('pids_limit',256))]
        if verifier_tests is not None:
            args += ['--cap-drop','ALL','--security-opt','no-new-privileges:true',
                     '--mount',f'type=bind,source={Path(verifier_tests).resolve()},target=/tests,readonly']
        if RESOURCES.get('nofile_limit'):
            limit=str(RESOURCES['nofile_limit'])
            args += ['--ulimit',f'nofile={limit}:{limit}']
        docker(*args,image or IMAGE,'sleep','infinity')
    def shell(self, script, timeout=120):
        # GNU timeout kills process groups inside the container as well as host wait bounds.
        limited = active_deadline() is not None
        timeout = bounded_timeout(timeout)
        with suspended():
            return docker('exec','-w','/app',self.name,'timeout','-k','1' if limited else '5',str(timeout),'bash','-lc',script, timeout=timeout+(2 if limited else 15),check=False)
    def put(self, path, content):
        import shlex
        r = docker('exec','-i',self.name,'bash','-lc','cat > '+shlex.quote(path), data=content.encode() if isinstance(content,str) else content)
        return r
    def read(self, path):
        # Python regular-file check avoids dereferencing a malicious deliverable symlink.
        script = 'import os,sys; p=sys.argv[1]; assert not os.path.islink(p); d=open(p,"rb").read(262145); assert len(d)<=262144; sys.stdout.buffer.write(d)'
        r = docker('exec',self.name,'python3','-c',script,path,check=False)
        return r['output'] if r['code']==0 else None
    def setup(self):
        r = self.shell(f'git config --global --add safe.directory /app && git config user.name Pilot && git config user.email pilot@local && git checkout -B main {BASE} && mkdir -p /deliverables')
        if r['code']: raise RuntimeError(r['output'])
        for fixture in PROTECTED_FILES:
            parent = str(Path('/app', fixture['path']).parent)
            self.shell('mkdir -p ' + __import__('shlex').quote(parent))
            self.put('/app/'+fixture['path'], fixture['content'])
    def code_hash(self):
        script=Path(source_scope.__file__).read_text()+"\nimport json,sys; print(source_digest('/app',json.loads(sys.argv[1])))\n"
        r=docker('exec',self.name,'python3','-c',script,json.dumps(SOURCE_ROOTS))
        return r['output'].strip()
    def workspace_snapshot(self):
        from . import workspace_evidence
        script=Path(workspace_evidence.__file__).read_text()+'\nimport json; print(json.dumps(capture("/app")))\n'
        result=docker('exec',self.name,'python3','-c',script)
        return json.loads(result['output'])

    def workflow_snapshot(self):
        from . import task_workflow
        script=Path(task_workflow.__file__).read_text()+'\nimport json,sys; print(json.dumps(capture("/app",sys.argv[1])))\n'
        result=docker('exec',self.name,'python3','-c',script,BASE)
        return json.loads(result['output'])

    def snapshot(self):
        patch = self.shell(f'git diff --binary {BASE} HEAD')['output']
        files = [p for p in self.shell(f'git diff --no-renames --name-only -z {BASE} HEAD')['output'].split('\0') if p]
        head = self.shell('git rev-parse HEAD')['output'].strip()
        work = [p for p in self.shell('git diff HEAD --no-renames --name-only -z')['output'].split('\0') if p]
        untracked = [p for p in self.shell('git ls-files --others --exclude-standard -z')['output'].split('\0') if p]
        artifacts = {n:self.read('/deliverables/'+n) for n in ARTIFACT_FILES}
        protected_files = {item['path']:self.read('/app/'+item['path']) for item in PROTECTED_FILES}
        branch = self.shell('git branch --show-current')['output'].strip()
        snap = {'patch':patch,'patch_sha256':sha(patch),'files':files,'head':head,'dirty':work,'untracked':untracked,
                'branch':branch,'protected_files':protected_files,
                'protected':protected_files.get(PROTECTED) if PROTECTED else None,'artifacts':artifacts}
        snap['working_tree_patch']=self.shell(f'git diff --binary {BASE}')['output']
        snap['untracked_source']={p:self.read('/app/'+p) for p in untracked
                                  if source_scope.source_matches(p,SOURCE_ROOTS)}
        snap['code_sha256']=self.code_hash()
        snap['workspace_identity']=self.workspace_snapshot()
        snap['snapshot_sha256'] = sha(json.dumps(snap,sort_keys=True))
        return snap
    def close(self):
        if hasattr(self,'_harbor_env'):
            from .harbor_runtime import close
            with suspended(): close(self)
            return
        with suspended():
            docker('rm','-f',self.name,check=False,timeout=30)
    def __enter__(self): return self
    def __exit__(self,*args): self.close()

def evaluate(patch, out:Path, *, official=False):
    out.mkdir(parents=True,exist_ok=True)
    started = time.monotonic()
    # Copy pinned inputs into a private read-only mount; neither upstream files
    # nor official commands are edited. Never mount these in the agent box.
    with (tempfile.TemporaryDirectory(prefix='dspilot-verifier-') if official else nullcontext(None)) as temporary:
      verifier_tests = None
      if official:
        verifier_tests = Path(temporary) / 'tests'
        source_manifest = integrity.tree_manifest(TASK/OFFICIAL_EVALUATION['tests_dir'])
        shutil.copytree(TASK/OFFICIAL_EVALUATION['tests_dir'],verifier_tests)
        before = integrity.tree_manifest(verifier_tests)
        if before != source_manifest:
            raise RuntimeError('Verifier copy differs from pinned inputs')
        config = json.loads((verifier_tests/'config.json').read_text())
      with (Box(verifier_tests=verifier_tests) if official else Box()) as box:
        box.shell('mkdir -p /logs/artifacts /logs/verifier')
        box.put('/logs/artifacts/model.patch', patch)
        if official:
            timeout=OFFICIAL_EVALUATION.get('timeout_seconds',600)
            from .runtime_backend import selected_runtime
            if selected_runtime() == 'harbor':
                from .task import BINDING_SPEC
                timeout=BINDING_SPEC.raw.get('staged',{}).get('pier_verifier_timeout_sec',min(timeout,900))
            result = box.shell(OFFICIAL_EVALUATION['command'],timeout=timeout)
            docker('cp',box.name+':/logs/verifier',out/'verifier',check=False)
            def read_report(name):
                path = out/'verifier'/name
                # Do not follow candidate-created report symlinks on the host.
                if (out/'verifier').is_symlink() or path.is_symlink() or not path.is_file():
                    return None
                if path.stat().st_size > 32 * 1024 * 1024:
                    return None
                try: return json.loads(path.read_text())
                except (ValueError,UnicodeError): return None
            reward = read_report('reward.json')
            audited = integrity.audit(patch,SOURCE_ROOTS,config,reward,read_report('ctrf.json'),
                                      result['code'],before,integrity.tree_manifest(verifier_tests))
            (out/'integrity.json').write_text(json.dumps(audited,indent=2))
        else:
            applied = box.shell('git apply --allow-empty /logs/artifacts/model.patch')
            result = box.shell(PUBLIC_VALIDATION['command'],timeout=PUBLIC_VALIDATION.get('timeout_seconds',180)) if applied['code']==0 else applied
            reward = None
        (out/'output.txt').write_text(result['output'])
        passed = (reward or {}).get('reward') == 1 if official else result['code']==0
        record = {'record_id':out.name,'patch_sha256':sha(patch),'code':result['code'],
                  'status':'passed' if passed else 'failed','reward':reward,
                  'seconds':round(time.monotonic()-started,3), 'output_tail':result['output'][-12000:]}
        if official:
            record['integrity'] = audited
        (out/'result.json').write_text(json.dumps(record,indent=2))
        return record
