"""Frozen contracts and deterministic disclosure policy for reactive users."""
from pathlib import Path
import hashlib, json, re, tomllib

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT
PERSONA_REPO = ROOT / 'runtime/vendor/user_simulation'
SEED = 20260929
MAX_USER_TURNS = 10

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def read(path):
    return json.loads(Path(path).read_text())

def write(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n'); tmp.replace(path)

def public_instruction(task):
    text = (ROOT / task['source'] / 'tests/persona/original/instruction.md').read_text()
    return re.sub(r'<!--.*?-->', '', text, flags=re.S).strip()

def contract_facts(text):
    """Partition the entire public contract without author-model omissions."""
    paragraphs = re.split(r'\n\s*\n', text.strip())
    groups = []; current = []
    target = max(500, len(text) // 4)
    for paragraph in paragraphs:
        if current and len('\n\n'.join(current)) + len(paragraph) > target:
            groups.append('\n\n'.join(current)); current = []
        current.append(paragraph)
    if current: groups.append('\n\n'.join(current))
    return [{'id': f'F{i+1}', 'text': s} for i, s in enumerate(groups)]

def get_attr(persona, path):
    # Authors may use the exact input JSON root in their provenance path.
    path = path.removeprefix('assigned_persona.').removeprefix('persona.')
    value = persona
    for component in path.split('.'): value = value[component]
    return value

def validate_evolution(spec, task, persona):
    if not spec['applicable']: return
    requirements = spec['requirements']
    if not 1 <= len(requirements) <= 3: raise ValueError('Need one to three substantive requirements')
    ids = [r['id'] for r in requirements]
    if ids != [f'R{i+1}' for i in range(len(ids))]: raise ValueError('Requirement IDs/order')
    original = public_instruction(task)
    exports = tomllib.loads((ROOT / task['source'] / 'task.toml').read_text()).get('artifacts', [])
    original_paths = [Path(e if isinstance(e, str) else e['source']) for e in exports]
    # TB3 tasks also use public output directories such as /results. A narrow
    # helper/log beside an original export must not be rejected just for living
    # outside /app; the container/artifact adapter supports these paths already.
    public_roots = {Path('/app'), Path('/workspace')}
    public_roots.update(p.parent for p in original_paths if p.parent != Path('/'))
    def allowed_artifact(raw):
        path = Path(raw)
        if not path.is_absolute() or '..' in path.parts or path == Path('/'):
            return False
        if any(path == p or p in path.parents for p in original_paths):
            return True
        if path.parts[1] in {'tests', 'solution', 'logs', 'proc', 'sys', 'dev'}:
            return False
        return any(p in path.parents for p in public_roots)
    reachable = {}
    for i, req in enumerate(requirements):
        if not req['public_basis_quote'].strip() or req['public_basis_quote'] not in original:
            raise ValueError('Requirement has no literal public-task basis')
        if not req['persona_attributes']: raise ValueError('Missing actual persona attribution')
        behavioral = False
        for attr in req['persona_attributes']:
            path = attr['path'].removeprefix('assigned_persona.').removeprefix('persona.')
            is_behavior = path.startswith(('preference.', 'coding_specific.concrete.', 'scenario_specific.terminal.'))
            behavioral = behavioral or is_behavior
            if not is_behavior and not path.startswith(('professional_background.', 'coding_specific.abstract.')):
                raise ValueError('Identity/demographics cannot drive evolved requirements')
            if get_attr(persona, attr['path']) != attr['value']: raise ValueError('Invented persona attribute')
        if not behavioral: raise ValueError('Requirement lacks a behavioral preference basis')
        if req['trigger_event'] not in {'first_plan', 'clarification', 'work_checkpoint', 'validation_report', 'completion_attempt'}:
            raise ValueError('Unsupported event')
        if not 2 <= req['earliest_turn'] <= req['deadline_turn'] <= 8: raise ValueError('Unreachable disclosure window')
        if not set(req['depends_on']) <= set(ids[:i]): raise ValueError('Cyclic/unknown dependency')
        reachable[req['id']] = max([req['earliest_turn'], *[reachable[k]+1 for k in req['depends_on']]])
        if reachable[req['id']] > req['deadline_turn']: raise ValueError('Dependency window cannot be reached')
        if not req['artifact_paths'] or any(not allowed_artifact(p) for p in req['artifact_paths']):
            raise ValueError('Unsupported artifact path')
        if not req['disclosure'].strip() or len(req['disclosure']) > 4500: raise ValueError('Missing/oversized disclosure')
    if any(not allowed_artifact(p) or Path(p) in public_roots for p in spec['additional_artifacts']):
        raise ValueError('Additional artifact export outside public workspace')

def validate_proposal(spec, task, persona):
    validate_evolution(spec, task, persona)
    if not spec['applicable']: return
    import ast
    tree = ast.parse(spec['checker_python'])
    if not any(isinstance(x, ast.FunctionDef) and x.name == 'check' for x in tree.body): raise ValueError('Missing check()')
    if not spec['opening_brief'].strip() or len(spec['opening_brief']) > 1200: raise ValueError('Opening brief')

def due_requirement(requirements, disclosed, turn, event):
    for req in requirements:
        if req['id'] in disclosed or not set(req['depends_on']) <= set(disclosed): continue
        if turn < req['earliest_turn']: continue
        if req['trigger_event'] == event or turn >= req['deadline_turn']: return req
    return None

def visible_transcript(events):
    """No command text, stdout, stderr, verifier results, or private reasoning."""
    result = []
    for e in events:
        if e['kind'] in {'initial_instruction', 'simulator'}:
            result.append({'role': 'user', 'content': e['data'].get('content', e['data'].get('message', ''))})
        elif e['kind'] == 'agent_action' and e['data'].get('message'):
            result.append({'role': 'agent', 'content': e['data']['message']})
    return result

def opening_valid(message, brief):
    if not message.strip() or len(message) > 1800: return False
    if re.search(r'/(?:app|workspace|tests|solution)/|\bMandatory review requirement\b', message): return False
    if re.search(r'persona-\d+|coding_specific|trust_in_agent|\[DONE\]', message): return False
    return True
