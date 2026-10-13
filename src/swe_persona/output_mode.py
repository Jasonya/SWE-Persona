"""Bounded provider-capability fallback, independent of JSON format repair."""
from copy import deepcopy
import json
import re
import threading

POLICY = 'structured-output-fallback-v1'
MODES = ('json_schema', 'json_object', 'prompt')
_cache = {}
_lock = threading.Lock()


def configured_mode(config):
    mode = config.get('structured_output_mode',
                      'json_schema' if config.get('strict_json_schema',True) else 'json_object')
    if mode not in MODES:
        raise ValueError('Invalid structured_output_mode')
    if type(config.get('structured_output_fallback',True)) is not bool:
        raise ValueError('structured_output_fallback must be a boolean')
    return mode


def unsupported_format(status, message, mode):
    """Only explicit capability rejection authorizes weakening provider format.

    Invalid schema definitions, auth, quotas, truncation, malformed model output
    and general HTTP errors must retain their own recovery/error handling.
    """
    if status not in (400,422) or mode == 'prompt':
        return False
    text = str(message).lower()
    if re.search(r'invalid (?:json )?schema|invalid_json_schema|'
                 r'schema (?:validation|is invalid|keyword|definition|property)|additionalproperties',text):
        return False
    names = (mode, 'response_format', 'text.format', 'text format', 'structured output')
    mentions = any(name in text for name in names) or bool(re.search(
        r'(?:unknown parameter|unrecognized request argument)[^\n]*[\'\"]text[\'\"]',text))
    return mentions and bool(re.search(
        r'not supported|unsupported|does not support|not available|'
        r'unrecognized|unknown (?:parameter|field)|not permitted|not allowed',text))


class OutputModeFallback:
    """Remember explicit rejection in this process for this route/configuration."""
    def __init__(self, endpoint, model, transport, mode, enabled=True):
        if mode not in MODES or type(enabled) is not bool:
            raise ValueError('Invalid structured output configuration')
        self.initial = mode
        self.enabled = enabled
        self.key = (endpoint.rstrip('/'),model,transport,mode)

    @property
    def mode(self):
        with _lock:
            return _cache.get(self.key,self.initial) if self.enabled else self.initial

    def reject(self, mode, status, message):
        if not self.enabled or not unsupported_format(status,message,mode):
            return None
        next_mode = MODES[MODES.index(mode)+1]
        with _lock:
            previous = _cache.get(self.key,self.initial)
            _cache[self.key] = MODES[max(MODES.index(previous),MODES.index(next_mode))]
        return next_mode


def mode_messages(messages, schema, mode):
    """Build each fallback from the original messages; never stack prompts."""
    messages = deepcopy(messages)
    if schema is None or mode == 'json_schema':
        return messages
    instruction = ('\nOutput transport: return ONLY one JSON object satisfying this schema, '
                   'with no prose or Markdown fences outside it. Natural-language role '
                   'content belongs INSIDE the appropriate JSON string fields. Schema: '
                   + json.dumps(schema))
    indices = [i for i,m in enumerate(messages)
               if m.get('role') == 'system' and isinstance(m.get('content'),str)]
    if indices:
        messages[indices[-1]]['content'] += instruction
    else:
        messages.insert(0,{'role':'system','content':instruction})
    return messages
