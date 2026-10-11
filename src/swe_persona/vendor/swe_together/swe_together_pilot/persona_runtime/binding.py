"""Bind original pool templates without LLM-authored requirements or rubrics."""
import copy
import hashlib
import json
from pathlib import Path
import re

PILOT=Path(__file__).resolve().parents[1]
POOL=PILOT.parent/'human-interactive-coding-personas.v3.json'
FULL109_POOL=PILOT.parent/'human-interactive-coding-personas.full109.v1.json'


def _pool_path(variant):
    paths={'v3':POOL,'full109_v1':FULL109_POOL}
    try:return paths[variant]
    except KeyError:raise ValueError(f'Unknown persona pool variant: {variant}') from None


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def bind(task_id, persona_id, specs, applicability_reason, task_root=None, pool_variant='v3'):
    if not applicability_reason.strip():raise ValueError('An explicit applicability review is required')
    root=Path(task_root) if task_root is not None else PILOT/'SWE-Together/tasks'
    task=root/task_id
    if task.parent != root or not task.is_dir():raise ValueError('Unknown frozen task')
    pool=json.loads(_pool_path(pool_variant).read_text())
    matches=[p for p in pool['personas'] if p['id']==persona_id]
    if len(matches)!=1:raise ValueError('Persona must come from the fixed pool')
    persona=copy.deepcopy(matches[0])
    templates={r['id']:r for r in persona['attributes']['evolved_requirements']}
    bound=[]
    for spec in specs:
        template=templates[spec['template_id']]
        slots=spec.get('bindings',{})
        if set(slots)!=set(template['bindings']):raise ValueError('All and only declared binding slots are required')
        text=template['user_requirement']
        for slot,value in slots.items():
            if value.get('source_kind') not in ['public_task','public_repository','registered_environment','trusted_event'] or not value.get('source_ref') or not value.get('text'):
                raise ValueError('Each slot requires public or registered provenance')
            text=text.replace('{{'+slot+'}}',value['text'])
        if re.search(r'{{[^}]+}}',text):raise ValueError('Unbound template slot')
        clauses={f'{group}[{i}]' for group in ['met_if','not_met_if','unknown_if'] for i,_ in enumerate(template['satisfaction'][group])}
        message_only=spec.get('message_only_clauses',[])
        if not set(message_only)<=clauses:raise ValueError('Unknown message-grounded clause')
        bound.append({'id':template['id'],'statement':text,'bindings':slots,'trigger':copy.deepcopy(template['trigger']),
                      'rubric':copy.deepcopy(template['satisfaction']),
                      'message_only_clauses':message_only,'checker':spec.get('checker'),
                      'checker_parameters':copy.deepcopy(spec.get('checker_parameters',{})),
                      'earliest_turn':spec.get('earliest_turn',2),'deadline_turn':spec.get('deadline_turn')})
    if not bound or len({r['id'] for r in bound})!=len(bound):raise ValueError('Unique, nonempty bound templates required')
    persona['bound_evolved_requirements']=bound
    # Keep the three sources of user behavior explicit.  ``persona`` remains
    # backwards compatible with existing packets; these aliases prevent the
    # original task persona, the pool persona, and the evolved requirements
    # from being conflated by downstream analysis.
    initial_pool=copy.deepcopy({k:v for k,v in persona.items() if k!='bound_evolved_requirements'})
    # Template definitions are evaluator-side data.  Do not expose all three
    # candidates to the user simulator before their registered trigger fires.
    initial_pool['attributes'].pop('evolved_requirements',None)
    packet={'schema_version':'pool-simulator-v3','evaluation_policy':'grounded_llm_persona_v1','task_id':task_id,'pool_sha256':digest(pool),'persona':persona,
            'applicability_review':applicability_reason,
            'original_instruction':(task/'instruction.md').read_text(),
            'original_user_context':(task/'user_simulation_prompt.md').read_text(),
            'base_user_persona':{'source':'swe-together:user_simulation_prompt.md','text':(task/'user_simulation_prompt.md').read_text()},
            'initial_pool_persona':initial_pool,
            'unbound_template_ids':sorted(set(templates)-{r['id'] for r in bound}),
            'stop_policy':'completion_requires_all_persona_requirements_met; hard budget and failure stops remain terminal',
            'base_verifier':'unchanged'}
    if pool_variant!='v3':packet['pool_variant']=pool_variant
    packet['binding_sha256']=digest(packet)
    return packet


def validate(packet):
    if packet['binding_sha256']!=digest({k:v for k,v in packet.items() if k!='binding_sha256'}):raise ValueError('Modified binding')
    if packet.get('evaluation_policy')!='grounded_llm_persona_v1':
        raise ValueError('Rebind this packet under grounded_llm_persona_v1')
    pool=json.loads(_pool_path(packet.get('pool_variant','v3')).read_text())
    if packet['pool_sha256']!=digest(pool):raise ValueError('Persona pool changed')
    phase=packet.get('native_phase_contract')
    if phase is not None:
        if phase.get('version')!='native-phase-disclosure-v2' or phase.get('acceptance_requires_all_followups_disclosed') is not True:
            raise ValueError('Unsupported native disclosure contract')
        ids=set()
        for f in phase['required_followups']:
            if not isinstance(f['id'],str) or f['id'] in ids or not f['id'] or type(f['after_agent_turn']) is not int or f['after_agent_turn']<1 or not f['text'].strip():
                raise ValueError('Invalid native disclosure schedule')
            ids.add(f['id'])
    original=next(p for p in pool['personas'] if p['id']==packet['persona']['id'])
    if {k:v for k,v in packet['persona'].items() if k!='bound_evolved_requirements'}!=original:raise ValueError('Persona attributes were rewritten')
    if packet.get('schema_version')=='pool-simulator-v3':
        base=packet.get('base_user_persona',{})
        initial=packet.get('initial_pool_persona',{})
        if base.get('source')!='swe-together:user_simulation_prompt.md' or base.get('text')!=packet.get('original_user_context'):
            raise ValueError('Base user persona must match the frozen SWE-Together prompt')
        expected=copy.deepcopy({k:v for k,v in original.items() if k!='bound_evolved_requirements'})
        expected['attributes'].pop('evolved_requirements',None)
        if initial!=expected:raise ValueError('Initial pool persona must exclude undisclosed evolved templates')

    templates={r['id']:r for r in original['attributes']['evolved_requirements']}
    for req in packet['persona']['bound_evolved_requirements']:
        source=templates[req['id']]
        if req['rubric']!=source['satisfaction'] or req['trigger']!=source['trigger']:raise ValueError('Pool rubric or trigger changed')
        text=source['user_requirement']
        if set(req['bindings'])!=set(source['bindings']):raise ValueError('Bindings changed')
        for slot,value in req['bindings'].items():text=text.replace('{{'+slot+'}}',value['text'])
        if text!=req['statement']:raise ValueError('Requirement text changed')


def sample_bindings(candidates, per_task=3, seed=0):
    """Sample only from explicit reviewed bindings, not unreviewed pool pairs."""
    import random
    if per_task<1:raise ValueError('per_task must be positive')
    groups={};seen=set()
    for candidate in candidates:
        validate(candidate)
        key=(candidate['task_id'],candidate['persona']['id'])
        if key in seen:raise ValueError('Duplicate task/persona candidate')
        seen.add(key);groups.setdefault(key[0],[]).append(candidate)
    rng=random.Random(seed)
    selected=[];coverage=[]
    for task,group in sorted(groups.items()):
        group=sorted(group,key=lambda b:b['persona']['id'])
        chosen=rng.sample(group,min(per_task,len(group)))
        selected.extend(chosen)
        coverage.append({'task_id':task,'reviewed_eligible':len(group),'selected':len(chosen),'requested':per_task})
    return {'seed':seed,'coverage':coverage,'bindings':selected}
