"""Explicit simulator transport instructions without changing decision policy."""
import json
from jsonschema import Draft202012Validator

POLICY = 'simulator-json-envelope-v1'


def response_envelope(schema, example, fields, *, notes=(), additional_examples=()):
    """Validate examples against the actual schema, including current enums.

    Examples explain serialization, not new permissions or acceptance criteria.
    Callers own current-state values and all semantic policy and validation.
    """
    examples = (example, *additional_examples)
    validator = Draft202012Validator(schema)
    for value in examples:
        validator.validate(value)
    required = schema['required']
    optional = [key for key in schema['properties'] if key not in required]
    names = ', '.join(f'`{key}`' for key in required)
    lines = [
        '## Response envelope', '',
        'Return exactly one JSON object. After optional surrounding whitespace, '
        'the first character is `{` and the last character is `}`.', '',
        f'- Required fields: {names}. Include every required field, including empty strings, empty arrays, and false values when applicable.',
        '- Key order is unrestricted. Do not add fields outside the supplied schema.',
        '- Put no prose, Markdown fences, `<tool_call>` tags, `[DONE]`, or `</think>` tags outside the JSON object.',
        r'- Use JSON escaping inside strings: a newline is `\n`, a quote is `\"`, and a backslash is `\\`. Raw line breaks inside a string are invalid JSON.',
        '- Put text addressed to the coding agent only in the designated user-message field. Private rationale stays in its designated audit field.',
    ]
    if optional:
        lines.append('- Conditional fields: '+', '.join(f'`{key}`' for key in optional)+'. Follow the conditions below.')
    lines.extend('- '+note for note in notes)
    lines.extend(['', '## Fields', ''])
    for key in schema['properties']:
        lines.extend(['### '+key, '', fields[key], ''])
    lines.extend(['## Output example', '',
        'These examples illustrate JSON structure only. They do not establish facts, '
        'grant permission, add obligations, report execution results, or approve completion. '
        'Choose the actual decision and wording from the current authoritative observation.', ''])
    lines.extend(json.dumps(value, ensure_ascii=False) for value in examples)
    return '\n'.join(lines)+'\n\n## Role\n\n'


def prepend_envelope(prompt, envelope):
    """Keep one current envelope, retaining the original role instructions."""
    stripped = prompt.lstrip()
    if stripped.startswith(envelope):
        return prompt
    if stripped.startswith('## Response envelope\n') and '\n\n## Role\n\n' in stripped:
        prompt = stripped.split('\n\n## Role\n\n', 1)[1]
    return envelope+prompt
