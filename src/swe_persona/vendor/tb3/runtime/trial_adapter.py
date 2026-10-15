"""Native TB3 grading with all-service cutoff capture and independent persona checks."""
from pathlib import Path
from contextlib import asynccontextmanager
import asyncio, copy, fnmatch, hashlib, json, os, shutil, subprocess, types, uuid
from protocol import ROOT, read, write
from harbor.models.task.artifacts import with_convention_entry, effective_artifact_service
from harbor.models.task.verifier_mode import resolve_effective_verifier_env_config
from harbor.models.trial.config import TrialConfig
from harbor.trial.trial import Trial
from harbor.trial.artifact_handler import artifact_host_path
from terminal_agent import docker, PersonaTerminalAgent
from checker_runtime import ensure_checker_python, materialize_collected_patch, restore_checker_permissions

def tree_index(root):
    result={}
    for p in sorted(root.rglob('*')):
        if p.is_symlink():result[p.relative_to(root).as_posix()]={'symlink':os.readlink(p)}
        elif p.is_file():
            h=hashlib.sha256()
            with p.open('rb') as f:
                for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
            result[p.relative_to(root).as_posix()]={'sha256':h.hexdigest(),'bytes':p.stat().st_size}
    return result

async def collect_frozen(self,*,artifacts_dir,step_cfg=None,step_artifacts=None,stop_main_before_sidecars=False):
    agent=self.agent
    if not isinstance(agent,PersonaTerminalAgent):
        return await self._native_collect(artifacts_dir=artifacts_dir,step_cfg=step_cfg,step_artifacts=step_artifacts,stop_main_before_sidecars=stop_main_before_sidecars)
    hooks=self._collect_hooks_for(step_cfg)
    if hooks and agent.terminal=='budget_exhausted':
        # The deadline already fixes the benchmark score at zero. Execute no
        # further task code; native grading of frozen artifacts is diagnostic.
        agent.event('collect_hooks_skipped',{'reason':'wall_timeout','count':len(hooks)})
    elif hooks:
        await agent.resume_containers()
        await self._run_collect_hooks(hooks)
        await agent.pause()
    artifacts_dir.mkdir(parents=True,exist_ok=True)
    manifest=[];claims=[]
    entries=with_convention_entry(self._artifact_handler.artifacts,convention_source='/logs/artifacts')
    for entry in entries:
        source=entry.source.rstrip('/');target=artifact_host_path(artifacts_dir,entry);service=effective_artifact_service(entry)
        if source=='/logs/artifacts':
            # Harbor mounts this exact host directory; it is frozen already.
            manifest.append({'source':source,'destination':'artifacts/'+target.relative_to(artifacts_dir).as_posix(),'service':entry.service,'exclude':list(entry.exclude),'type':'directory','status':'ok' if target.exists() else 'empty'})
            claims.append(target);continue
        if any(target==p or p in target.parents or target in p.parents for p in claims):raise RuntimeError('Overlapping artifact destinations')
        claims.append(target);target.parent.mkdir(parents=True,exist_ok=True)
        row={'source':entry.source,'destination':'artifacts/'+target.relative_to(artifacts_dir).as_posix(),'service':entry.service,'exclude':list(entry.exclude),'type':'file','status':'failed'}
        try:
            cid=agent.ids[service]
            await docker('cp',cid+':'+source,str(target))
            if target.is_dir() and not target.is_symlink():
                row['type']='directory'
                for p in sorted(target.rglob('*'),reverse=True):
                    rel=p.relative_to(target).as_posix()
                    if any(fnmatch.fnmatch(rel,pat) or fnmatch.fnmatch(p.name,pat) or fnmatch.fnmatch('./'+rel,pat) for pat in entry.exclude):
                        if p.is_dir() and not p.is_symlink():shutil.rmtree(p)
                        else:p.unlink(missing_ok=True)
            row['status']='ok'
        except Exception as exc:row['error']=str(exc)[-800:]
        manifest.append(row)
    (artifacts_dir/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    index=await asyncio.to_thread(tree_index,artifacts_dir)
    (self.paths.agent_dir/'cutoff-artifacts.json').write_text(json.dumps(index,indent=2)+'\n')
    # Kill while paused. No student/background process resumes during teardown.
    for cid in agent.ids.values():
        try:await docker('kill',cid)
        except RuntimeError:pass
    agent.save()

async def make_trial(config):
    trial=await Trial.create(TrialConfig.model_validate(config))
    trial._native_collect=trial._collect_artifacts_phased
    trial._collect_artifacts_phased=types.MethodType(collect_frozen,trial)
    return trial

async def check_artifacts(task_id, artifacts_dir, binding, checker_path, output=None, gpu_device=None, runtime_image=None, gpu_requests=None):
    """Grade frozen exports in the disclosed main runtime, not verifier ABI."""
    task=next(t for t in read(ROOT/'inventory.json')['cases'] if t['task_id']==task_id)
    image={'tag':runtime_image, 'image':{'id':runtime_image}}
    cid=None;result={'status':'unknown','checks':{},'reason':'Not executed','runtime_image':image['image']['id']}
    try:
        if not runtime_image: raise RuntimeError('Main task image was not captured')
        actual=(await docker('image','inspect','--format','{{.Id}}',image['tag'])).strip()
        if actual!=image['image']['id']:raise RuntimeError('Changed task runtime image')
        checker_image = await ensure_checker_python(image['tag'])
        result['checker_image'] = checker_image
        result['checker_python_layer'] = checker_image != actual
        gpu_args=['--gpus','device='+str(gpu_device)] if gpu_device is not None else []
        if gpu_requests and not gpu_args:
            if len(gpu_requests)!=1: raise RuntimeError('Unsupported multiple GPU requests')
            request=gpu_requests[0]
            ids=request.get('DeviceIDs') or []
            selection='"device='+','.join(ids)+'"' if ids else str(request.get('Count',task['gpus']))
            if selection=='-1': selection='all'
            gpu_args=['--gpus',selection]
        if task['gpus'] and not gpu_args: raise RuntimeError('Original GPU allocation unavailable')
        cid=(await docker('run','-d','--name','pcb-tb3-reactive-check-'+uuid.uuid4().hex[:12],
            '--network','none','--user','0:0','--cpus',str(task['cpus'] or 1),
            '--memory',str(task['memory_mb'] or 4096)+'m','--pids-limit','512',
            '--env','OMP_NUM_THREADS='+str(task['cpus'] or 1),
            '--env','OPENBLAS_NUM_THREADS='+str(task['cpus'] or 1),
            '--env','MKL_NUM_THREADS='+str(task['cpus'] or 1),*gpu_args,
            '--entrypoint','/bin/sh',checker_image,'-c','sleep 300')).strip()
        root=Path(artifacts_dir)
        result['native_patch_materialized'] = (await materialize_collected_patch(task_id, root, cid)
            if '/tmp/agent.patch' in binding.get('original_artifacts', []) else False)
        for entry in sorted(root.iterdir()):
            if entry.name=='manifest.json' or entry.name in {'checkpoint.json','logs'}:continue
            if entry.is_dir():
                await docker('exec',cid,'mkdir','-p','/'+entry.name)
                await docker('cp',str(entry)+'/.',cid+':/'+entry.name+'/')
        restorations = await restore_checker_permissions(task_id, cid)
        if restorations:
            result['checker_permission_restorations'] = restorations
        await docker('cp',str(checker_path),cid+':/tmp/persona_checker.py')
        version=await docker('exec',cid,'python3','--version');result['python_version']=version.strip()
        command="import importlib.util,json; s=importlib.util.spec_from_file_location('c','/tmp/persona_checker.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print('PERSONA_CHECK_RESULT='+json.dumps(m.check()))"
        run=await asyncio.wait_for(docker('exec',cid,'python3','-c',command),250)
        lines=[line for line in run.splitlines() if line.startswith('PERSONA_CHECK_RESULT=')]
        if len(lines)!=1:raise RuntimeError('Checker must emit exactly one structured result')
        checks=json.loads(lines[0].split('=',1)[1]);expected={q['id'] for q in binding['proposal']['requirements']}
        if set(checks)!=expected or any(v['status'] not in {'pass','fail','unknown'} for v in checks.values()):raise ValueError('Invalid checker results')
        result['checks']=checks;result['stdout']=run
        result['status']='fail' if any(v['status']=='fail' for v in checks.values()) else 'unknown' if any(v['status']=='unknown' for v in checks.values()) else 'pass'
        result.pop('reason',None)
    except Exception as exc:result.update(status='unknown',reason=str(exc)[-1500:],error_type=type(exc).__name__)
    finally:
        if cid:
            try:await docker('rm','-f',cid)
            except RuntimeError:pass
    if output:write(output,result)
    return result

async def check_persona(trial,binding,checker_path):
    return await check_artifacts(binding['task_id'],trial.paths.artifacts_dir,binding,checker_path,
                                 trial.paths.trial_dir/'persona-check.json',gpu_device=trial.agent.gpu_device, runtime_image=getattr(trial.agent,'runtime_image',None),
                                 gpu_requests=getattr(trial.agent,'checker_gpu_requests',[]))

async def grade_checkpoints(trial):
    """Post-episode native grades; no score or hidden feedback reaches either model."""
    from harbor.models.trial.paths import TrialPaths
    rows=[]
    for checkpoint in trial.agent.checkpoints:
        if checkpoint['label']!='completion-attempt':continue
        row=dict(checkpoint);root=Path(row['path']);clone=copy.copy(trial)
        clone.paths=TrialPaths(trial_dir=root/'native-evaluation');clone.paths.mkdir()
        if trial._collect_hooks_for(None) or any(e['status']!='copied' for e in row['manifest']):
            row['native_grade']='unknown';row['reason']='Checkpoint cannot reproduce original collection hooks or missing exports'
        else:
            try:
                result=await clone._run_separate_verifier(key='checkpoint-'+str(len(rows)),
                    timeout_sec=trial.task.config.verifier.timeout_sec,artifacts_dir=root,
                    artifacts=trial.task.config.artifacts,user=None)
                row['original_reward']=(result.rewards or {}).get('reward');row['native_grade']='recorded'
            except Exception as exc:row.update(native_grade='unknown',reason=str(exc)[-1000:])
        write(root/'checkpoint-native-result.json',row);rows.append(row)
    write(trial.paths.trial_dir/'checkpoint-results.json',rows)
    return rows
