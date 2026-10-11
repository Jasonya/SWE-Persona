"""Select a registered task/persona pair before importing the runtime.

Examples:
  python3 -m dspilot.launch list
  python3 -m dspilot.launch validate --task-id TASK --persona-id PERSONA
  python3 -m dspilot.launch run --task-id TASK --persona-id PERSONA --arm pivot --run-id ID
  python3 -m dspilot.launch calibrate --task-id TASK --persona-id PERSONA
  python3 -m dspilot.launch suite --task-id TASK --persona-id PERSONA --prefix ID --repeats 1
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .specs import catalog_summary, compile_episode


def _selection(parser):
    parser.add_argument('--task-id',required=True)
    parser.add_argument('--persona-id',required=True)


def _activate(task_id,persona_id):
    # Validate before importing modules that expose selection-derived constants.
    episode=compile_episode(task_id,persona_id)
    os.environ['DS_TASK_ID']=task_id
    os.environ['DS_PERSONA_ID']=persona_id
    return episode


def main():
    parser=argparse.ArgumentParser(description='Configurable DeepSWE persona benchmark launcher')
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('list',help='List registered tasks, personas, and runnable bindings.')
    validate=commands.add_parser('validate',help='Validate one task/persona binding without Docker or model calls.')
    _selection(validate)
    run=commands.add_parser('run',help='Run one registered episode.')
    _selection(run);run.add_argument('--arm',required=True,choices=['baseline','control','pivot']);run.add_argument('--run-id',required=True)
    calibrate=commands.add_parser('calibrate',help='Calibrate the selected registered binding.')
    _selection(calibrate)
    suite=commands.add_parser('suite',help='Run fresh baseline/control/pivot episodes for one binding.')
    _selection(suite);suite.add_argument('--prefix',required=True);suite.add_argument('--repeats',type=int,default=1)
    matrix=commands.add_parser('matrix',help='Plan or execute all matching registered bindings.')
    matrix.add_argument('--prefix',required=True);matrix.add_argument('--repeats',type=int,default=1)
    matrix.add_argument('--task-id',action='append',dest='task_ids');matrix.add_argument('--persona-id',action='append',dest='persona_ids')
    matrix.add_argument('--execute',action='store_true');matrix.add_argument('--calibrate',action='store_true')
    matrix.add_argument('--resume',action='store_true')
    report=commands.add_parser('report',help='Generate versioned matrix reports from saved results; no Docker or API calls.')
    report.add_argument('--prefix',required=True)
    from . import pairing
    pairing.configure_parser(commands.add_parser('pair', help='Propose, check, review and publish candidate bindings.'))
    args=parser.parse_args()
    if args.command=='list':
        print(json.dumps(catalog_summary(),indent=2,ensure_ascii=False));return
    if args.command=='pair':
        pairing.dispatch(args);return
    if args.command=='report':
        from .matrix_report import ROOT, write_report
        folder=write_report(ROOT,args.prefix)
        print(json.dumps({'report':str(folder/'report.md'),'summary':str(folder/'summary.json'),
                          'pairs':str(folder/'pairs.csv')}));return
    if args.command=='matrix':
        from . import matrix as matrix_module
        sys.argv=['dspilot.matrix','--prefix',args.prefix,'--repeats',str(args.repeats)]
        for value in args.task_ids or []:sys.argv += ['--task-id',value]
        for value in args.persona_ids or []:sys.argv += ['--persona-id',value]
        if args.execute:sys.argv.append('--execute')
        if args.calibrate:sys.argv.append('--calibrate')
        if args.resume:sys.argv.append('--resume')
        matrix_module.main();return
    episode=_activate(args.task_id,args.persona_id)
    if args.command=='validate':
        print(json.dumps({'valid':True,'task_id':episode.task.id,'persona_id':episode.persona['id'],
                          'requirements':[{'id':r['id'],'template':r['template'],'trigger':r['trigger'],
                                           'runtime_trigger':r.get('runtime_trigger',r['trigger']),
                                           'checker':r['checker']['kind']} for r in episode.requirements]},
                         indent=2,ensure_ascii=False));return
    if args.command=='run':
        from . import run_v3
        sys.argv=['dspilot.run_v3','--task-id',args.task_id,'--persona-id',args.persona_id,
                  '--arm',args.arm,'--run-id',args.run_id]
        run_v3.main();return
    if args.command=='calibrate':
        from . import calibrate_v3
        calibrate_v3.main();return
    if args.command=='suite':
        from . import suite as suite_module
        sys.argv=['dspilot.suite','--prefix',args.prefix,'--repeats',str(args.repeats)]
        suite_module.main();return


if __name__=='__main__':
    main()
