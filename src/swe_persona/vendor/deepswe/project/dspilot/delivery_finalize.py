"""Export a completed prospective batch as a new immutable delivery, without rerunning it."""
import argparse
import json
from pathlib import Path
import tempfile
from . import delivery_runtime as rt


def finalize(work, output):
    from .export_tb3_results import export_batch
    from .export_deepswe_release import package
    work = Path(work).resolve(); project = work / 'project'; batch = project / 'runs/delivery-batch'
    if not (batch/'manifest.json').is_file(): raise ValueError('No completed batch manifest; run/preflight is not a result export')
    # Verify all pairs before creating a final release. Failed-before-opening pairs
    # retain their result export, but cannot be certified as complete task deliveries.
    from .delivery_capture import verify_opening
    manifest = rt.read(batch/'manifest.json')
    for job in manifest['jobs']:
        if job['arm'] != 'pivot': raise ValueError('A Persona dataset needs Pivot openings; export baseline/control results separately')
        run = rt.safe(project, 'runs/' + job['run_id'])
        if not (run/'delivery/opening-receipt.json').is_file():
            raise ValueError('No full opening was recorded for '+job['task_id']+' / '+job['persona_id']+'; retain as incomplete, do not fabricate')
        first = next(t['content'] for t in rt.read(run/'turns.json') if t['speaker']=='user')
        verify_opening(run/'delivery',task_id=job['task_id'],persona_id=job['persona_id'],arm='pivot',
                       plan=rt.read(run/'config.json')['plan'],delivered_message=first)
    temporary_root = project/'log'; temporary_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='delivery-finalize-',dir=temporary_root) as temp:
        results = Path(temp)/'results'
        export_batch(project,batch,results,work_dir=work)
        return package(project,batch,results,Path(output),require_frozen_openings=True)


def main(dataset):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True,help='External directory created by run_batch.py')
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    rt.verify_dataset(Path(dataset))
    print(json.dumps(finalize(args.run,args.output),indent=2))
