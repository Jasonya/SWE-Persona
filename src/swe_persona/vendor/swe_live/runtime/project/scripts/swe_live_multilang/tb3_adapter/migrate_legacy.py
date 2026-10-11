"""Project legacy evidence to TB3 result layout without inventing v6 judgments."""
import bootstrap_adapter
from bootstrap_adapter import ROOT
import argparse
from collections import Counter
from pathlib import Path
import json
import shutil
from support import read,file_hash
from build_task import save,text


def migrate(study,output):
    study,output=study.resolve(),output.resolve()
    if output.exists(): raise ValueError('Use a new migration directory; source runs are immutable')
    rows=read(study/'results.json'); m=read(study/'manifest.json'); tasks={t['key']:t for t in m['tasks']}
    output.mkdir(parents=True); results=[]; ux=0
    for row in rows:
        src=Path(row['source_result']); assert file_hash(src)==row['result_sha256']
        key,persona=row['pair_id'].split('/'); worker=src.parent/'worker'
        dest=output/'trials'/key/persona;dest.mkdir(parents=True)
        result={'source_protocol':row['source_protocol'],'format':'TB3 result-layout migration, legacy evidence',
            'task_id':tasks[key]['instance_id'],'persona':persona,'original_reward':row['base_task_success'],
            'source_status':row['status'],'online_requirements':None,'objective_requirements':None,
            'benchmark_outcome':'invalid','tb3_protocol_compliance':'not_executed',
            'reason':'Legacy collection had no frozen evolved requirement, qualified quote-based judge or independent persona checker',
            'seconds':row['seconds'],'cap_seconds':row['cap_seconds'],'source_study':row['source_study'],
            'source_receipt_sha256':row['result_sha256'],'legacy_original_task_pass':row['base_task_success']==1 and row['status'] in ('completed','historical_rescored')}
        save(dest/'persona-result.json',result)
        save(dest/'persona-check.json',{'status':'not_executed','checks':None,
            'reason':'No technical or delivery-structure checker ran in this historical protocol'})
        for name,target in [('episode.json','agent-private/episode.json'),('transcript.json','agent/transcript.json'),
             ('transcript.md','agent/transcript.md'),('protocol.json','provenance/source-protocol.json'),
             ('dialogue-rating.json','legacy-dialogue-rating.json'),('evaluation/results.json','provenance/original-verifier.json')]:
            if (worker/name).exists():
                path=dest/target;path.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(worker/name,path)
                if name=='dialogue-rating.json': ux+=1
        save(dest/'provenance/source-result.json',read(src))
        if (worker/'predictions.json').exists():
            patch=read(worker/'predictions.json')[tasks[key]['instance_id']]['model_patch']
            path=dest/'agent/model.patch';path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(patch.encode())
        results.append({'task_id':result['task_id'],'persona':persona,'result':(dest/'persona-result.json').relative_to(output).as_posix(),
            'original_reward':result['original_reward'],'source_status':row['status'],'benchmark_outcome':'invalid',
            'source_protocol':row['source_protocol']})
    summary={'completed':len(results),'unique_tasks':len({r['task_id'] for r in results}),
        'original_task_passes':sum(r['original_reward']==1 and r['source_status'] in ('completed','historical_rescored') for r in results),
        'original_known':sum(r['original_reward'] is not None for r in results),
        'source_status_counts':dict(Counter(r['source_status'] for r in results)),
        'legacy_dialogue_ratings':ux,'tb3_benchmark_passes':0,'tb3_not_evaluated':len(results),
        'policy':'Layout migration and approximate evidence audit only; no new model calls or rescoring',
        'publication_ready':False}
    save(output/'status.json',summary);save(output/'results.json',results)
    save(output/'run-plan.json',{'kind':'legacy_migration','source_results_sha256':file_hash(study/'results.json'),
        'source_manifest_sha256':file_hash(study/'manifest.json'),'target_spec':'TB3_DELIVERY_FORMAT.md',
        'new_protocol_executed':False})
    save(output/'preflight.json',{'status':'not_executed','reason':'Historical projection only'})
    text(output/'README.md', '# Legacy MultiLang evidence in the shared result layout\n\n'
        f'{len(results)} entries; {summary["unique_tasks"]} original tasks; {summary["original_task_passes"]} retained original-task passes. '
        'Missing requirement judge/checker stages remain not executed. These are not 774 TB3-finalized trials. '
        'The legacy 0–2 dialogue ratings keep their original scale; no 1–5 experience scores are synthesized. '
        'Private episode files preserve the original recorded schema and provenance; no missing events are filled in.\n')
    save(output/'release-lock.json',{p.relative_to(output).as_posix():file_hash(p) for p in output.rglob('*') if p.is_file()})
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--study',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();print(json.dumps(migrate(a.study,a.output),indent=2))
