"""Reused TB2 LLM client, with one downstream correction to JSON extraction.

`tb2_persona_pilot.llm.parse_json` unwraps a Markdown fence found anywhere in the
reply.  That is wrong for this pilot: a rubric or judge response legitimately quotes
an agent message that contains an internal ```python ... ``` block, and the greedy
search extracts that inner code instead of the response, so a valid reply is reported
as invalid JSON and burns a schema-repair round.

The corrected version parses the reply as-is first, and only unwraps a fence that
encloses the ENTIRE reply.  Because TB2's `Llm.json_obj` resolves `parse_json` from
its own module globals, the corrected function is installed onto that module here:
the TB2 files are never modified, and the patch is confined to processes that import
this pilot.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tb2_persona_pilot.llm as _tb2_llm  # noqa: E402
from tb2_persona_pilot.llm import *  # noqa: E402,F401,F403

WHOLE_FENCE = re.compile(r"```(?:json)?[ \t]*\r?\n?(.*?)\r?\n?```", re.DOTALL | re.IGNORECASE)


def parse_json(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as raw_error:
        fence = WHOLE_FENCE.fullmatch(text)
        if fence:
            return json.loads(fence.group(1).strip())
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise raw_error


# TB2's repair loop calls `parse_json` through its own module globals, so the
# corrected implementation has to be installed there to take effect.
_tb2_llm.parse_json = parse_json


class Llm(_tb2_llm.Llm):
    def json_obj_once(self, messages, schema, schema_name="payload", role="unspecified",
                      max_output_tokens=4096):
        """One schema attempt; the caller owns bounded evaluator-side repair."""
        convo = messages if self.spec.enforces_schema else _tb2_llm._with_schema_instructions(
            messages, schema, schema_name)
        text, _ = self._call(convo, role=role, max_output_tokens=max_output_tokens,
                            attempts=self.request_attempts, schema=schema, schema_name=schema_name)
        try:
            payload = parse_json(text)
            errors = _tb2_llm.validate(payload, schema)
        except (ValueError, TypeError) as exc:
            errors = [type(exc).__name__]
        if errors:
            raise SchemaRepairFailed(self.model, schema_name, errors)
        return payload
