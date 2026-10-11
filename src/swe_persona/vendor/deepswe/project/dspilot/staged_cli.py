"""Explicit opt-in entrypoint; default legacy launch commands remain unchanged."""
import argparse
from .coding_backend import add_backend_argument, apply_backend_argument
import json
import os
from .specs import ROOT, compile_episode
from .staged import load_plan, template_coverage


def main():
    p=argparse.ArgumentParser(description='Staged persona data validation and gated episodes')
    p.add_argument('command',choices=['list','audit','validate','preflight','calibrate','run'])
    p.add_argument('--task-id');p.add_argument('--persona-id')
    p.add_argument('--plan',help='Frozen staged-plan.json under runs/staged-pipelines with a basic-check receipt')
    p.add_argument('--run-id');p.add_argument('--arm',choices=['baseline','control','pivot'],default='pivot')
    add_backend_argument(p)
    args=p.parse_args()
    apply_backend_argument(args)
    if args.plan:
        if args.command in ('list','audit'):p.error('--plan applies to validate, preflight, calibrate or run')
        os.environ['DS_STAGED_PLAN_PATH']=args.plan
    if args.command=='audit':
        from .staged_audit import audit
        result=audit();print(json.dumps(result,indent=2))
        if not result['specifications_valid']:raise SystemExit(1)
        return
    if args.command=='list':
        rows=[]
        for path in sorted((ROOT/'data/staged_bindings').glob('*.json')):
            d=json.loads(path.read_text());ep=compile_episode(d['task_id'],d['persona_id']);plan=load_plan(ep)
            coverage=template_coverage(plan)
            rows.append({k:plan[k] for k in ('task_id','persona_id','task_disclosure_mode','persona_disclosure_mode','status','blocked_reasons')} |
                        {'implemented_templates':sum(r['implemented'] for r in coverage),
                         'source_templates':len(coverage),'pending_templates':[
                             r['template_id'] for r in coverage if r['status']=='pending_fixture'],
                         'full_persona_coverage':all(r['implemented'] for r in coverage)})
        print(json.dumps({'pairs':len(rows),'implemented_templates':sum(r['implemented_templates'] for r in rows),
                          'source_templates':sum(r['source_templates'] for r in rows),
                          'full_persona_pairs':sum(r['full_persona_coverage'] for r in rows),'bindings':rows},indent=2));return
    if not args.task_id or not args.persona_id:p.error('Specify --task-id and --persona-id')
    ep=compile_episode(args.task_id,args.persona_id);plan=load_plan(ep)
    if args.command=='validate':
        print(json.dumps({'valid_specification':True,'calibrated':False,'plan':plan},indent=2));return
    os.environ['DS_TASK_ID']=args.task_id;os.environ['DS_PERSONA_ID']=args.persona_id;os.environ['DS_PROTOCOL']='staged'
    if args.command=='calibrate':
        from . import calibrate_staged
        calibrate_staged.main();return
    if args.command=='run' and (not args.run_id or not args.run_id.replace('-','').replace('_','').isalnum()):p.error('Specify a fresh alphanumeric --run-id (hyphens/underscores allowed)')
    from .prepare import verify_cache
    from .planning_staged import require_execution_admission
    require_execution_admission(ep)
    verify_cache()
    from .run_staged import episode
    if args.command=='preflight':
        from .run_staged import EPISODE, episode_manifest
        admission=require_execution_admission(EPISODE)
        manifest=episode_manifest(EPISODE)
        print(json.dumps({'passed':True,'task_id':args.task_id,'persona_id':args.persona_id,
            'plan':admission['plan'],'manifest_file_count':len(manifest),
            'checks':['runtime_import','adapted_plan_identity','runtime_manifest'],
            'model_calls':0,'coding_agent_started':False},indent=2));return
    from .runtime_backend import selected_runtime
    if selected_runtime()=='pier_native':
        from .pier_native_runtime import run as pier_run
        result=pier_run(args.run_id,args.arm)
    elif selected_runtime()=='pier':
        from .pier_runtime import run as pier_run
        result=pier_run(args.run_id,args.arm)
    else:
        result=episode(args.run_id,args.arm)
    print(json.dumps(result,indent=2))
    if not result['evaluation_valid']:raise SystemExit('Episode retained with evaluation/interaction error; inspect result.json')


if __name__=='__main__':main()
