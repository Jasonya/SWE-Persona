"""Public CLI. Routing uses runtime profiles, never source benchmark names."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pyarrow.parquet as pq

from . import __version__
from .api import load_config, fingerprint
from .dataset import rows, load_task, materialize, write, VENDOR
from .schema import PROFILES
from .time_limit import resolve_limit, run_bounded, positive_seconds

def runtime_hash():
    h=hashlib.sha256()
    for p in sorted(Path(__file__).parent.rglob('*')):
        if p.is_file() and '__pycache__' not in p.parts:
            h.update(str(p.relative_to(Path(__file__).parent)).encode()+b'\0'+p.read_bytes())
    return h.hexdigest()

def worker(root,mode,output,config=None,gate=None,time_limit=None):
    command=[sys.executable,'-B','-m','swe_persona.worker','--root',str(root),'--mode',mode,'--output',str(output)]
    if config:command+=['--config',str(Path(config).resolve())]
    if gate:command+=['--gate-dir',str(gate)]
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1')
    env['PYTHONPATH']=str(Path(__file__).parents[1])+os.pathsep+env.get('PYTHONPATH','')
    Path(output).mkdir(parents=True,exist_ok=True)
    with (Path(output)/(mode+'.log')).open('w') as log:
        if time_limit is None:
            return subprocess.run(command,env=env,stdout=log,stderr=subprocess.STDOUT).returncode
        receipt=run_bounded(command,env=env,log=log,seconds=time_limit['effective_sec'])
        write(Path(output)/'time-limit.json',dict(time_limit,**{'execution':receipt}))
        return receipt['process_exit_code']

def calibrate(dataset,profile,config_path,cache):
    config=load_config(config_path,require_keys=True)
    identity={'profile':profile,'api_config_sha256':fingerprint(config),'runtime_sha256':runtime_hash()}
    key=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()[:20]
    gate=Path(cache)/'calibration'/key;marker=gate/'gate.json'
    if marker.exists() and json.loads(marker.read_text()).get('passed'):return gate
    selected=None
    for row in rows(dataset):
        if row['interaction']['profile_id']!=profile:continue
        if profile=='swe-bench-pro-shell' and row['persona']['persona_id']!='first_time_pr_contributor':continue
        selected=row;break
    if selected is None:raise ValueError('No calibration-compatible row for '+profile)
    gate.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='calibration-input-',dir=Path(cache)) as temp:
        root=materialize(selected,Path(temp)/'task')
        rc=worker(root,'calibrate',gate,config_path)
    if profile=='swe-bench-pro-shell':
        reports=list(gate.glob('rubric_report.*.json'))
        passed=rc==0 and bool(reports) and all(json.loads(p.read_text()).get('gate_passed') for p in reports)
    else:
        p=gate/'calibration.json'
        passed=rc==0 and p.exists() and bool(json.loads(p.read_text()).get('passed'))
    write(marker,dict(identity,passed=passed,worker_exit_code=rc))
    if not passed:raise RuntimeError(f'Judge calibration failed; see {gate / "calibrate.log"}')
    return gate

def normalize_result(row,out,rc):
    profile=row['interaction']['profile_id'];native=None;persona=None;details=[]
    status='infrastructure_error'
    if profile=='swe-bench-pro-shell':
        matches=list(out.glob('*/episode.json'))
        if matches:
            p=matches[0];d=json.loads(p.read_text());details=[str(p.relative_to(out))]
            native=d.get('base_pass');persona=d.get('persona_pass')
            if d.get('episode_valid') is True:status='pass' if d.get('joint_success') else 'fail'
            else:status='invalid'
    else:
        matches=([out/'persona-result.json'] if (out/'persona-result.json').is_file() else list(out.rglob('persona-result.json')))
        if matches:
            p=matches[0];d=json.loads(p.read_text());details=[str(p.relative_to(out))]
            outcome=d.get('benchmark_outcome')
            status=outcome.get('status',outcome.get('outcome','invalid')) if isinstance(outcome,dict) else outcome or 'invalid'
            reward=d.get('original_reward');native=None if reward is None else reward==1
            checker=d.get('persona_check',d.get('checker',{})) or {}
            persona=checker.get('status')
            if persona in {'pass','fail'}:persona=persona=='pass'
            else:
                values=[v.get('status') for v in checker.get('requirements',{}).values()]
                persona=False if 'fail' in values else True if values and all(v=='pass' for v in values) else None
    if profile=='swe-bench-live' and matches:
        checks=d.get('objective_requirements') or []
        values=[x.get('verdict') for x in checks]
        persona=False if 'fail' in values else True if values and all(v=='pass' for v in values) else None
    result={'task_id':row['task_id'],'source_benchmark':row['source_benchmark'],'source_subset':row['source_subset'],
            'content_sha256':row['content_sha256'],'harness_version':__version__,'profile':profile,
            'status':status,'native_pass':native,'persona_pass':persona,'joint_success':status=='pass',
            'worker_exit_code':rc,'details':details,'note':'Invalid and infrastructure errors are not task failures.'}
    write(out/'result.json',result)
    return result

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dataset',type=Path,default=Path('swe-persona-lite.parquet'))
    sub=ap.add_subparsers(dest='command',required=True)
    sub.add_parser('list',help='List tasks and their source benchmarks')
    val=sub.add_parser('validate',help='Validate rows and optionally import every executable profile')
    val.add_argument('--dry-run',action='store_true');val.add_argument('--output',type=Path,default=Path('runs/validation'))
    prep=sub.add_parser('materialize');prep.add_argument('--task',required=True);prep.add_argument('--output',type=Path,required=True)
    cal=sub.add_parser('calibrate');cal.add_argument('--profile',choices=PROFILES,required=True)
    cal.add_argument('--config',type=Path,required=True);cal.add_argument('--cache',type=Path,default=Path.home()/'.cache/swe-persona')
    run=sub.add_parser('run');run.add_argument('--task',required=True);run.add_argument('--config',type=Path,required=True)
    run.add_argument('--output',type=Path,required=True);run.add_argument('--dry-run',action='store_true')
    run.add_argument('--cache',type=Path,default=Path.home()/'.cache/swe-persona')
    run.add_argument('--timeout-sec','--total-timeout-sec',dest='timeout_sec',type=positive_seconds,
        help='Total worker wall-clock seconds for every profile; default: row budget.episode_timeout_sec. Includes setup and grading; existing inner limits remain in force.')
    args=ap.parse_args()
    if args.command=='list':
        table=pq.read_table(args.dataset,columns=['task_id','source_benchmark','source_subset'])
        for row in table.to_pylist():print(json.dumps(row))
        return 0
    if args.command=='materialize':
        materialize(load_task(args.dataset,args.task),args.output);print(args.output.resolve());return 0
    if args.command=='validate':
        args.output.mkdir(parents=True,exist_ok=True);results=[];seen=set();originals=set()
        with tempfile.TemporaryDirectory(prefix='sp-validation-') as temp:
            for row in rows(args.dataset):
                if row['task_id'] in seen:raise ValueError('Duplicate task_id')
                seen.add(row['task_id']);originals.add((row['source_benchmark'],row['source']['instance_id']))
                entry={'task_id':row['task_id'],'source_benchmark':row['source_benchmark'],'profile':row['interaction']['profile_id'],'status':'pass'}
                if args.dry_run:
                    root=materialize(row,Path(temp)/str(len(results)))
                    rc=worker(root,'dry-run',args.output/row['task_id'].replace('/','__'))
                    entry.update(worker_exit_code=rc,status='pass' if rc==0 else 'fail')
                    import shutil
                    shutil.rmtree(root)
                results.append(entry)
        report={'tasks':len(results),'unique_source_instances':len(originals),'passed':sum(r['status']=='pass' for r in results),
                'model_calls':0,'containers_started':0,'scope':'Row integrity and profile dry runs only' if args.dry_run else 'Row integrity only',
                'results':results}
        write(args.output/'report.json',report);print(json.dumps({k:v for k,v in report.items() if k!='results'},indent=2))
        return 0 if report['passed']==report['tasks'] else 1
    args.cache.mkdir(parents=True,exist_ok=True)
    if args.command=='calibrate':
        print(calibrate(args.dataset,args.profile,args.config,args.cache));return 0
    row=load_task(args.dataset,args.task)
    config=load_config(args.config,require_keys=not args.dry_run)
    if args.output.exists() and any(args.output.iterdir()):raise ValueError('Run output must be a new empty directory')
    args.output.mkdir(parents=True,exist_ok=True)
    time_limit=resolve_limit(row,args.timeout_sec)
    profile=row['interaction']['profile_id']
    if profile=='swe-bench-pro-mini' and config['agent']['api']!='chat_completions':
        raise ValueError('This profile uses mini-swe-agent Chat Completions; configure agent.api=chat_completions')
    write(args.output/'run-config.json',{'task_id':row['task_id'],'content_sha256':row['content_sha256'],
          'runtime_sha256':runtime_hash(),'api_config_sha256':fingerprint(config),'models':config,'time_limit':time_limit})
    try:
        gate=None if args.dry_run else calibrate(args.dataset,profile,args.config,args.cache)
    except Exception as exc:
        result=normalize_result(row,args.output,2)
        result.update(status='calibration_error',error=str(exc))
        write(args.output/'result.json',result)
        print(json.dumps(result,indent=2));return 2
    with tempfile.TemporaryDirectory(prefix='input-',dir=args.cache) as temp:
        root=materialize(row,Path(temp)/'task')
        restorations=[json.loads(p.read_text()) for p in root.glob('tasks/*/asset-restoration.json')]
        if restorations:write(args.output/'asset-restoration.json',{'restorations':restorations})
        rc=worker(root,'dry-run' if args.dry_run else 'run',args.output,args.config,gate,
                  None if args.dry_run else time_limit)
    if args.dry_run:
        print(json.dumps({'task_id':row['task_id'],'dry_run':rc==0,'log':str(args.output/'dry-run.log')}));return rc
    result=normalize_result(row,args.output,rc)
    receipt=json.loads((args.output/'time-limit.json').read_text())
    result['time_limit']=receipt
    if receipt['execution']['timed_out']:
        result.update(status='fail',joint_success=False,outcome_reason='total_timeout',
                      verification_complete=False,score=0.0,
                      note='Total wall-clock budget exhausted. Unexecuted grading is not synthesized; partial diagnostics are retained.')
    write(args.output/'result.json',result)
    print(json.dumps(result,indent=2))
    return 0 if result['status'] in {'pass','fail'} else 2

if __name__=='__main__':raise SystemExit(main())
