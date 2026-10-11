"""One randomized persona per task; one distinct fallback after a non-pass.

No global deadline. Whole-sample caps continue the censored online P20 policy.
Historical task coverage is imported separately and never relabelled as v1 passes.
"""
import bootstrap_adapter
from bootstrap_adapter import ROOT, HERE, REFERENCE, SCRIPTS
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import random
import secrets
import shutil
import subprocess
import sys
import time
from support import read,file_hash,source_hashes,collection_source_hashes,Manifest
from run_fullset import atomic_save, get_image, release_image
from collection.run_roleplay import gold_directory
from collection.online import budget_after
from whole_sample import supervise_sample
from recover_sample_cleanup import cleanup_verified,recover_receipt
from build_task import build
from protocol import PROTOCOL

DEFAULT=ROOT/'.local/multilang-roleplay/task-rotation-tb3-v1'
HISTORY=ROOT/'.local/multilang-roleplay/fullset-once-v3'


def sources():
    result=source_hashes(); result.update(collection_source_hashes())
    result.update({p.relative_to(ROOT).as_posix():file_hash(p) for p in HERE.glob('*.py') if not p.name.startswith('test_')})
    result['scripts/swe_live_multilang/recover_sample_cleanup.py']=file_hash(SCRIPTS/'recover_sample_cleanup.py')
    return result


def draw(personas,seed,key):
    rng=random.Random(hashlib.sha256((seed+':'+key).encode()).digest())
    return rng.sample(sorted(personas),2)


def passed(receipt):
    return receipt.get('status')=='completed' and receipt.get('joint_success')==1


def next_slot(receipts):
    if any(passed(r) for r in receipts) or len(receipts)>=2: return None
    return len(receipts)


def prepare(study,history):
    study.mkdir(parents=True,exist_ok=True); path=study/'run-plan.json'
    if path.exists():
        plan=read(path)
        if plan['sources']!=sources(): raise ValueError('Frozen runtime changed; use a new study directory')
        for t in plan['tasks']:
            if file_hash(Path(t['path']))!=t['sha256']: raise ValueError('Frozen task changed')
        return plan
    old=read(history/'manifest.json'); rows=read(history/'results.json'); state=read(history/'summary.json')
    seed=secrets.token_hex(16); tasks=[]
    covered={r['pair_id'].split('/')[0] for r in rows}
    for t in old['tasks']:
        source=history/t['path']
        if file_hash(source)!=t['sha256']: raise ValueError('Historical task changed')
        tasks.append({**t,'path':str(source.resolve()),'personas':draw(old['personas'],seed,t['key']),
                      'historical_coverage':t['key'] in covered})
    random.Random(seed).shuffle(tasks)
    plan={'protocol':PROTOCOL,'collection_policy':'one-random-persona-plus-one-distinct-fallback',
        'seed':seed,'tasks':tasks,'personas':old['personas'],'source_history':str(history.resolve()),
        'history_results_sha256':file_hash(history/'results.json'),'sources':sources(),
        'dataset_revision':old['dataset_revision'],'model':old['config']['model'],
        'initial_seconds':state['next_cap_seconds'],'maximum_seconds':3600,'update_every':20,
        'global_deadline':None,'historical_tasks':len(covered),'total_tasks':len(tasks),
        'success':'New attempts require original pass, independent R1 checker pass and final online MET/user satisfaction',
        'timing':'Per-attempt whole-sample online P20; no exact globally-fastest-fifth claim'}
    atomic_save(study/'historical-results.json',rows); atomic_save(path,plan)
    return plan


def clean(owner):
    import docker
    client=docker.from_env(timeout=10)
    try: return cleanup_verified(client,owner)['removed_containers']
    finally: client.close()


def existing(directory):
    if not directory.exists(): return None
    path=directory/'result.json'
    if path.exists():
        r=read(path)
        if str(r.get('error_type','')).startswith('process_cleanup:'): raise RuntimeError('Unresolved process cleanup')
        if r.get('cleanup_error'):
            import docker
            client=docker.from_env(timeout=10)
            try: recover_receipt(path,client)
            finally: client.close()
            r=read(path)
        return r
    start=read(directory/'started.json'); clean(start['owner'])
    r={'status':'interrupted','base_task_success':None,'joint_success':None,'seconds':None,
       'cap_seconds':start['cap_seconds'],'reason':'Interrupted attempt retained without retry'}
    atomic_save(path,r); return r


def trial_projection(directory,receipt):
    """Expose the agreed trial layout while retaining the supervisor's worker tree."""
    worker=directory/'worker'
    for name in ('persona-result.json','persona-check.json','agent-private/episode.json'):
        source=worker/name;target=directory/name
        if source.exists() and (not target.exists() or source.stat().st_mtime_ns>target.stat().st_mtime_ns):
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
    outcome = ('fail' if receipt['status']=='timeout' else 'invalid'
               if receipt['status'] in ('not_applicable','interrupted') else 'infrastructure_error')
    if not (directory/'persona-result.json').exists():
        atomic_save(directory/'persona-result.json',{'protocol':PROTOCOL,'original_reward':receipt.get('base_task_success'),
            'benchmark_outcome':outcome,
            'termination':receipt['status'],'online_requirements':None,'objective_requirements':None,
            'reason':'Worker did not finish; no unexecuted stages are synthesized'})
    elif receipt['status']!='completed':
        target=directory/'persona-result.json'; value=read(target)
        if value.get('supervisor_status')!=receipt['status']:
            if receipt['status']=='evaluation_invalid' and value.get('benchmark_outcome')=='invalid':
                outcome='invalid'
            value.update(supervisor_status=receipt['status'],diagnostic_original_reward=value.get('original_reward'),
                original_reward=None,benchmark_outcome=outcome,
                termination=receipt['status'],note='Supervisor deadline/validity takes precedence over late worker output')
            atomic_save(target,value)
    for name in ('transcript.json','transcript.md'):
        source=worker/name; target=directory/'agent'/name
        if source.exists() and (not target.exists() or source.stat().st_mtime_ns>target.stat().st_mtime_ns):
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)


def report(study,plan):
    tasks=[]; observations=[]; attempts=[]
    for t in plan['tasks']:
        if t['historical_coverage']:
            tasks.append({'task_key':t['key'],'instance_id':t['instance_id'],'status':'historical_covered',
                'benchmark_outcome':None,'attempts':[]}); continue
        receipts=[]
        for i,persona in enumerate(t['personas']):
            directory=study/'trials'/t['key']/f'{i+1:02d}-{persona}'
            r=existing(directory)
            if r is None: break
            trial_projection(directory,r)
            receipts.append(r); observations.append(r)
            attempts.append({'task_key':t['key'],'instance_id':t['instance_id'],'persona':persona,
                'attempt':i+1,'result':str(directory/'result.json'),**r})
        done=next_slot(receipts) is None
        tasks.append({'task_key':t['key'],'instance_id':t['instance_id'],
            'status':'pass' if any(passed(r) for r in receipts) else 'exhausted' if done else 'pending',
            'benchmark_outcome':'pass' if any(passed(r) for r in receipts) else None,
            'attempts':[{'persona':p,'status':r['status'],'joint_success':r.get('joint_success')} for p,r in zip(t['personas'],receipts)]})
    state=budget_after(observations,plan['initial_seconds'],plan['maximum_seconds'],plan['update_every'])
    complete=sum(t['status']!='pending' for t in tasks)
    summary={'protocol':PROTOCOL,'total_tasks':len(tasks),'processed_tasks':complete,'remaining_tasks':len(tasks)-complete,
        'historical_tasks':plan['historical_tasks'],'new_attempts':len(attempts),'new_benchmark_passes':sum(t['status']=='pass' for t in tasks),
        'counts':dict(Counter(t['status'] for t in tasks)),'next_cap_seconds':state['next_cap_seconds'],
        'global_deadline':None,'publication_ready':False}
    atomic_save(study/'results.json',attempts);atomic_save(study/'task-results.json',tasks)
    atomic_save(study/'status.json',summary);atomic_save(study/'online-budget.json',state)
    return summary,tasks,state


def setup_main(a):
    """Bounded preparation child; owner-scoped supervisor also covers gold containers."""
    study=a.study_dir.resolve(); plan=read(study/'run-plan.json'); task=next(t for t in plan['tasks'] if t['key']==a.setup_task)
    output=a.output_dir;output.mkdir(parents=True,exist_ok=False)
    import docker
    client=docker.from_env(timeout=min(30,a.sample_seconds)); start=time.monotonic()
    try:
        image,pulled=get_image(client,Manifest(Path(task['path'])))
        remaining=max(.1,a.sample_seconds-(time.monotonic()-start))
        gold=gold_directory(study,task,image.id,remaining)
        atomic_save(output/'setup.json',{'image_id':image.id,'gold_dir':str(gold),'pulled':pulled})
        atomic_save(output/'release_check.json',{'ready_for_review':True})
        return 0
    finally: client.close()


def run(study,plan):
    from calibrate_judge import calibrate
    calibrate(study/'calibration',plan['model'])
    for task in plan['tasks']:
        summary,rows,state=report(study,plan)
        record=next(r for r in rows if r['task_key']==task['key'])
        if record['status']!='pending': continue
        receipts=[]
        for i,persona in enumerate(task['personas']):
            directory=study/'trials'/task['key']/f'{i+1:02d}-{persona}'
            r=existing(directory)
            if r is not None:
                receipts.append(r)
                if passed(r): break
                continue
            cap=state['next_cap_seconds']; budget=study/'budgets'/task['key']/f'{i+1:02d}.json'
            if not budget.exists(): atomic_save(budget,{'cap_seconds':cap,'persona':persona})
            cap=read(budget)['cap_seconds']
            print(f'[{summary["processed_tasks"]+1}/{plan["total_tasks"]}] {task["key"]}/{persona}; attempt {i+1}/2; whole sample cap={cap:.2f}s',flush=True)
            preparation=study/'preflight'/task['key']
            pre=existing(preparation)
            if pre is None:
                pre=supervise_sample([sys.executable,'-u',str(Path(__file__)),'--study-dir',str(study),
                    '--setup-task',task['key']],preparation,max(60,cap),cleanup=clean)
            if pre['status']!='completed' or not (preparation/'worker/setup.json').exists():
                directory.mkdir(parents=True,exist_ok=False)
                r={'status':'infrastructure_invalid','base_task_success':None,'joint_success':None,
                    'seconds':None,'cap_seconds':cap,'reason':'Shared image/gold preflight failed',
                    'agent_started':False,'setup_receipt':str(preparation/'result.json')}
                atomic_save(directory/'result.json',r)
            else:
                setup=read(preparation/'worker/setup.json')
                binding=build(Path(task['path']),persona,study/'release-candidates/tasks'/(task['key']+'--'+persona),cap)
                command=[sys.executable,'-u',str(HERE/'run.py'),'--task',task['path'],'--persona',persona,
                    '--gold-dir',setup['gold_dir'],'--binding-dir',str(binding),'--model',plan['model']]
                r=supervise_sample(command,directory,cap,cleanup=clean)
            receipts.append(r)
            summary,rows,state=report(study,plan)
            print('  '+r['status']+'; benchmark_pass='+str(passed(r))+'; no retry of this persona',flush=True)
            if passed(r): break
        # Only an image acquired for this task may be released; never prune shared cache.
        prepared=study/'preflight'/task['key']/'worker/setup.json'
        if prepared.exists() and read(prepared).get('pulled'):
            import docker
            client=docker.from_env(timeout=10)
            try:
                data=read(prepared)
                try: release_image(client,client.images.get(data['image_id']),True,False)
                except docker.errors.ImageNotFound: pass
            finally: client.close()
    return report(study,plan)[0]


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',action='store_true')
    p.add_argument('--study-dir',type=Path,default=DEFAULT);p.add_argument('--history',type=Path,default=HISTORY)
    p.add_argument('--setup-task');p.add_argument('--sample-seconds',type=float);p.add_argument('--deadline-monotonic',type=float)
    p.add_argument('--output-dir',type=Path)
    a=p.parse_args(argv)
    if a.setup_task: return setup_main(a)
    study=a.study_dir.resolve();study.mkdir(parents=True,exist_ok=True)
    with (study/'collection.lock').open('a+b') as lock:
        lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        plan=prepare(study,a.history.resolve())
        print(json.dumps(report(study,plan)[0],indent=2),flush=True)
        if a.run:
            if os.name=='nt':
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
            try: run(study,plan)
            finally:
                if os.name=='nt': ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
    return 0


if __name__=='__main__':raise SystemExit(main())
