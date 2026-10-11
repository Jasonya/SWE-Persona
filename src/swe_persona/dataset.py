"""Read, validate and materialize one row without exposing evaluator data to agents."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import urllib.request

import pyarrow.parquet as pq
import toml

from .schema import SCHEMA, validate_row, safe_path

VENDOR = Path(__file__).parent / 'vendor'
ENGINES = {'terminal-bench-3':'tb3', 'swe-bench-pro-mini':'swe_pro/mini', 'swe-bench-pro-shell':'swe_pro/shell', 'swe-bench-live':'swe_live', 'swe-together':'swe_together', 'deep-swe':'deepswe'}

def write(path, value):
    path = Path(path);path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')

def load_task(path, task_id):
    rows = pq.read_table(path, filters=[('task_id','=',task_id)], schema=SCHEMA).to_pylist()
    if len(rows) != 1: raise ValueError(f'Expected exactly one task for {task_id!r}, got {len(rows)}')
    return validate_row(rows[0])

def rows(path):
    pf = pq.ParquetFile(path)
    for index in range(pf.num_row_groups):
        for row in pf.read_row_group(index).to_pylist(): yield validate_row(row)

def requirements(row):
    out = []
    profile = row['interaction']['profile_id']
    for r in row['requirements']:
        d = json.loads(r['runtime_config_json'])
        d['depends_on'] = r['depends_on']
        d['id' if profile == 'terminal-bench-3' else 'requirement_id'] = r['requirement_id']
        if profile == 'swe-bench-pro-shell':
            d['newness_justification'] = 'Frozen persona requirement grounded in the public task contract.'
            d.setdefault('source_refs', [])
            d['checker'] = json.loads(r['checker_config_json'])
            d['schedule'] = {k:v for k,v in dict(trigger_event=r['schedule']['trigger_event'],
                trigger_guard=r['schedule']['guard_id'],earliest_turn=r['schedule']['earliest_turn'],
                deadline_turn=r['schedule']['deadline_turn'],priority=r['schedule']['priority'],required=r['required']).items() if v is not None}
        else:
            d['disclosure'] = r['disclosure']
            if profile == 'swe-bench-pro-mini': d['checker'] = json.loads(r['checker_config_json'])
            else:
                d.update(trigger_event=r['schedule']['trigger_event'],earliest_turn=r['schedule']['earliest_turn'],deadline_turn=r['schedule']['deadline_turn'])
        out.append(d)
    return out

def materialize(row, destination):
    validate_row(row)
    root = Path(destination).resolve()
    if root.exists() and any(root.iterdir()): raise ValueError('Materialization destination must be empty')
    root.mkdir(parents=True, exist_ok=True)
    profile = row['interaction']['profile_id']
    shutil.copytree(VENDOR / ENGINES[profile], root, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__'))
    short = row['source']['task_id']
    safe_path(short)
    if '/' in short: raise ValueError('source.task_id must be a directory-safe local identifier')
    task = root / 'tasks' / short
    task.mkdir(parents=True)
    for a in row['assets']:
        target = task.joinpath(*safe_path(a['path']).parts)
        payload = a['inline_bytes']
        if payload is None:
            if not a['uri'].startswith('https://'): raise ValueError('External assets require HTTPS')
            with urllib.request.urlopen(a['uri'], timeout=120) as response: payload = response.read()
        if hashlib.sha256(payload).hexdigest() != a['sha256']: raise ValueError('Asset hash mismatch')
        target.parent.mkdir(parents=True, exist_ok=True);target.write_bytes(payload)
        target.chmod(0o755 if a['executable'] else 0o644)
    config = json.loads(row['evaluation']['config_json'])['harbor_task_config']
    config['agent']['timeout_sec'] = row['budget']['episode_timeout_sec']
    config['verifier']['timeout_sec'] = row['budget']['verifier_timeout_sec']
    config['environment'] = json.loads(row['environment']['agent']['harbor_config_json'])
    # Metadata is descriptive; executable values come from the typed row.
    config['environment'].update({k:v for k,v in row['environment']['agent']['resources'].items() if v is not None})
    if row['environment']['agent']['image']: config['environment']['docker_image'] = row['environment']['agent']['image']
    (task/'task.toml').write_text(toml.dumps(config))
    (task/'instruction.md').write_text(row['initial_message']+'\n')
    if profile == 'deep-swe':
        from .deepswe import materialize_deepswe
        return materialize_deepswe(row,root,task)
    if profile == 'swe-together':
        from .swe_together import materialize_together
        return materialize_together(row,root,task)
    if profile == 'swe-bench-live':
        from .swe_live import materialize_live
        return materialize_live(row,root,task)
    pp = task/'tests/persona';pp.mkdir(parents=True,exist_ok=True)
    specs = requirements(row);persona=json.loads(row['persona']['profile_json'])
    native=json.loads(row['native_task']['config_json']);extra=json.loads(row['interaction']['config_json'])
    binding={'task_id':short,'persona_id':row['persona']['persona_id']}
    if profile == 'terminal-bench-3':
        from .asset_restoration import restore_tb3_assets
        restore_tb3_assets(task, short)
        native['instruction']=row['native_task']['instruction']
        native['facts']=[{'id':f['fact_id'],'text':f['text']} for f in row['native_task']['facts']]
        binding.update(persona=persona,public_contract=native,opening_message=row['initial_message'],
            proposal={'requirements':specs,'additional_artifacts':extra.get('additional_artifacts',[]),'opening_brief':row['initial_message']},
            interaction_contract=json.loads(row['persona']['interaction_contract_json']),
            original_artifacts=extra.get('original_artifacts',[]),accepted_for_screening=True,
            status='runtime_export',runtime_controls_validated=False)
        binding['binding_sha256']=hashlib.sha256(json.dumps(binding,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        inventory={'cases':[{'task_id':short,'source':f'tasks/{short}',**row['environment']['agent']['resources']}]}
    elif profile == 'swe-bench-pro-mini':
        binding.update(instance_id=row['source']['instance_id'],base_commit=row['environment']['expected_base_commit'],
                       requirements=[{'requirement_id':s['requirement_id'],'checker':s['checker']} for s in specs],
                       runner_artifacts=extra.get('runner_artifacts',[]),checker='pcb-v6-checker@6')
        write(pp/'persona.json',{'card':persona})
        contract=json.loads(row['persona']['interaction_contract_json'])
        write(pp/'interaction.json',{'agent_brief':row['agent_context'],**contract,
             'leak_guard':{'markers':extra.get('hidden_markers',[])}})
        write(pp/'blurred_task.json',{'public_task':native,'original_facts':[{'id':f['fact_id'],'wording':f['text']} for f in row['native_task']['facts']]})
        (pp/'original').mkdir(exist_ok=True);(pp/'original/instruction.md').write_text(row['native_task']['instruction'])
        write(pp/'evolved_requirements.json',{'requirements':specs})
        closed=pp/'closed_loop'
        rubric=json.loads((closed/'rubric.json').read_text())
        for item in rubric['requirements']:
            req=next(r for r in row['requirements'] if r['requirement_id']==item['requirement_id'])
            item['criteria']=json.loads(req['rubric_json'])
        write(closed/'rubric.json',rubric)
        policy=json.loads((closed/'policy.json').read_text())
        policy.setdefault('budget',{}).update(max_user_turns=row['budget']['max_user_turns'],max_simulator_decisions=row['budget']['max_simulator_decisions'])
        write(closed/'policy.json',policy)
        inventory={'tasks':[{'task_id':short}]}
    else:
        portable=root/'portable';case_id=extra['case_id'];version=extra['data_version']
        pool={'schema_version':extra['persona_schema_version'],'personas':[persona]}
        write(portable/'personas.json',pool)
        pool_hash=hashlib.sha256((portable/'personas.json').read_bytes()).hexdigest()
        binding.update(schema_version='1.0.0',data_version=version,row_index_zero=row['source']['row_index'],
            row_index_one=row['source']['row_index']+1,instance_id=row['source']['instance_id'],
            repository=row['source']['repository'],base_commit=row['environment']['expected_base_commit'],
            public_contract=native,public_contract_sha256=hashlib.sha256(json.dumps(native,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
            persona_version={'schema_version':pool['schema_version'],'sha256':pool_hash},status='ready',requirements=specs)
        case_path=f'cases/{case_id}'
        write(portable/case_path/'binding.json',binding)
        write(portable/case_path/'opening.json',dict(extra['opening'],vague_request=row['initial_message']))
        write(portable/'runtime-v3/runner/bindings'/version/case_id/'binding.json',binding)
        write(portable/'release-manifest.json',{'cases':[{'case_id':case_id,'runtime_status':'adapter_draft','runtime_path':'runtime-v3',
            'instance_id':row['source']['instance_id'],'persona_id':row['persona']['persona_id'],'data_version':version,
            'row_index_zero':row['source']['row_index'],'binding_path':case_path+'/binding.json','opening_path':case_path+'/opening.json'}]})
        # Base image is only used by the source profile after its clean-base preparation.
        text=(task/'environment/Dockerfile').read_text()
        image=next(l.split()[1] for l in text.splitlines() if l.startswith('FROM '))
        inventory={'tasks':[{'task_id':short,'case_id':case_id,'pinned_oci_reference':image}]}
    write(pp/'binding.json',binding)
    write(root/'inventory.json',inventory)
    write(root/'release-manifest.json',{'release':'swe-persona-0.1.0','task_id':row['task_id'],'content_sha256':row['content_sha256']})
    # Host-only row metadata excludes large binary payloads. It is never mounted.
    write(root/'row.json',{k:v for k,v in row.items() if k!='assets'})
    return root
