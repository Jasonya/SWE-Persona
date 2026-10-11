"""Opt-in public probes with a host-side read-only SQL oracle.

Candidate code only supplies untrusted SQL/parameters. The workload, authorizer,
comparison and measurements run in a separate host process after the candidate
container is gone. No candidate Python or commands execute on the host.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from .full_persona import public_probe as legacy_probe
from .sandbox import Box
from .task import BASE, sha


def sqlite_measure(probe, query_text):
    try:query=json.loads(query_text)
    except (TypeError,ValueError):
        return {'code':1,'output':'Candidate did not supply valid query JSON.','oracle':'host-owned-readonly-sqlite-v1'}
    if (not isinstance(query,dict) or set(query)!={'sql','params'} or
            not isinstance(query['sql'],str) or len(query['sql'])>16384 or
            not isinstance(query['params'],list) or len(query['params'])>100):
        return {'code':1,'output':'Candidate query exceeds the registered schema or size bounds.','oracle':'host-owned-readonly-sqlite-v1'}
    if sys.version_info<(3,11):raise RuntimeError('This staged host SQLite oracle requires Python 3.11+ for bounded database limits')
    script=next(p['script'] for p in probe['extra_files'] if p['filename']=='persona_sqlite.py')
    with tempfile.TemporaryDirectory(prefix='persona-sqlite-') as folder:
        path=Path(folder)/'query.json';path.write_text(json.dumps(query))
        # The script is source-controlled public fixture code, not candidate output.
        command=[sys.executable,'-I','-c',script.replace('/tmp/persona-query.json',str(path))]
        if probe['host_oracle']=='sqlite_cost_v1':command.append('--cost')
        result=subprocess.run(command,cwd=folder,env={'PATH':os.defpath},capture_output=True,text=True,timeout=25)
    return {'code':result.returncode,'output':result.stdout+result.stderr,
            'oracle':'host-owned-readonly-sqlite-v1','query_sha256':sha(query_text)}


def public_probe(patch,directory,probe):
    if probe.get('resource_measurement'):
        from .resource_probe import public_probe as measured_probe
        return measured_probe(patch,directory,probe)
    if not probe.get('host_oracle'):return legacy_probe(patch,directory,probe)
    if probe['host_oracle'] not in ('sqlite_semantics_v1','sqlite_cost_v1'):
        raise ValueError('Unknown host oracle')
    directory.mkdir(parents=True,exist_ok=False)
    with Box() as box:
        box.setup();box.put('/tmp/candidate.patch',patch)
        applied=box.shell('git apply --allow-empty /tmp/candidate.patch')
        files=[{'filename':probe['filename'],'script':probe['script']}]+probe.get('extra_files',[])
        for item in files:box.put('/tmp/'+item['filename'],item['script'])
        execution=box.shell(probe['command'],timeout=probe['timeout_seconds']) if applied['code']==0 else applied
        query_text=box.read('/tmp/persona-query.json')
        tampered=[item['filename'] for item in files if box.read('/tmp/'+item['filename'])!=item['script']]
        source=box.shell(probe.get('source_context_command','git diff --stat'))['output']
    oracle=None
    if query_text:
        oracle=sqlite_measure(probe,query_text)
        (directory/'candidate-query.json').write_text(query_text)
    original=execution
    output=execution['output']+'\nHOST-OWNED ORACLE (independent of candidate stdout):\n'+(oracle['output'] if oracle else 'No generated query available.')
    code=execution['code'] or (oracle['code'] if oracle else 1) or (1 if tampered else 0)
    record={'record_id':directory.name,'base_commit':BASE,'patch_sha256':sha(patch),
            'probe_id':probe['id'],'case_sha256':sha(probe['script']),'script':probe['script'],
            'extra_files':probe.get('extra_files',[]),'execution':{'code':code,'output':output},
            'container_execution':original,'host_oracle':oracle,'tampered_fixture_files':tampered,
            'source_context':source,'passed':code==0}
    (directory/'record.json').write_text(json.dumps(record,indent=2));return record
