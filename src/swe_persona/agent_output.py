"""Explicit JSON action instructions without changing agent execution policy."""
import json
from jsonschema import Draft202012Validator

POLICY = 'coding-agent-json-envelope-v1'


def action_envelope(schema, examples, fields):
    """Validate transport examples against the runtime's unchanged action schema."""
    validator = Draft202012Validator(schema)
    for example in examples:
        validator.validate(example)
    required = ', '.join(f'`{key}`' for key in schema['required'])
    lines = [
        '## Response envelope', '',
        'Return exactly one JSON object. After optional surrounding whitespace, '
        'the first character is `{` and the last character is `}`.', '',
        f'- Include every required field: {required}. Include unused strings as "" and required booleans as true or false, never strings.',
        '- Key order is unrestricted. Do not add fields outside the supplied schema.',
        '- Put no prose, Markdown fences, `<tool_call>` tags, or thinking tags outside the JSON object. A rejected response executes no action.',
        r'- Use JSON escaping inside strings: a newline is `\n`, a quote is `\"`, and a backslash is `\\`. Raw line breaks and escapes such as `\|` inside a string are invalid JSON.',
        '- Put the complete user-facing plan, question, progress report or handoff inside the designated message string, even when the user requests plain language.',
        '', '## Fields', '',
    ]
    for key in schema['properties']:
        lines.extend(['### '+key, '', fields[key], ''])
    lines.extend(['## Output example', '',
        'These examples illustrate JSON structure only. They do not define the task, '
        'establish facts, grant permissions, add requirements, report execution results '
        'or approve completion. Choose actions and wording from the current user '
        'instructions and actual host observations. Follow the existing authorization '
        'boundaries; an example does not require an additional approval step.', ''])
    lines.extend(json.dumps(example, ensure_ascii=False) for example in examples)
    return '\n'.join(lines)+'\n\n## Role\n\n'
