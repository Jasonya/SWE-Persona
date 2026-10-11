"""Read-only validation of an exported TB3-layout MultiLang evidence delivery."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def verify(root):
    root=root.resolve();lock=read(root/'files.sha256.json')
    for name,h in lock.items():
        path=(root/name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=h:
            raise ValueError('Payload mismatch: '+name)
    run=root/'runs/collection-20261003';rows=read(run/'results.json');selected=[r for r in rows if r['detail_included']]
    summary=read(root/'summary.json');status=read(run/'status.json')
    assert len(rows)==status['new_attempts']==1975
    assert dict(Counter(r['status'] for r in rows))==summary['record_counts']
    assert len(selected)==60 and len({r['task_key'] for r in selected})==45
    assert all(r['detail_included']==(r['status']=='completed') for r in rows)
    successes=0
    for row in selected:
        trial=run/row['trial'];result=read(trial/'persona-result.json');checks=read(trial/'persona-check.json')
        original=read(trial/'verifier/results.json');receipt=read(trial/'provenance/supervisor-result.json')
        assert row['base_task_success']==result['original_reward']==int(original['success']==1)
        assert row['joint_success']==int(result['benchmark_outcome']=='pass')
        assert receipt['status']=='completed' and receipt['seconds']<=receipt['cap_seconds']
        assert (trial/'agent/transcript.md').is_file() and (trial/'agent/model.patch').is_file()
        if row['joint_success']==1:
            assert result['simulated_user_satisfied'] and all(r['verdict']=='pass' for r in checks['checks'])
            assert result['online_requirements'][-1]['stage']=='final_recheck'
            assert result['online_requirements'][-1]['judgment']['verdict']=='MET'
            successes+=1
    assert successes==30==len(read(root/'successful-cases.json'))
    assert read(run/'calibration/calibration.json')['passed']
    assert all(r['trial'] is None for r in rows if not r['detail_included'])
    pre=read(run/'preflight.json');assert len(pre['tasks'])==999
    assert Counter(r['status'] for r in pre['tasks'])==pre['counts']
    for name,origin in read(root/'provenance/artifacts.json').items():
        assert hashlib.sha256((root/name).read_bytes()).hexdigest()==origin['export_sha256']
    return {'files_verified':len(lock),'all_records':len(rows),'detailed_trials':len(selected),
            'distinct_detailed_tasks':45,'passes':successes,'completed_failures':len(selected)-successes,
            'schema_and_numeric_checks':'passed','live_evaluation':'not_rerun'}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('directory',nargs='?',type=Path,default=Path(__file__).resolve().parent)
    a=p.parse_args();print(json.dumps(verify(a.directory),indent=2))
