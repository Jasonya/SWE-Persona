"""Populate a private task cache from fixed public source; no dependency on sibling dirs."""
import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def lock(task_id=None):
    from .specs import load_task, selected_ids
    task_id=task_id or selected_ids()[0]
    task=load_task(task_id)
    source=task.raw['source']
    if source.get('lock_file'):
        info=json.loads((ROOT/source['lock_file']).read_text())
        if info.get('task')!=task_id:
            raise ValueError(f'Source lock does not describe task {task_id}')
        return info
    required=('repository','commit','files')
    if any(key not in source for key in required):
        raise ValueError(f'Task {task_id} source needs {required} or lock_file')
    if not isinstance(source['files'],dict) or not source['files']:
        raise ValueError(f'Task {task_id} source files must contain pinned SHA256 entries')
    return {'deepswe_repository':source['repository'],'deepswe_commit':source['commit'],
            'task':task_id,'task_files':source['files'],'vendored_files':source.get('vendored_files',{})}


def verify_files(directory, expected):
    for relative,digest in expected.items():
        path=directory/relative
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            raise ValueError(f'Pinned source missing or modified: {relative}. Run python3 -m dspilot.prepare.')


def verify_cache(task_id=None):
    info=lock(task_id)
    verify_files(ROOT,info['vendored_files'])
    verify_files(ROOT/'.cache/tasks'/info['task'],{k:v for k,v in info['task_files'].items() if not k.startswith('solution/')})


def install(source,task_id=None):
    info=lock(task_id); task=source/'tasks'/info['task']
    verify_files(ROOT,info['vendored_files']);verify_files(task,info['task_files'])
    target=ROOT/'.cache/tasks'/info['task']
    if target.exists():
        verify_files(target,info['task_files'])
        return
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent,prefix='.prepare-') as temp:
        stage=Path(temp)/info['task'];stage.mkdir()
        for relative in info['task_files']:
            dst=stage/relative;dst.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(task/relative,dst)
        verify_files(stage,info['task_files'])
        stage.rename(target)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--from-local',type=Path,help='Optional existing DeepSWE checkout used only as a verified copy source.')
    parser.add_argument('--verify',action='store_true')
    parser.add_argument('--task-id',help='Registered DeepSWE task id (default: selected/default task).')
    args=parser.parse_args()
    if args.verify:verify_cache(args.task_id)
    elif args.from_local:install(args.from_local.resolve(),args.task_id)
    else:
        info=lock(args.task_id)
        with tempfile.TemporaryDirectory(prefix='deepswe-source-') as temp:
            source=Path(temp)
            for argv in [ ['git','init','-q',str(source)],
                          ['git','-C',str(source),'fetch','--quiet','--depth','1',f'https://github.com/{info["deepswe_repository"]}.git',info['deepswe_commit']],
                          ['git','-C',str(source),'checkout','--quiet','--detach','FETCH_HEAD'] ]:
                subprocess.run(argv,check=True)
            install(source,args.task_id)
    print('Pinned task cache verified; no sibling repository is needed at runtime.')

if __name__=='__main__':main()
