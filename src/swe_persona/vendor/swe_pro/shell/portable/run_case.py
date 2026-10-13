#!/usr/bin/env python3
"""Recipient entry point: validate public inputs or run with a chosen adapter."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

sys.dont_write_bytecode=True
os.environ["PYTHONDONTWRITEBYTECODE"]="1"

ROOT=Path(__file__).resolve().parent

def prepare(case_id):
    manifest=json.loads((ROOT/'release-manifest.json').read_text())
    case=next((c for c in manifest['cases'] if c['case_id']==case_id),None)
    if case is None:
        raise ValueError('Unknown case_id')
    if case['runtime_status']!='adapter_draft':
        raise ValueError('Historical generation is unverified: this case has no runnable runtime assignment')
    runtime=ROOT/case.get('runtime_path','runtime')/'runner'
    os.environ['PCB_PERSONA_FILE']=str(ROOT/'personas.json')
    os.environ['PCB_COMPLETE_NATIVE_OPENING']='1'
    sys.path.insert(0,str(runtime))
    import registry
    binding=json.loads((ROOT/case['binding_path']).read_text())
    task=SimpleNamespace(task_id=case['instance_id'],row_index_zero=case['row_index_zero'],public_contract=binding['public_contract'])
    loaded=registry.load_binding(task,case['persona_id'],case['data_version'])
    assert loaded==binding
    # Use the exact production loader and selection path, including applicability.
    expected=[r['requirement_id'] for r in binding['requirements']]
    candidates=registry.build_candidates(task,case['persona_id'],max_evolved=len(expected),data_version=case['data_version'])
    selected=registry.applicable_candidates(candidates,len(expected))
    if (not expected or [c.requirement_id for c in selected]!=expected):
        raise ValueError('Production loader did not load every frozen persona requirement')
    for candidate in candidates:
        candidate.statement()
    import run_episode  # Real controller, judge, checkers and adapter import graph.
    first=json.loads((ROOT/case['opening_path']).read_text())
    assert first['vague_request']
    task.row=binding['public_contract']
    errors=run_episode.safe_progressive_contract_errors(first['vague_request'],task)
    from disclosure import NativeDisclosureTracker,native_requirement_inventory
    tracker=NativeDisclosureTracker(native_requirement_inventory(binding['public_contract']['requirements']))
    tracker.record_agent_visible_message(message_id='u-1',turn=1,text=first['vague_request'],source='frozen_export')
    if errors or tracker.missing_ids():
        raise ValueError('Frozen opening does not preserve the complete public native disclosure')
    return case,runtime,binding,candidates


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--case',required=True)
    ap.add_argument('--dry-run',action='store_true')
    ap.add_argument('--describe',action='store_true')
    ap.add_argument('--adapter',default='portable_shell:ShellAgent')
    ap.add_argument('--agent-model',default='gpt-5.6-luna')
    ap.add_argument('--simulator-model',default='gpt-6-astra')
    ap.add_argument('--judge-model',default='gpt-6-astra')
    ap.add_argument('--model-config',type=Path)
    ap.add_argument('--image-ref')
    ap.add_argument('--output',type=Path)
    ap.add_argument('--run-id',help='Canonical CASE_ID-aN; defaults to CASE_ID-a1')
    args=ap.parse_args()
    os.environ["PCB_AGENT_ADAPTER"]=args.adapter
    case,runtime,binding,candidates=prepare(args.case)
    from agent import resolve_factory
    resolve_factory(args.adapter)
    if args.describe:
        print(json.dumps({'case_id':args.case,'requirements':[{'requirement_id':c.requirement_id,'statement':c.statement(),'schedule':c.schedule,'rubric':c.template['satisfaction'],'checker':c.checker} for c in candidates]},indent=2))
        return 0
    if args.dry_run:
        print(json.dumps({'case_id':args.case,'dry_run':'pass','requirements_expected':len(binding['requirements']),
            'requirements_loaded':len(candidates),'loaded_requirement_ids':[c.requirement_id for c in candidates],
            'requirement_loader':'registry.build_candidates + applicable_candidates',
            'native_contract_sha256':binding['public_contract_sha256'],'runtime_path':case.get('runtime_path','runtime'),'imports':'real controller, simulator, judge, checkers and adapter dispatcher','official_evaluator':'scaleapi/SWE-bench_Pro-os@ca10a60a5fcae51e6948ffe1485d4153d421e6c5','model_calls':0,'container_runs':0,'limit':'No official test or coding-agent episode is executed'},indent=2))
        return 0
    if not args.output:
        ap.error('--output is required for execution and must be outside the frozen public root')
    output=args.output.resolve()
    if output==ROOT or ROOT in output.parents:
        ap.error('Execution outputs must be outside the frozen public root')
    run_id=args.run_id or case['case_id']+'-a1'
    import re
    if not re.fullmatch(re.escape(case['case_id'])+r'-a[1-9][0-9]*',run_id):
        ap.error('--run-id must match CASE_ID-aN; use a new attempt number without overwriting prior results')
    os.environ['PCB_AGENT_ADAPTER']=args.adapter
    if args.model_config:
        os.environ['PCB_MODEL_CONFIG']=str(args.model_config.resolve())
    command=[sys.executable,str(runtime/'run_episode.py'),'--instance-id',case['instance_id'],'--persona',case['persona_id'],'--data-version',case['data_version'],'--max-turns','6','--max-steps','12','--max-evolved','2','--agent-model',args.agent_model,'--simulator-model',args.simulator_model,'--rubric-model',args.judge_model,'--judge-model',args.judge_model,'--adapter-model',args.agent_model,'--rubric-feedback','online','--runs-root',str(output),'--run-id',run_id,'--first-message-file',str(ROOT/case['opening_path'])]
    if args.image_ref:
        command+=['--image-ref',args.image_ref]
    return subprocess.call(command,env=dict(os.environ))

if __name__=='__main__':
    raise SystemExit(main())
