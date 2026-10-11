#!/usr/bin/env python3
"""Sequential evaluation with optional source/profile selection and explicit results."""
import argparse
import math
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys
import pyarrow.parquet as pq


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset',type=Path,default=Path('swe-persona-lite.parquet'))
    p.add_argument('--config',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--source-benchmark')
    p.add_argument('--profile')
    p.add_argument('--limit',type=int)
    p.add_argument('--cpu-only',action='store_true',help='Select rows requiring no GPUs')
    p.add_argument('--dry-run',action='store_true')
    p.add_argument('--cache',type=Path,help='Directory for materialization and cached calibration')
    p.add_argument('--timeout-sec','--total-timeout-sec',dest='timeout_sec',type=float,help='Override the total wall-clock limit for each selected task')
    args=p.parse_args()
    if args.timeout_sec is not None and (not math.isfinite(args.timeout_sec) or args.timeout_sec<=0):
        p.error('--timeout-sec must be finite and positive')
    if args.limit is not None and args.limit<1:p.error('--limit must be positive')
    if args.output.exists() and any(args.output.iterdir()):p.error('--output must be new or empty')
    metadata=pq.read_table(args.dataset,columns=['task_id','source_benchmark','interaction','environment']).to_pylist()
    selected=[r for r in metadata if (not args.source_benchmark or r['source_benchmark']==args.source_benchmark)
              and (not args.profile or r['interaction']['profile_id']==args.profile)
              and (not args.cpu_only or not r['environment']['agent']['resources']['gpus'])]
    if args.limit:selected=selected[:args.limit]
    if not selected:p.error('No matching tasks')
    args.output.mkdir(parents=True,exist_ok=True)
    results=[]
    for index,row in enumerate(selected):
        out=args.output/f'{index:04d}'
        command=[sys.executable,'-m','swe_persona.cli','--dataset',str(args.dataset.resolve()),'run',
                 '--task',row['task_id'],'--config',str(args.config.resolve()),'--output',str(out.resolve())]
        if args.dry_run:command.append('--dry-run')
        if args.cache:command+=['--cache',str(args.cache.resolve())]
        if args.timeout_sec is not None:command+=['--timeout-sec',str(args.timeout_sec)]
        rc=subprocess.run(command).returncode
        result=json.loads((out/'result.json').read_text()) if (out/'result.json').exists() else {
            'task_id':row['task_id'],'status':'dry_run_pass' if args.dry_run and rc==0 else 'infrastructure_error',
            'worker_exit_code':rc}
        result.update(source_benchmark=row['source_benchmark'],profile=row['interaction']['profile_id'],
                      run_directory=out.name)
        results.append(result)
        summary={'selected':len(selected),'attempted':len(results),'status_counts':dict(Counter(r['status'] for r in results)),
                 'selection':[{'task_id':r['task_id'],'source_benchmark':r['source_benchmark'],
                               'profile':r['interaction']['profile_id']} for r in selected],
                 'results':results,'note':'Errors and invalid episodes must be reported separately from pass/fail.'}
        (args.output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
        if result['status']=='calibration_error':break
    return 0 if len(results)==len(selected) and all(r['status'] in {'pass','fail','dry_run_pass'} for r in results) else 2

if __name__=='__main__':raise SystemExit(main())
