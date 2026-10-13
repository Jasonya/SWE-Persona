#!/usr/bin/env python3
"""Repeat unchanged SWE-Live reference checks in fresh environments, with no API calls."""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset',type=Path,default=Path('swe-persona-lite.parquet'))
    parser.add_argument('--task',help='One SWE-Live task ID; omit to check every SWE-Live pair')
    parser.add_argument('--repetitions',type=int,default=3)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--_worker',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.repetitions<2:parser.error('--repetitions must be at least 2 for a stability check')
    from swe_persona.dataset import rows, load_task, materialize, write
    from swe_persona.cli import runtime_hash
    from swe_persona.native_preflight import native_preflight_report
    selected=[load_task(args.dataset,args.task)] if args.task else [
        row for row in rows(args.dataset) if row['interaction']['profile_id']=='swe-bench-live']
    if not selected or any(row['interaction']['profile_id']!='swe-bench-live' for row in selected):
        parser.error('Only SWE-Live tasks are supported')
    if any(row['environment']['agent']['resources'].get('gpus',0) for row in selected):
        parser.error('This check supports CPU tasks only')
    if not args._worker:
        if args.output.exists() and any(args.output.iterdir()):parser.error('Use a new empty output directory')
        args.output.mkdir(parents=True,exist_ok=True)
        results=[]
        for index,row in enumerate(selected):
            directory=args.output/f'{index:04d}'
            command=[sys.executable,str(Path(__file__).resolve()),'--_worker','--dataset',str(args.dataset.resolve()),
                '--task',row['task_id'],'--repetitions',str(args.repetitions),'--output',str(directory.resolve())]
            directory.mkdir()
            with (directory/'check.log').open('w') as log:
                rc=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT).returncode
            report=directory/'report.json'
            value=json.loads(report.read_text()) if report.is_file() else {
                'task_id':row['task_id'],'passed':False,'error':'Check worker did not produce a report; see check.log'}
            value['worker_exit_code']=rc
            results.append(value)
            write(args.output/'report.json',{'runtime_sha256':runtime_hash(),'model_calls':0,
                'tasks':len(selected),'completed':len(results),'passed':len(results)==len(selected) and all(r['passed'] for r in results),
                'results':results})
            print(json.dumps({'task_id':row['task_id'],'passed':value['passed'],'worker_exit_code':rc}),flush=True)
        return 0 if all(r['passed'] for r in results) else 2
    args.output.mkdir(parents=True,exist_ok=True)
    row=selected[0];root=materialize(row,args.output/'input')
    from swe_persona.swe_live import install_live
    install_live(root,None,args.output)
    import docker
    from support import Manifest
    from run_fullset import get_image
    from run_study import ensure_gold
    manifest=Manifest(root/'tasks'/row['source']['task_id']/'tests/native-task.jsonl')
    source={'key':row['source']['task_id'],'path':str(manifest.task_path),
            'sha256':manifest.task_sha256,'instance_id':manifest.task_id}
    client=docker.from_env(timeout=30)
    try:
        image,_=get_image(client,manifest)
        checks=[]
        for index in range(args.repetitions):
            attempt=args.output/f'repetition-{index+1:03d}'
            ensure_gold(attempt,source,image.id,{'gold_seconds':row['budget']['verifier_timeout_sec']},False)
            checks.append(native_preflight_report(attempt/'gold'/source['key']))
            write(args.output/'report.json',{'task_id':row['task_id'],'content_sha256':row['content_sha256'],
                'image_id':image.id,'runtime_sha256':runtime_hash(),'model_calls':0,
                'expected_repetitions':args.repetitions,'completed_repetitions':len(checks),
                'passed':len(checks)==args.repetitions and all(c['passed'] for c in checks),'checks':checks})
    finally:
        client.close()
    return 0 if all(c['passed'] for c in checks) else 2


if __name__=='__main__':raise SystemExit(main())
