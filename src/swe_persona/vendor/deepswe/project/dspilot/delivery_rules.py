"""Export real source-derived prompt builders and response-schema templates.

Dynamic evidence IDs and document enums are supplied at runtime. Their assembly
program is included verbatim alongside literal prompt text, not replaced by an
invented static prompt or an example claimed as the actual episode input.
"""
import ast
import copy
import hashlib
import json
from pathlib import Path


SOURCES = {
    'judge': {'requirement_judge.py': ['instructions', 'rubric', 'response_schema', 'runtime_schema', 'literal_evidence', 'normalized_quote', 'match_quote', 'citation_matches', 'citation_errors', 'citation_source_lines', 'eligible_messages', 'validate', 'judge'],
              'checks.py': ['check'], 'checks_v3.py': ['GroundedJudge'],
              'full_persona.py': ['check_explanation']},
    'simulator': {'simulator_staged.py': ['propose', 'manager_update_guidance', 'message_authority', 'stale_check_review', 'render', 'review_user_message']},
}


def prompt_document(project, role):
    entries = []
    for filename, names in SOURCES[role].items():
        text = (Path(project) / 'dspilot' / filename).read_text()
        tree = ast.parse(text)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
                source = ast.get_source_segment(text, node)
                fragments = list(dict.fromkeys(n.value for n in ast.walk(node)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str) and len(n.value) >= 100))
                entries.append({'module': filename, 'builder': node.name,
                    'source_sha256': hashlib.sha256(text.encode()).hexdigest(),
                    'literal_prompt_fragments': fragments, 'assembly_program': source})
    return {'role': role, 'protocol': 'DeepSWE staged', 'builders': entries,
            'inputs': ('Online: bound requirement rubric and eligible post-disclosure agent-visible messages. Final checker (checks/checks_v3/full_persona builders only): snapshot/artifact/probe evidence and execution history. These are separate assessments.'
                       if role == 'judge' else 'Current controller receipt, disclosed documents/requirements, public checks, voice preferences, budget and visible history'),
            'interpretation': 'Literal text and complete assembly code are exact source exports. Conditional fragments are not all concatenated; execute the supplied builder for the current inputs.'}


def markdown(document):
    parts = [f'# DeepSWE {document["role"]} prompt templates', document['interpretation'], 'Runtime inputs: ' + document['inputs']]
    for entry in document['builders']:
        parts.extend([f'## {entry["module"]}: {entry["builder"]}',
                      'Literal instruction fragments (branch selection is defined below):',
                      *['```text\n' + t + '\n```' for t in entry['literal_prompt_fragments']],
                      'Complete executable assembly and validation:', '```python\n' + entry['assembly_program'] + '\n```'])
    return '\n\n'.join(parts) + '\n'


def schema_expression(project, filename, function, variable, context):
    # Evaluate only the trusted, shipped schema expression, never a user response.
    tree = ast.parse((Path(project) / 'dspilot' / filename).read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function)
    assignment = next(n for n in ast.walk(node) if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == variable for t in n.targets))
    return eval(compile(ast.Expression(assignment.value), filename, 'eval'),
                {'__builtins__': {'list': list, 'len': len, 'enumerate': enumerate}, **context})


def response_schema(project):
    # Load the exact shipped, dependency-free online schema builder.
    import runpy
    online = runpy.run_path(str(Path(project) / 'dspilot/requirement_judge.py'))['response_schema']()
    vote = schema_expression(project, 'checks.py', 'check', 'schema', {'evidence': {}})
    grounded = copy.deepcopy(vote)
    grounded['properties']['citations']['items'] = {'type': 'object', 'properties': {
        'reference_id': {'type': 'string', 'minLength': 1, 'x-runtime-enum': 'GroundedJudge evidence_registry keys'}},
        'required': ['reference_id'], 'additionalProperties': False}
    direct = copy.deepcopy(vote)
    prop = direct['properties']['citations']['items']['properties']['evidence_id']
    prop.pop('enum'); prop['x-runtime-enum'] = 'Supplied evidence dictionary keys'
    proposal = schema_expression(project, 'simulator_staged.py', 'propose', 'schema', {'allowed': ['continue', 'advance', 'finish', 'start_implementation', 'accept_plan'], 'keys': []})
    proposal['properties']['proposal']['x-runtime-enum'] = 'Allowed proposals in the current controller view'
    proposal['properties']['focus_ids'].pop('maxItems')
    proposal['properties']['focus_ids']['items']['x-runtime-enum'] = 'Currently disclosed requirement IDs'
    render = schema_expression(project, 'simulator_staged.py', 'render', 'schema',
        {'documents': {}, 'statuses': {}, 'context': {'stage': 'S1'}, 'outcome': 'continue'})
    for key in ('public_documents', 'acknowledged_checks'):
        render['properties'][key] = {'type': 'object', 'additionalProperties': {'type': 'string'},
            'x-runtime-properties': 'Exact current document IDs' if key == 'public_documents' else 'Exact current requirement IDs and statuses'}
    for key in ('stage', 'controller_outcome'):
        render['properties'][key] = {'type': 'string', 'x-runtime-enum': 'Singleton current ' + key}
    review = schema_expression(project, 'simulator_staged.py', 'render', 'review_schema', {})
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema',
            'title': 'DeepSWE actual response-schema templates',
            '$comment': 'Dynamic enum/property sets are narrowed by the unchanged runtime builders. x-runtime-* describes those constraints; this general template is not their replacement.',
            '$ref': '#/$defs/online_requirement_judgment',
            '$defs': {'online_requirement_judgment': online,
                      'grounded_judge_vote': grounded, 'direct_quote_vote': direct,
                      'simulator_proposal': proposal, 'simulator_render': render, 'simulator_message_review': review}}
