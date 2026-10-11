"""Freeze existing configuration -> basic checks -> staged run.

This entry point uses registered task/persona/checker scaffolds. It never publishes
over a registry binding. Calibration is an optional standalone development test.
"""
import argparse
from .coding_backend import add_backend_argument, apply_backend_argument
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from . import specs
from .planning_staged import VERSION, dump, prepare, verify_prepared_plan


def directory_for(pipeline_id):
    if not isinstance(pipeline_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',pipeline_id):
        raise ValueError('pipeline-id must be 1..80 letters/digits/hyphens/underscores')
    root=specs.ROOT/'runs/staged-pipelines'
    path=root/pipeline_id
    if path.resolve()!=specs.ROOT.resolve()/'runs/staged-pipelines'/pipeline_id:
        raise ValueError('Pipeline paths cannot use symlinks')
    return path


def invoke(command, log):
    env=os.environ.copy();env.pop('DS_STAGED_PLAN_PATH',None)
    # Fresh processes select the reviewed plan before importing task-bound modules.
    with Path(log).open('w') as output:
        result=subprocess.run([sys.executable,'-m','dspilot.staged_cli',*command],
            cwd=specs.ROOT,env=env,stdout=output,stderr=subprocess.STDOUT,timeout=10800)
    return result.returncode


def execute(pipeline_id, *, task_id=None, persona_id=None, command='prepare',
            max_revisions=2, arm='pivot', client=None, launcher=invoke, prepared_only=False, source_plan=None,
            delivery_opening=None):
    if command not in ('prepare','freeze','run','status'):raise ValueError('Unknown pipeline command')
    if arm not in ('pivot','control','baseline'):raise ValueError('Unknown arm')
    directory=directory_for(pipeline_id)
    if command=='status':return json.loads((directory/'state.json').read_text())
    directory.parent.mkdir(parents=True,exist_ok=True)
    lock_path=directory.parent/f'.{pipeline_id}.lock'
    with lock_path.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if directory.exists():
            state=json.loads((directory/'state.json').read_text())
            if command in ('prepare','freeze'):raise ValueError('Preparation is immutable; use run/status or a fresh pipeline-id')
            if (task_id and state['task_id']!=task_id) or (persona_id and state['persona_id']!=persona_id):
                raise ValueError('Pipeline identity mismatch')
            task_id=state['task_id'];persona_id=state['persona_id']
        else:
            if prepared_only or command=='run':raise ValueError('Prepared-only execution cannot create a plan; freeze a configuration first')
            if not task_id or not persona_id:raise ValueError('A new pipeline needs task-id and persona-id')
            # Unknown tasks/personas are input errors. An absent checker scaffold
            # is missing design information, not an unsupported applicability verdict.
            specs.load_task(task_id)
            # Construct the task-only input in an isolated process BEFORE loading
            # persona/binding data in this preparation path.
            from .delivery_capture import prepare_blur, verify_opening
            if delivery_opening is not None:
                if command != 'freeze' or arm != 'pivot': raise ValueError('Opening reuse requires a Pivot freeze')
                delivery_opening = Path(delivery_opening)
                verify_opening(delivery_opening, task_id=task_id, persona_id=persona_id, arm='pivot')
                delivery_blur = {'blur': json.loads((delivery_opening/'task-blur.json').read_text()),
                                 'receipt': json.loads((delivery_opening/'blur-receipt.json').read_text())}
            else:
                delivery_blur = prepare_blur(specs.ROOT, task_id) if command == 'freeze' else None
            if persona_id not in specs.persona_catalog():raise ValueError('Unknown persona')
            binding=specs.binding_paths().get((task_id,persona_id))
            seed=specs.ROOT/'data/staged_bindings'/f'{task_id}--{persona_id}.json'
            if binding is None or not seed.is_file():
                state={'version':VERSION,'task_id':task_id,'persona_id':persona_id,
                       'status':'needs_information','reasons':['No registered task/persona checker and staged fixture scaffold. This is not a not_applicable verdict.'],
                       'missing_information':['A source-bound binding with executable checker/probe and evidence contracts is required.']}
                dump(directory/'state.json',state);return state
            ep=specs.compile_episode(task_id,persona_id)
            if command=='freeze':
                from .frozen_staged import freeze_plan, seed_path
                state=freeze_plan(ep,directory,specs.ROOT/source_plan if source_plan else seed_path(ep),
                                  delivery_blur=delivery_blur, delivery_opening=delivery_opening)
            else:
                state=prepare(ep,directory,max_revisions=max_revisions,client=client)
        if command in ('prepare','freeze') or state['status'] != 'prepared':
            # Interrupted or failed attempts retain their evidence. Never silently
            # regenerate a plan or repeat paid execution on resume.
            return state
        ep=specs.compile_episode(task_id,persona_id)
        plan_path=specs.ROOT/state['plan']
        try:
            verify_prepared_plan(plan_path,ep)
            if state.get('arm',arm)!=arm:raise ValueError('Cannot change arm within an execution attempt')
            state['arm']=arm
            common=['--task-id',task_id,'--persona-id',persona_id,'--plan',state['plan']]
            # staged_cli and the runner independently verify reviewed admission.
            run_id=f'pipeline-{pipeline_id}-{arm}'
            state.update(status='running',run_id=run_id);dump(directory/'state.json',state)
            code=launcher(['run',*common,'--arm',arm,'--run-id',run_id],directory/'execution.log')
            result_path=specs.ROOT/'runs'/run_id/'result.json'
            if not result_path.is_file():
                state.update(status='execution_failed',reasons=['Execution did not produce result.json; inspect execution.log.'])
                return state
            result=json.loads(result_path.read_text())
            if result.get('run_id')!=run_id or result.get('task_id')!=task_id or result.get('persona_id')!=persona_id or result.get('arm')!=arm:
                raise ValueError('Execution result identity mismatch')
            summary={key:result.get(key) for key in (
                'coding_backend','base_pass','persona_pass','joint_pass','original_evaluation_valid','persona_evaluation_valid',
                'task_workflow','task_workflow_pass','task_acceptance_scope',
                'admission_policy','calibration_policy','calibration_status','model_calibrated',
                'execution_admission','review_policy','preparation_warnings','persona_evaluation_exclusion','evaluation_valid','end_reason','task_disclosure_mode','persona_disclosure_mode','seconds','timings')}
            summary.update(result=str(result_path.relative_to(specs.ROOT)),
                           full_task_disclosed=(result.get('stage') or {}).get('full_task_disclosed'),
                           preparation_time_included=False)
            dump(directory/'summary.json',summary)
            state.update(status='complete' if code==0 and result.get('evaluation_valid') else 'execution_failed',
                         result=summary['result'],summary=str((directory/'summary.json').relative_to(specs.ROOT)))
            return state
        except Exception as exc:
            # Endpoint credentials are never included in our subprocess command.
            state.update(status='blocked',reasons=[f'{type(exc).__name__}: {exc}'])
            return state
        finally:
            dump(directory/'state.json',state)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['prepare','freeze','run','status'])
    parser.add_argument('--source-plan',help='Existing configuration to freeze; defaults to registered staged binding')
    parser.add_argument('--pipeline-id',required=True)
    parser.add_argument('--task-id');parser.add_argument('--persona-id')
    parser.add_argument('--prepared-only',action='store_true',help='Never generate a missing configuration')
    parser.add_argument('--max-revisions',type=int,default=2,choices=range(4))
    parser.add_argument('--arm',choices=['pivot','control','baseline'],default='pivot')
    add_backend_argument(parser)
    args=parser.parse_args()
    apply_backend_argument(args)
    result=execute(args.pipeline_id,task_id=args.task_id,persona_id=args.persona_id,
                   command=args.command,max_revisions=args.max_revisions,arm=args.arm,prepared_only=args.prepared_only,
                   source_plan=args.source_plan)
    print(json.dumps(result,indent=2,ensure_ascii=False))
    if args.command!='status' and result['status'] not in ('prepared','complete'):
        raise SystemExit(1)


if __name__=='__main__':main()
