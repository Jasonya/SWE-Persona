"""Host-only role routing, shared by the client and portable delivery runners."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
ROLES = ('agent', 'simulator', 'judge')


def _provider(layer):
    if layer.get('API_PROVIDER'):
        return layer['API_PROVIDER']
    if layer.get('API_BASE_URL') or layer.get('API_KEY'):
        return 'azure'  # Historical API_* configuration used Azure api-key auth.
    azure = any(layer.get(k) for k in ('AZURE_OPENAI_BASE_URL', 'AZURE_OPENAI_ENDPOINT', 'AZURE_OPENAI_API_KEY'))
    openai = any(layer.get(k) for k in ('OPENAI_BASE_URL', 'OPENAI_API_KEY'))
    if azure and openai:
        raise ValueError('Both OpenAI and Azure are configured; set API_PROVIDER explicitly')
    return 'azure' if azure else 'openai' if openai else None


def _base_url(value, provider, *, legacy=False):
    parts = urlsplit(value.rstrip('/'))
    if (parts.scheme not in ('http', 'https') or not parts.netloc or parts.username
            or parts.password or parts.query or parts.fragment):
        raise ValueError('API base URL must be an HTTP(S) prefix without credentials, query or fragment')
    path = parts.path.rstrip('/')
    if path.endswith(('/responses', '/chat/completions')) or '/deployments/' in path:
        raise ValueError('Use an API base prefix, not an operation or deployment URL; Azure uses /openai/v1')
    if provider == 'azure' and not legacy and path in ('', '/openai'):
        path = '/openai/v1'
    return urlunsplit((parts.scheme, parts.netloc, path, '', ''))


def config(path: Path | None = None, *, environ=None) -> dict[str, str]:
    """Read optional .env as data, then apply host environment overrides.

    New provider credentials never fall back to another provider's legacy key.
    Explicit role credentials win over provider defaults. Returned role fields
    are effective values; callers must never serialize this credential mapping.
    """
    env = os.environ if environ is None else environ
    explicit = path or env.get('DS_PERSONA_ENV_FILE')
    env_path = Path(explicit or ROOT / '.env')
    file_values = {}
    if env_path.is_file():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            if key.startswith('export '):
                key = key[7:]
            file_values[key.strip()] = value.strip().strip('\"\'')
    elif explicit:
        raise ValueError('Explicit host configuration file is missing; credential values withheld')
    layers = (env, file_values)

    def pick(*keys):
        return next((layer[k] for layer in layers for k in keys if layer.get(k)), None)

    provider = pick('API_PROVIDER')
    if not provider:
        provider = next((p for layer in layers if (p := _provider(layer))), 'azure')
    if provider not in ('azure', 'openai'):
        raise ValueError('API_PROVIDER must be openai or azure')

    def credential(selected, field):
        generic_key = 'API_BASE_URL' if field == 'BASE_URL' else 'API_KEY'
        names = (('AZURE_OPENAI_BASE_URL', 'AZURE_OPENAI_ENDPOINT') if field == 'BASE_URL'
                 else ('AZURE_OPENAI_API_KEY',)) if selected == 'azure' else ('OPENAI_' + field,)
        for layer in layers:
            # Provider-specific names may coexist; explicit selection resolves them.
            generic_provider = layer.get('API_PROVIDER') or (
                'azure' if layer.get('API_BASE_URL') or layer.get('API_KEY') else None)
            if generic_provider == selected and layer.get(generic_key):
                return layer[generic_key]
            for name in names:
                if layer.get(name):
                    return layer[name]
        return 'https://api.openai.com/v1' if field == 'BASE_URL' and selected == 'openai' else None

    result = {}
    default_model = pick('AGENT_MODEL', 'PCB_MODEL')
    for role in ROLES:
        prefix = role.upper()
        selected = pick(prefix + '_API_PROVIDER') or provider
        if selected not in ('azure', 'openai'):
            raise ValueError(prefix + '_API_PROVIDER must be openai or azure')
        values = {
            'MODEL': pick(prefix + '_MODEL') or default_model,
            'API_PROVIDER': selected,
            'API_BASE_URL': pick(prefix + '_API_BASE_URL') or credential(selected, 'BASE_URL'),
            'API_KEY': pick(prefix + '_API_KEY') or credential(selected, 'API_KEY'),
            'API_MODE': pick(prefix + '_API_MODE') or pick('API_MODE') or 'responses',
            'REASONING_EFFORT': pick(prefix + '_REASONING_EFFORT') or pick('REASONING_EFFORT') or 'inherit',
            'HTTP_TIMEOUT_SECONDS': pick(prefix + '_HTTP_TIMEOUT_SECONDS') or '300',
            'REQUEST_OPTIONS_JSON': pick(prefix + '_REQUEST_OPTIONS_JSON') or '{}',
            'STRUCTURED_OUTPUT_MODE': pick(prefix + '_STRUCTURED_OUTPUT_MODE') or pick('STRUCTURED_OUTPUT_MODE') or 'json_schema',
            'STRUCTURED_OUTPUT_FALLBACK': pick(prefix + '_STRUCTURED_OUTPUT_FALLBACK') or pick('STRUCTURED_OUTPUT_FALLBACK') or 'true',
            'MAX_TOKENS_FIELD': pick(prefix + '_MAX_TOKENS_FIELD') or pick('MAX_TOKENS_FIELD') or 'max_completion_tokens',
        }
        for key in ('MODEL', 'API_BASE_URL', 'API_KEY'):
            if not values[key]:
                raise ValueError(f'Missing {prefix}_{key}; configure the role or provider default (values withheld)')
        if values['STRUCTURED_OUTPUT_MODE'] not in ('json_schema', 'json_object', 'prompt'):
            raise ValueError(prefix + '_STRUCTURED_OUTPUT_MODE must be json_schema, json_object, or prompt')
        if values['STRUCTURED_OUTPUT_FALLBACK'] not in ('true','false'):
            raise ValueError(prefix + '_STRUCTURED_OUTPUT_FALLBACK must be true or false')
        if values['API_MODE'] not in ('responses', 'chat_completions'):
            raise ValueError(prefix + '_API_MODE must be responses or chat_completions')
        if values['REASONING_EFFORT'] not in ('inherit', 'omit', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'):
            raise ValueError(prefix + '_REASONING_EFFORT is unsupported; use omit for models without reasoning support')
        if values['MAX_TOKENS_FIELD'] not in ('max_completion_tokens', 'max_tokens'):
            raise ValueError(prefix + '_MAX_TOKENS_FIELD must be max_completion_tokens or max_tokens')
        legacy_base = not pick(prefix + '_API_BASE_URL') and any(
            layer.get('API_BASE_URL') == values['API_BASE_URL'] and not layer.get('API_PROVIDER')
            for layer in layers)
        values['API_BASE_URL'] = _base_url(values['API_BASE_URL'], selected, legacy=legacy_base)
        result.update({prefix + '_' + k: v for k, v in values.items()})
    # Retain the original public mapping for legacy consumers.
    for key in ('API_BASE_URL', 'API_KEY', 'API_PROVIDER', 'API_MODE'):
        result[key] = result['AGENT_' + key]
    return result


def role_config(values, role):
    """Accept effective configuration and older injected Client.cfg mappings."""
    if role not in ROLES:
        raise ValueError('Unknown model role; expected agent, simulator or judge')
    prefix = role.upper()
    return {field: values.get(prefix + '_' + field) or values.get(field, default)
            for field, default in (
                ('MODEL', values.get('AGENT_MODEL')),
                ('API_PROVIDER', 'azure'), ('API_MODE', 'responses'),
                ('API_BASE_URL', None), ('API_KEY', None),
                ('HTTP_TIMEOUT_SECONDS', '300'), ('REQUEST_OPTIONS_JSON', '{}'), ('STRUCTURED_OUTPUT_MODE', 'json_schema'), ('STRUCTURED_OUTPUT_FALLBACK','true'), ('REASONING_EFFORT', 'inherit'), ('MAX_TOKENS_FIELD', 'max_completion_tokens'))}


def public_routes(values):
    """Reproducibility metadata without keys or raw host endpoints."""
    routes = {}
    for role in ROLES:
        route = role_config(values, role)
        routes[role] = {k.lower(): route[k] for k in
                       ('MODEL', 'API_PROVIDER', 'API_MODE', 'REASONING_EFFORT', 'MAX_TOKENS_FIELD', 'STRUCTURED_OUTPUT_MODE', 'STRUCTURED_OUTPUT_FALLBACK', 'REQUEST_OPTIONS_JSON', 'HTTP_TIMEOUT_SECONDS')}
        routes[role]['endpoint_sha256'] = hashlib.sha256(
            (route['API_BASE_URL'] or '').rstrip('/').encode()).hexdigest()
    return routes
