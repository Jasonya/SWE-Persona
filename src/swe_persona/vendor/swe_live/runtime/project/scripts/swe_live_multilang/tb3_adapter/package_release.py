"""Seal frozen task bindings with a companion runtime; no model/Docker calls."""
import bootstrap_adapter
from bootstrap_adapter import ROOT,HERE,SCRIPTS
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
from tempfile import TemporaryDirectory
from support import file_hash,read
from build_task import validate,save,text
from protocol import PROTOCOL


VALIDATOR = '''from pathlib import Path
import hashlib,json,sys
root=Path(__file__).resolve().parent
lock=json.loads((root/'release-lock.json').read_text(encoding='utf-8'))
bad=[name for name,h in lock.items() if not (root/name).is_file() or hashlib.sha256((root/name).read_bytes()).hexdigest()!=h]
if bad: raise SystemExit('Integrity mismatch: '+str(bad[:10]))
sys.path.insert(0,str(root/'runtime/project/scripts/swe_live_multilang/tb3_adapter'))
from build_task import validate
inventory=json.loads((root/'inventory.json').read_text(encoding='utf-8'))
for task in inventory: validate(root/task['path'])
manifest=json.loads((root/'release-manifest.json').read_text(encoding='utf-8'))
print(json.dumps({'files_verified':len(lock),'tasks_validated':len(inventory),'official_harbor_packager_at_build':manifest['official_harbor_packager_validation'],'live_runs':'not_implied'}))
'''


def package(tasks_root,output):
    tasks_root,output=tasks_root.resolve(),output.resolve()
    if output.exists():raise ValueError('Use a new release version directory')
    tasks=[p for p in sorted(tasks_root.iterdir()) if p.is_dir() and (p/'release-lock.json').exists()]
    if not tasks:raise ValueError('No frozen task bundles')
    from harbor.models.task.config import TaskConfig
    from harbor.publisher.packager import Packager
    from harbor.models.dataset.manifest import DatasetManifest
    inventory=[]; packaging=[]
    for task in tasks:
        result=validate(task);identity=read(task/'tests/persona/build-identity.json')
        shutil.copytree(task,output/'tasks'/task.name)
        config=TaskConfig.model_validate_toml((task/'task.toml').read_text(encoding='utf-8'))
        content_hash,collected=Packager.compute_content_hash(task)
        with TemporaryDirectory() as temporary:
            restored=Path(temporary)/'task'
            for source in collected:
                target=restored/source.relative_to(task);target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
            validate(restored)
        packaging.append({'task':task.name,'packaged_files':len(collected),'all_persona_rules_retained':True,
            'reassembled_validation':'passed','content_hash':content_hash})
        inventory.append({'id':task.name,'path':'tasks/'+task.name,'source_task_id':result['original_task'],
            'persona_id':identity['persona_id'],'task_lock_sha256':file_hash(task/'tests/persona/task-lock.json'),
            'package_name':config.task.name,'content_hash':content_hash,
            'runtime_validation':'offline_only','source_task_sha256':identity['source_task_sha256']})
    project=output/'runtime/project'
    files=[]
    for directory in (SCRIPTS,ROOT/'tb2_persona_pilot'):
        files.extend(p for p in directory.rglob('*') if p.is_file() and '__pycache__' not in p.parts
                     and p.suffix in ('.py','.cjs','.ps1') and 'runs' not in p.relative_to(directory).parts)
    files += [ROOT/'human-interactive-coding-personas.v3.json',ROOT/'LICENSE']
    for source in files:
        target=project/source.relative_to(ROOT);target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
    upstream={}
    for relative in ('.local/SWE-bench-Live','.local/SWE-bench-Live/launch'):
        source=ROOT/relative
        names=subprocess.check_output(['git','-C',str(source),'ls-files','-z']).decode().split('\0')
        upstream[relative]=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD']).decode().strip()
        for name in names:
            path=source/name
            needed = (name.startswith('evaluation/') or '/' not in name) if relative.endswith('SWE-bench-Live') else (name.startswith('launch/') or '/' not in name)
            if path.is_file() and needed:
                target=project/relative/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,target)
    # The simulator is the project's MIT implementation, not an impersonated TB3 vendor dependency.
    text(output/'runtime/README.md','Project-native user simulator and native SWE adapter, MIT license in project/LICENSE. '
        'This adapter does not vendor or claim use of TB3\'s unpublished User Simulation runtime.\n')
    save(output/'inventory.json',inventory);save(output/'registry.json',{'protocol':PROTOCOL,'tasks':[t['id'] for t in inventory]})
    save(output/'source-accepted.json',{'status':'bound_shape_requirements','tasks':inventory,'live_acceptance_not_implied':True})
    save(output/'release-manifest.json',{'protocol':PROTOCOL,'tasks':inventory,'upstream_revisions':upstream,
        'delivery_spec':'TB3_DELIVERY_FORMAT.md','compatibility':'Harbor task layout plus native SWE companion adapter',
        'official_harbor_packager_validation':'passed','technical_scenario_coverage':False})
    save(output/'provenance/harbor-packaging.json',{'source_commit':'bf991e490394ef9c3250a6db2bc5cbda903cb4c3','tasks':packaging})
    text(output/'requirements.txt','docker>=7.1.0\nopenai\njsonschema\ndatasets\npython-dotenv\n-e ./runtime/project/.local/SWE-bench-Live/launch\n')
    boot="from pathlib import Path\nimport sys\nroot=Path(__file__).resolve().parent\nsys.path.insert(0,str(root/'runtime/project/scripts/swe_live_multilang/tb3_adapter'))\n"
    text(output/'run.py',boot+"from single_release import main\nraise SystemExit(main(['--dataset',str(root),*sys.argv[1:]]))\n")
    text(output/'run_batch.py',boot+"import json,subprocess,argparse\np=argparse.ArgumentParser();p.add_argument('--output',default=str(root.parent/'runs/batch'));p.add_argument('--dry-run',action='store_true');a=p.parse_args()\nfor task in json.loads((root/'inventory.json').read_text(encoding='utf-8')):\n command=[sys.executable,str(root/'run.py'),'--task',task['id'],'--output',a.output]\n if a.dry_run:command.append('--dry-run')\n if subprocess.call(command):raise SystemExit('Trial infrastructure failure; resume preserves earlier records')\n")
    text(output/'calibrate_judge.py',boot+"import argparse\nfrom calibrate_judge import calibrate\np=argparse.ArgumentParser();p.add_argument('--output',default=str(root.parent/'runs/calibration'));p.add_argument('--model',default='gpt-6-astra');a=p.parse_args();calibrate(Path(a.output).resolve(),a.model)\n")
    text(output/'validate_release.py',VALIDATOR)
    test_header="from pathlib import Path\nimport sys\nsys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime/project/scripts/swe_live_multilang/tb3_adapter'))\n"
    text(output/'tests/test_adapter.py',test_header+(HERE/'test_adapter.py').read_text(encoding='utf-8'))
    prompts=output/'prompts';prompts.mkdir()
    for f in (tasks[0]/'tests/persona/closed_loop').iterdir():
        if 'prompt' in f.name or 'schema' in f.name:shutil.copyfile(f,prompts/f.name)
    save(output/'provenance/packaging.json',{'method':'Independent directory copy; exact native task rows, original patches and tests retained',
        'agent_boundary':'Only environment image and disclosed public messages enter the agent; tests/persona and solution remain host-only'})
    text(output/'README.md','# MultiLang persona delivery bundle\n\n'
        'Install Python >=3.12, Docker, Git and requirements.txt. Run `python validate_release.py`, then '
        '`python run.py --task <inventory-id> --dry-run`. Set host AZURE_OPENAI_API_KEY and use the team model registry. '
        '`python run.py --task <inventory-id> --output ../runs/single` performs judge calibration and the native task evaluation. '
        '`python run_batch.py --output ../runs/batch` reproduces included bindings; it does not schedule the full 1,077-task exploration. '
        'The full PC collection command and its frozen random draws remain in the source runner.\n\n'
        'Each included task binds one explicit, objectively checked handoff-format requirement. Online MET is checked using '
        'qualified visible quotes, followed by final recheck and independent native/checker verdicts. Technical scenario '
        'requirements (resource peaks, incident recovery, credentials, races) are not claimed as validated. '
        'Original model test-file edits use the recorded v2 projection policy; native official tests are unchanged.\n\n'
        'This is a new MultiLang adapter, not a claim of TB3 v6-equivalent measurements. Official Harbor configuration, file collection '
        'and independent reassembly checks passed. Live positive/negative task controls remain unexecuted. Plain harbor run does not activate this companion simulator '
        'or native SWE verifier. Do not mount tests/ or solution/ into the coding-agent environment. '
        'The runtime attachment and task files must be distributed together. Keep outputs outside the frozen release.\n')
    with tarfile.open(output/'runtime_bundle.tar.gz','w:gz') as archive:
        for name in ('runtime','prompts','run.py','run_batch.py','calibrate_judge.py','validate_release.py','requirements.txt'):
            archive.add(output/name,arcname=name)
    dataset=['schema_version = "1.0"','[dataset]','name = "personacodebench/swe-live-multilang-handoff-v1"','version = "1"','']
    for t in inventory:
        dataset+=['[[tasks]]','name = '+json.dumps(t['package_name']),
                  'digest = '+json.dumps('sha256:'+t['content_hash']),'']
    for name in ('runtime_bundle.tar.gz','README.md','inventory.json'):
        dataset+=['[[files]]','path = '+json.dumps(name),'digest = '+json.dumps('sha256:'+file_hash(output/name))]
    text(output/'dataset.toml','\n'.join(dataset)+'\n')
    DatasetManifest.from_toml((output/'dataset.toml').read_text(encoding='utf-8'))
    lock={p.relative_to(output).as_posix():file_hash(p) for p in sorted(output.rglob('*')) if p.is_file()}
    save(output/'release-lock.json',lock);text(output/'SHA256SUMS',''.join(h+'  '+n+'\n' for n,h in lock.items()))
    return {'tasks':len(inventory),'files':len(lock),'output':str(output),'protocol':PROTOCOL}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--tasks',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();print(json.dumps(package(a.tasks,a.output),indent=2))
