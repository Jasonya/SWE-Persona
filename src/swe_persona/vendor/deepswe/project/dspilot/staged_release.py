"""Freeze existing configurations, check basics, then execute the frozen cohort.

Preparation never runs here. Failed attempts and excluded pairs remain visible;
smoke episodes count as cohort executions and are never silently repeated.
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

from . import specs, staged_batch as batch
from .planning_staged import digest, dump, verify_prepared_plan

VERSION = 'staged-release-v1'
BASIC_VERSION = 'staged-release-v2-basic-checks'


def directory_for(release_id):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,49}',release_id):
        raise ValueError('release-id must be 1..50 letters/digits/hyphens/underscores')
    path=specs.ROOT/'runs/staged-releases'/release_id
    if path.resolve()!=path:raise ValueError('Release paths cannot use symlinks')
    return path


def identity(pair):return pair['task_id'],pair['persona_id']


def pipeline_state(pipeline_id):
    from .staged_pipeline import directory_for as pipeline_directory
    return batch.read(pipeline_directory(pipeline_id)/'state.json')


def verify_entry(entry):
    state=pipeline_state(entry['pipeline_id'])
    if identity(state)!=identity(entry) or state.get('plan')!=entry['plan']:
        raise ValueError('Prepared pipeline identity or selected plan changed')
    path=specs.ROOT/entry['plan']
    ep=specs.compile_episode(*identity(entry))
    receipt=verify_prepared_plan(path,ep)
    if digest(path)!=entry['plan_sha256'] or digest(path.parent/'admission.json')!=entry['admission_sha256']:
        raise ValueError('Frozen plan or admission changed')
    return receipt


def freeze_reviewed(release_id, batch_id, *, replacements=(), representatives=()):
    """Historical v1 workflow retained for reproducing old releases only."""
    directory=directory_for(release_id)
    if directory.exists():raise ValueError('Release is immutable; choose a fresh release-id')
    source=batch.directory_for(batch_id);manifest=batch.read(source/'manifest.json')
    attempts=batch.read(source/'attempts.json')
    if manifest.get('phase')!='prepare':raise ValueError('A preparation-only batch is required')
    if (source/'stop-request.json').exists():raise ValueError('Preparation batch was stopped')
    if any(attempts.get(p['pipeline_id'],{}).get('status') not in ('finished','interrupted') for p in manifest['pairs']):
        raise ValueError('Finish every preparation attempt before freezing')
    pairs=[{k:p[k] for k in ('task_id','persona_id')} for p in manifest['pairs']]
    seal=batch.fingerprint(pairs)
    if manifest['seal']!=seal:raise ValueError('Preparation sources/models changed; prepare a new batch')
    selected={identity(p):p['pipeline_id'] for p in manifest['pairs']}
    replaced=set()
    for pipeline_id in replacements:
        state=pipeline_state(pipeline_id);key=identity(state)
        if key not in selected or key in replaced:raise ValueError('Unknown or duplicate replacement pair')
        if state.get('status')!='prepared':raise ValueError('Replacement must be a fresh prepared pipeline')
        selected[key]=pipeline_id;replaced.add(key)
    entries=[];excluded=[]
    for pair in manifest['pairs']:
        pipeline_id=selected[identity(pair)];state=pipeline_state(pipeline_id)
        if state and identity(state)!=identity(pair):raise ValueError('Preparation identity mismatch')
        row={k:pair[k] for k in ('task_id','persona_id')}
        row.update(pipeline_id=pipeline_id,original_pipeline_id=pair['pipeline_id'])
        if state.get('status')!='prepared':
            excluded.append(row|{'status':state.get('status'),'reasons':state.get('reasons',[]),
                'missing_information':state.get('missing_information',[]),'last_design_feedback':state.get('feedback',[])[-1:]})
            continue
        path=specs.ROOT/state['plan'];plan=batch.read(path)
        entry=row|{'plan':state['plan'],'plan_sha256':digest(path),
            'admission_sha256':digest(path.parent/'admission.json'),
            'task_disclosure_mode':plan['task_disclosure_mode'],
            'persona_disclosure_mode':plan['persona_disclosure_mode']}
        receipt=verify_entry(entry)
        models={role:receipt['models'].get(key) or receipt['models'].get('AGENT_MODEL')
                for role,key in (('agent','AGENT_MODEL'),('simulator','SIMULATOR_MODEL'),('judge','JUDGE_MODEL'))}
        if models!=seal['models']:raise ValueError('Prepared model configuration is stale')
        entry['preparation_warnings']=batch.read(path.parent/'review.json')['result']['warnings']
        entries.append(entry)
    if not entries:raise ValueError('No executable prepared configurations')
    chosen=list(representatives)
    if not chosen:
        first=next((e for e in entries if e['task_disclosure_mode']=='progressive'),entries[0])
        chosen=[first['pipeline_id']]
        rest=[e for e in entries if e!=first]
        if rest:
            second=max(rest,key=lambda e:(e['task_disclosure_mode']!=first['task_disclosure_mode'],
                e['persona_id']!=first['persona_id'],e['task_id']!=first['task_id']))
            chosen.append(second['pipeline_id'])
    if not 1<=len(chosen)<=2 or len(set(chosen))!=len(chosen) or not set(chosen)<={e['pipeline_id'] for e in entries}:
        raise ValueError('Select one or two distinct ready representative pipelines')
    frozen={'version':VERSION,'release_id':release_id,'created_at':batch.now(),'source_batch':batch_id,
        'source_manifest_sha256':digest(source/'manifest.json'),'seal':seal,'inventory':pairs,
        'arm':manifest['arm'],'max_revisions':manifest['max_revisions'],
        'pairs':entries,'excluded_pairs':excluded,'representatives':chosen,
        'handoff_policy':'Real simulator delivery, agent calls and valid final evaluation; benchmark pass is not required.',
        'timing_scope':'Only staged interaction time; preparation, handoff orchestration and final evaluation excluded.'}
    directory.mkdir(parents=True,exist_ok=False)
    dump(directory/'manifest.json',frozen)
    dump(directory/'manifest-digest.json',{'sha256':digest(directory/'manifest.json')})
    result=status(directory,frozen);dump(directory/'report.json',result)
    return result


def freeze(release_id, batch_id=None, *, inventory_path=None, replacements=(), representatives=()):
    """Copy all selected proposals; semantic review verdicts are advisory."""
    from .frozen_staged import freeze_plan, select_source
    from .staged_pipeline import directory_for as pipeline_directory
    directory=directory_for(release_id)
    if directory.exists():raise ValueError('Release is immutable; choose a fresh release-id')
    if batch_id and inventory_path:raise ValueError('Choose from-batch or from-inventory')
    if inventory_path:
        source=specs.ROOT/Path(inventory_path)
        rows=batch.read(source)['pairs']; origin={'inventory':str(source.relative_to(specs.ROOT)), 'sha256':digest(source)}
        arm='pivot'
    elif batch_id:
        source=batch.directory_for(batch_id)
        if (source/'stop-request.json').exists():raise ValueError('Source preparation batch was stopped')
        old=batch.read(source/'manifest.json')
        if old.get('phase')!='prepare':raise ValueError('A preparation-only batch is required')
        attempts=batch.read(source/'attempts.json')
        if any(attempts.get(p['pipeline_id'],{}).get('status') not in ('finished','interrupted') for p in old['pairs']):
            raise ValueError('Finish every preparation attempt before freezing')
        rows=old['pairs'];origin={'batch':batch_id,'sha256':digest(source/'manifest.json')};arm=old['arm']
    else:
        rows=batch.inventory();origin={'selection':'registered_staged_bindings'};arm='pivot'
    rows=[dict(r) for r in rows]
    if not rows or len({identity(r) for r in rows})!=len(rows):raise ValueError('Empty or duplicate cohort')
    selected={identity(r):r for r in rows};seen=set()
    for key in replacements:
        state=pipeline_state(key);pair=identity(state)
        if pair not in selected or pair in seen:raise ValueError('Unknown or duplicate replacement pair')
        selected[pair]={'task_id':pair[0],'persona_id':pair[1],'pipeline_id':key};seen.add(pair)
    # Resolve every source before writing anything; no silent fallback on missing files.
    resolved=[select_source(selected[identity(row)]) for row in rows]
    targets=[pipeline_directory(f'{release_id}-{i:02d}') for i in range(1,len(rows)+1)]
    if any(p.exists() for p in targets):raise ValueError('Frozen pipeline IDs already exist; use a fresh release-id')
    pairs=[{'task_id':ep.task.id,'persona_id':ep.persona['id']} for ep,_,_ in resolved]
    seal=batch.fingerprint(pairs)
    directory.mkdir(parents=True,exist_ok=False)
    entries=[];excluded=[];snapshots=[]
    for (ep,path,notes),target,pair in zip(resolved,targets,pairs):
        state=freeze_plan(ep,target,path,advisory=notes)
        frozen=specs.ROOT/state['plan']
        entry=pair|{'pipeline_id':target.name,'original_pipeline_id':notes.get('source_pipeline_id'),
            'plan':state['plan'],'plan_sha256':digest(frozen)}
        snapshots.append(entry)
        if state['status']!='prepared':
            excluded.append(entry|{'status':state['status'],'reasons':state['reasons']});continue
        plan=batch.read(frozen)
        entry=entry|{'admission_sha256':digest(frozen.parent/'admission.json'),
            'task_disclosure_mode':plan['task_disclosure_mode'],'persona_disclosure_mode':plan['persona_disclosure_mode'],
            'preparation_warnings':notes}
        verify_entry(entry);entries.append(entry)
    if batch.fingerprint(pairs)!=seal:raise ValueError('Sources changed while freezing; use a fresh release-id')
    chosen=list(representatives)
    if not chosen:chosen=[e['pipeline_id'] for e in entries[:2]]
    if len(set(chosen))!=len(chosen) or len(chosen)>2 or not set(chosen)<={e['pipeline_id'] for e in entries}:
        raise ValueError('Select at most two distinct frozen representative pipelines')
    manifest={'version':BASIC_VERSION,'release_id':release_id,'created_at':batch.now(),
        'source_selection':origin,'seal':seal,'inventory':pairs,'frozen_configurations':snapshots,
        'arm':arm,'max_revisions':0,'pairs':entries,'excluded_pairs':excluded,'representatives':chosen,
        'admission_policy':'frozen_configuration_basic_checks','review_policy':'advisory_only',
        'handoff_policy':'Optional diagnostic only; no representative or semantic-review pass required.',
        'timing_scope':'Only staged interaction; freeze, basic checks, setup and final grading excluded.'}
    dump(directory/'manifest.json',manifest)
    dump(directory/'manifest-digest.json',{'sha256':digest(directory/'manifest.json')})
    result=status(directory,manifest);dump(directory/'report.json',result)
    return result


def verify_release(directory):
    manifest=batch.read(directory/'manifest.json')
    if manifest.get('version') not in (VERSION,BASIC_VERSION) or digest(directory/'manifest.json')!=batch.read(directory/'manifest-digest.json').get('sha256'):
        raise ValueError('Frozen release manifest changed')
    if manifest['version']==VERSION:
        source=batch.directory_for(manifest['source_batch'])
        if digest(source/'manifest.json')!=manifest['source_manifest_sha256']:
            raise ValueError('Source preparation manifest changed')
    if batch.fingerprint(manifest['inventory'])!=manifest['seal']:
        raise ValueError('Release sources/models changed; freeze a new version')
    for row in manifest.get('frozen_configurations',[]):
        if digest(specs.ROOT/row['plan'])!=row['plan_sha256']:raise ValueError('Frozen configuration changed')
    for entry in manifest['pairs']:verify_entry(entry)
    return manifest


def status(directory, manifest=None):
    manifest=manifest or batch.read(directory/'manifest.json')
    attempts=batch.read(directory/'attempts.json')
    result=batch.report(directory,manifest|{'batch_id':manifest['release_id'],'phase':'frozen_execution'},attempts)
    result.update(release_id=manifest['release_id'],registered_pair_count=len(manifest['inventory']),
                  ready_pair_count=len(manifest['pairs']),excluded_pairs=manifest['excluded_pairs'],
                  representatives=manifest['representatives'],handoff=batch.read(directory/'handoff.json') or None)
    result.update(admission_policy=manifest.get('admission_policy','reviewed_configuration'),
                  basic_checks=batch.read(directory/'environment-checks.json') or None,
                  frozen_pair_count=len(manifest.get('frozen_configurations',manifest['pairs'])))
    if result['basic_checks']:
        tasks=result['basic_checks']['tasks']
        result['environment_ready_pair_count']=sum(tasks.get(e['task_id'],{}).get('passed') is True for e in manifest['pairs'])
        runtime=result['basic_checks'].get('runtime')
        result['runtime_ready_pair_count']=sum(r.get('passed') is True for r in runtime.values()) if runtime is not None else None
    if result['status']=='finished' and result['failed_execution_attempts']:
        result['status']='finished_with_errors'
    if result['status'] not in ('stopped','finished','finished_with_errors'):
        handoff=result['handoff'] or {}
        if any(a['status']=='launching' for a in attempts.values()):result['status']='running'
        elif manifest['version']==BASIC_VERSION:
            checked=(result['basic_checks'] or {}).get('passed')
            result['status']='basic_checks_failed' if checked is False else 'ready_for_bulk' if checked is True else 'frozen'
        elif not attempts:result['status']='frozen'
        elif handoff.get('passed') is False:result['status']='handoff_failed'
        elif handoff.get('passed') is True:result['status']='ready_for_bulk'
        else:result['status']='awaiting_handoff_validation'
    return result


def handoff_evidence(entry, arm):
    """Validate observed transport and evaluation, independently of task/persona scores."""
    state=pipeline_state(entry['pipeline_id']);run_id=f"pipeline-{entry['pipeline_id']}-{arm}"
    out=specs.ROOT/'runs'/run_id
    result=batch.read(out/'result.json');turns_path=out/'turns.json';api_path=out/'api.jsonl'
    if (state.get('status')!='complete' or state.get('run_id')!=run_id or
            result.get('run_id')!=run_id or (result.get('task_id'),result.get('persona_id'))!=identity(entry) or
            result.get('arm')!=arm or result.get('evaluation_valid') is not True):
        raise ValueError('Representative has no valid completed execution')
    if result.get('user_message_source')!='user_simulator':raise ValueError('Unexpected instruction source')
    admission=result.get('execution_admission',{})
    if admission.get('plan')!=entry['plan'] or admission.get('receipt_sha256')!=entry['admission_sha256']:
        raise ValueError('Execution used a different prepared configuration')
    turns=json.loads(turns_path.read_text());users=[t for t in turns if t.get('speaker')=='user']
    calls=[json.loads(line) for line in api_path.read_text().splitlines() if line.strip()]
    if not users or any(t.get('source')!='user_simulator' for t in users):raise ValueError('No simulator-origin user requests delivered')
    if not any(c.get('role')=='agent' for c in calls):raise ValueError('No actual coding agent call recorded')
    timing=result.get('timings',{})
    if timing.get('execution_status')!='started' or timing.get('staged_execution_seconds') is None:
        raise ValueError('Missing staged execution timing evidence')
    files=[out/'result.json',turns_path,api_path]
    return {'pipeline_id':entry['pipeline_id'],'run_id':run_id,
            'files':{str(p.relative_to(specs.ROOT)):digest(p) for p in files},
            'task_pass':result.get('base_pass'),'persona_pass':result.get('persona_pass'),
            'note':'Handoff validation is not task/persona success or calibration.'}


def invoke(entry, manifest, log):
    # Identity and prepared status are checked before this subprocess. No planner
    # fallback is allowed if the saved state disappears or changes.
    env=os.environ.copy()
    for key in ('DS_STAGED_PLAN_PATH','DS_TASK_ID','DS_PERSONA_ID','DS_PROTOCOL'):env.pop(key,None)
    with log.open('w') as output:
        return subprocess.run([sys.executable,'-u','-m','dspilot.staged_pipeline','run',
            '--pipeline-id',entry['pipeline_id'],'--task-id',entry['task_id'],'--persona-id',entry['persona_id'],
            '--arm',manifest['arm'],'--prepared-only'],cwd=specs.ROOT,env=env,
            stdout=output,stderr=subprocess.STDOUT).returncode


def check_runtime(entry):
    """Exercise the actual CLI imports, adapted admission and manifest; no model calls."""
    env=os.environ.copy()
    for key in ('DS_STAGED_PLAN_PATH','DS_TASK_ID','DS_PERSONA_ID','DS_PROTOCOL'):env.pop(key,None)
    process=subprocess.run([sys.executable,'-m','dspilot.staged_cli','preflight',
        '--task-id',entry['task_id'],'--persona-id',entry['persona_id'],'--plan',entry['plan']],
        cwd=specs.ROOT,env=env,capture_output=True,text=True,timeout=240)
    if process.returncode:
        # Host configuration/credentials are not included in the command or output.
        lines=process.stderr.strip().splitlines()
        return {'passed':False,'error':lines[-1] if lines else 'Runtime preflight failed',
                'returncode':process.returncode,'model_calls':0}
    result=json.loads(process.stdout)
    if (result.get('task_id'),result.get('persona_id'),result.get('plan'))!=(entry['task_id'],entry['persona_id'],entry['plan']):
        raise ValueError('Runtime preflight identity mismatch')
    return result


def check_environments(directory, manifest):
    from .frozen_staged import environment_check
    tasks={}
    for entry in manifest['pairs']:
        if entry['task_id'] in tasks:continue
        try:tasks[entry['task_id']]=environment_check(specs.compile_episode(*identity(entry)))
        except (ValueError,OSError,subprocess.SubprocessError) as exc:
            tasks[entry['task_id']]={'passed':False,'error':str(exc)}
    runtime={}
    if manifest['version']==BASIC_VERSION:
        for entry in manifest['pairs']:
            if (directory/'stop-request.json').exists():
                runtime[entry['pipeline_id']]={'passed':False,'error':'Release stopped'}
                continue
            if not tasks[entry['task_id']]['passed']:
                runtime[entry['pipeline_id']]={'passed':False,'error':'Environment unavailable'};continue
            try:runtime[entry['pipeline_id']]=check_runtime(entry)
            except (ValueError,OSError,subprocess.SubprocessError) as exc:
                runtime[entry['pipeline_id']]={'passed':False,'error':str(exc)}
    report={'checked_at':batch.now(),'passed':all(r['passed'] for r in [*tasks.values(),*runtime.values()]),'tasks':tasks,
            'runtime':runtime,
            'timing_included':False,'model_calls':0}
    dump(directory/'environment-checks.json',report)
    return report


def execute(release_id, *, command='status', launcher=invoke):
    if command not in ('status','check','smoke','run'):raise ValueError('Unknown release command')
    directory=directory_for(release_id)
    if command=='status' or (directory/'stop-request.json').exists():return status(directory)
    with (directory/'execution.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        manifest=verify_release(directory);attempts=batch.read(directory/'attempts.json')
        handoff=batch.read(directory/'handoff.json')
        basic=manifest['version']==BASIC_VERSION
        if command=='check' or basic:
            environment=check_environments(directory,manifest)
            if command=='check':
                result=status(directory,manifest);dump(directory/'report.json',result)
                return result
        if command=='run' and not basic:
            if handoff.get('passed') is not True or handoff.get('manifest_sha256')!=digest(directory/'manifest.json'):
                raise ValueError('Complete representative handoff validation before bulk execution')
            if [e['pipeline_id'] for e in handoff.get('evidence',[])]!=manifest['representatives']:
                raise ValueError('Handoff representative list changed')
            for evidence in handoff['evidence']:
                entry=next(e for e in manifest['pairs'] if e['pipeline_id']==evidence['pipeline_id'])
                if handoff_evidence(entry,manifest['arm'])!=evidence:raise ValueError('Handoff evidence changed')
        selected=[e for e in manifest['pairs'] if command=='run' or e['pipeline_id'] in manifest['representatives']]
        for entry in selected:
            if (directory/'stop-request.json').exists():break
            key=entry['pipeline_id']
            if key in attempts:
                if attempts[key]['status']=='launching':
                    attempts[key].update(status='interrupted',ended_at=batch.now())
                    dump(directory/'attempts.json',attempts)
                continue
            verify_release(directory)
            if pipeline_state(key).get('status')!='prepared':raise ValueError('Execution needs an unused prepared pipeline; no replanning or reroll')
            if basic and (not environment['tasks'][entry['task_id']]['passed'] or
                          not environment['runtime'][key]['passed']):
                # Leave unused pairs retryable after environment repair; no API call.
                continue
            attempts[key]={'status':'launching','phase':command,'started_at':batch.now()}
            dump(directory/'attempts.json',attempts)
            print(f'START {command} {key}',flush=True)
            try:
                code=launcher(entry,manifest,directory/f'{key}.log')
                attempts[key].update(status='finished',returncode=code,ended_at=batch.now())
            except Exception as exc:
                attempts[key].update(status='interrupted',error_type=type(exc).__name__,ended_at=batch.now())
            dump(directory/'attempts.json',attempts);dump(directory/'report.json',status(directory,manifest))
            print(f"END {command} {key} status={pipeline_state(key).get('status')} returncode={attempts[key].get('returncode')}",flush=True)
        if command=='smoke' and not handoff and not (directory/'stop-request.json').exists():
            evidence=[];errors=[]
            for key in manifest['representatives']:
                entry=next(e for e in manifest['pairs'] if e['pipeline_id']==key)
                try:evidence.append(handoff_evidence(entry,manifest['arm']))
                except (ValueError,OSError,KeyError) as exc:errors.append({'pipeline_id':key,'reason':str(exc)})
            dump(directory/'handoff.json',{'passed':not errors,'checked_at':batch.now(),
                'manifest_sha256':digest(directory/'manifest.json'),'evidence':evidence,'errors':errors})
        result=status(directory,manifest);dump(directory/'report.json',result)
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('freeze','status','check','smoke','run'))
    parser.add_argument('--release-id',required=True)
    parser.add_argument('--from-batch')
    parser.add_argument('--from-inventory',help='JSON pairs[] selecting existing pipelines/plans, including rejected proposals')
    parser.add_argument('--replacement-pipeline',action='append',default=[])
    parser.add_argument('--representative',action='append',default=[])
    add_backend_argument(parser)
    args=parser.parse_args()
    apply_backend_argument(args)
    if args.command=='freeze':
        result=freeze(args.release_id,args.from_batch,inventory_path=args.from_inventory,
                      replacements=args.replacement_pipeline,representatives=args.representative)
    else:
        if args.from_batch or args.from_inventory or args.replacement_pipeline or args.representative:parser.error('Configuration options apply only to freeze')
        result=execute(args.release_id,command=args.command)
    print(json.dumps(result,indent=2,ensure_ascii=False))
    if args.command=='smoke' and (result.get('handoff') or {}).get('passed') is not True:raise SystemExit(1)
    if args.command in ('check','run') and (result.get('basic_checks') or {}).get('passed') is False:raise SystemExit(1)
    if args.command in ('run','smoke') and result.get('failed_execution_attempts'):raise SystemExit(1)


if __name__=='__main__':main()
