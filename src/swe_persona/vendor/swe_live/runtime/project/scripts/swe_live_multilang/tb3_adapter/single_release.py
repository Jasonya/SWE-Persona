"""Companion entry point for an independently copied release task."""
import bootstrap_adapter
from bootstrap_adapter import HERE
import argparse
import json
from pathlib import Path
import sys
from build_task import validate,save
from support import read,Manifest,file_hash
from run_fullset import get_image
from collection.run_roleplay import gold_directory
from whole_sample import supervise_sample
from run_batch import clean,existing,trial_projection
from calibrate_judge import calibrate


def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--dataset',type=Path,required=True)
    p.add_argument('--task',required=True);p.add_argument('--output',type=Path,default=Path('../runs/single'))
    p.add_argument('--model',default='gpt-6-astra');p.add_argument('--dry-run',action='store_true')
    a=p.parse_args(argv);directory=(a.dataset/'tasks'/a.task).resolve()
    if directory.parent!=(a.dataset/'tasks').resolve(): raise ValueError('Task outside dataset')
    print(json.dumps(validate(directory)))
    if a.dry_run:return 0
    output=a.output.resolve();output.mkdir(parents=True,exist_ok=True)
    trial=output/'trials'/a.task/'persona'
    receipt=existing(trial)
    if receipt is not None:
        print(json.dumps(receipt));return 0
    calibrate(output/'calibration',a.model)
    identity=read(directory/'tests/persona/build-identity.json');cap=identity['cap_seconds']
    taskfile=directory/'tests/native-task.jsonl';m=Manifest(taskfile)
    import docker
    client=docker.from_env(timeout=30)
    try:
        image,_=get_image(client,m)
        task={'key':a.task,'path':str(taskfile),'sha256':file_hash(taskfile),'instance_id':m.task_id}
        gold=gold_directory(output,task,image.id,cap)
    finally:client.close()
    cmd=[sys.executable,'-u',str(HERE/'run.py'),'--task',str(taskfile),'--persona',identity['persona_id'],
        '--binding-dir',str(directory),'--gold-dir',str(gold),'--model',a.model]
    result=supervise_sample(cmd,trial,cap,cleanup=clean);trial_projection(trial,result)
    print(json.dumps(result));return 0


if __name__=='__main__':raise SystemExit(main())
