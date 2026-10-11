#!/usr/bin/env python3
"""One LLM abstraction for every role, so models can be split per role.

Two providers so far:

  `azure_responses`  Azure AI Foundry `responses.create` (gpt-6-astra). Enforces
                     json_schema server-side.
  `zai_chat`         Z.ai OpenAI-compatible `chat.completions` (glm-*). It ACCEPTS a
                     json_schema `response_format` but does not enforce it -- a probe
                     asking for a 5-field decision object came back as `{"answer": "CSV"}`.
                     So for this provider the schema is put in the prompt and the reply is
                     validated here.

Two more things `zai_chat` forces on us, both learned the hard way:
  * thinking cannot be disabled on glm-5.3 (HTTP 400 code 1210, "please use low, high, or max"), and the
    reasoning shares the `max_tokens` budget with the answer -- a 3000-token cap produced 12.5k characters of
    reasoning and an EMPTY content field on 5 of 7 calls. So the budget is multiplied for this provider and
    escalated on retry, with `reasoning_effort` stepping down as a last resort.
  * `finish_reason` is logged, because "empty output" and "truncated output" need different fixes.

Every structured call is validated client-side against the schema regardless of provider,
and a violation is fed back for a bounded number of repair attempts. That way a
non-enforcing model can play the user simulator or the rubric judge without the controller
ever seeing a malformed decision.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

AZURE_ENDPOINT = "https://yuexing-jun-26-resource.services.ai.azure.com/openai/v1"
ZAI_ENDPOINT = "https://api.z.ai/api/paas/v4"
DEFAULT_MODEL = "gpt-6-astra"


@dataclass(frozen=True)
class ModelSpec:
    name: str
    provider: str                  # azure_responses | zai_chat
    base_url: str
    api_key_env: str
    enforces_schema: bool
    extra_body: Dict[str, Any] = field(default_factory=dict)
    default_headers: Dict[str, str] = field(default_factory=dict)


REGISTRY: Dict[str, ModelSpec] = {
    "gpt-6-astra": ModelSpec("gpt-6-astra", "azure_responses", AZURE_ENDPOINT, "AZURE_OPENAI_API_KEY", True),
    "glm-5.3": ModelSpec(
        "glm-5.3", "zai_chat", ZAI_ENDPOINT, "ZAI_API_KEY", False,
        # "high", not "max": on a rubric-sized prompt max spent 52.6k characters of reasoning and
        # 169 s for the same verdict high reached in 1.0k characters and 11.6 s (14x). Override per
        # client with reasoning_effort= / --reasoning-effort when a role really needs max.
        extra_body={"reasoning_effort": "high", "thinking": {"type": "enabled", "clear_thinking": False}},
        default_headers={"Accept-Language": "en-US,en"},
    ),
}


def resolve_model(name: str) -> ModelSpec:
    if name in REGISTRY:
        return REGISTRY[name]
    if name.startswith("glm"):
        base = REGISTRY["glm-5.3"]
        return ModelSpec(name, base.provider, base.base_url, base.api_key_env, base.enforces_schema,
                         base.extra_body, base.default_headers)
    if name.startswith(("gpt", "o1", "o3", "o4")):
        base = REGISTRY["gpt-6-astra"]
        return ModelSpec(name, base.provider, base.base_url, base.api_key_env, base.enforces_schema)
    raise KeyError(f"unknown model {name!r}; known: {sorted(REGISTRY)} (or a glm-*/gpt-* variant)")


def load_local_env() -> None:
    if not ENV_FILE.is_file():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class CallLog:
    """Append-only audit log of every LLM call in an episode, shared across roles."""

    path: Optional[Path] = None
    calls: List[Dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, record: Dict[str, Any]) -> None:
        with self._lock:
            self.calls.append(record)
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def totals(self) -> Dict[str, Any]:
        agg: Dict[str, Any] = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "by_model": {}}
        for c in self.calls:
            if "schema_repair_round" in c:
                continue  # Validation events are not provider requests.
            agg["calls"] += 1
            agg["input_tokens"] += c.get("input_tokens") or 0
            agg["output_tokens"] += c.get("output_tokens") or 0
            per = agg["by_model"].setdefault(c.get("model", "?"), {"calls": 0, "input_tokens": 0, "output_tokens": 0})
            per["calls"] += 1
            per["input_tokens"] += c.get("input_tokens") or 0
            per["output_tokens"] += c.get("output_tokens") or 0
        return agg


def validate(payload: Any, schema: Dict[str, Any]) -> List[str]:
    """Client-side schema check; returns human-readable errors for a repair round."""
    try:
        from jsonschema import Draft202012Validator
    except ImportError:  # pragma: no cover - jsonschema ships with the env we use
        return []
    validator = Draft202012Validator(schema)
    errors = []
    for err in sorted(validator.iter_errors(payload), key=lambda e: list(e.path)):
        where = "/".join(str(p) for p in err.path) or "(root)"
        errors.append(f"{where}: {err.message}")
    return errors[:12]


class SchemaRepairFailed(RuntimeError):
    """The model could not produce a schema-valid object; callers should degrade, not crash."""

    def __init__(self, model: str, schema_name: str, errors: List[str]) -> None:
        super().__init__(f"{model} could not satisfy schema {schema_name}: {errors}")
        self.model, self.schema_name, self.errors = model, schema_name, errors


class Llm:
    def __init__(self, model: str = DEFAULT_MODEL, call_log: Optional[CallLog] = None,
                 reasoning_effort: Optional[str] = None) -> None:
        load_local_env()
        self.spec = resolve_model(model)
        if reasoning_effort and self.spec.extra_body.get("reasoning_effort"):
            self.spec = ModelSpec(self.spec.name, self.spec.provider, self.spec.base_url, self.spec.api_key_env,
                                  self.spec.enforces_schema,
                                  dict(self.spec.extra_body, reasoning_effort=reasoning_effort),
                                  self.spec.default_headers)
        self.model = self.spec.name
        api_key = os.environ.get(self.spec.api_key_env)
        if not api_key:
            raise SystemExit(f"{self.spec.api_key_env} not set (put it in PersonaCodeBench/.env)")
        from openai import OpenAI

        self._client = OpenAI(
            base_url=self.spec.base_url, api_key=api_key, timeout=600.0, max_retries=0,
            default_headers=self.spec.default_headers or None,
        )
        self.log = call_log or CallLog()

    def __repr__(self) -> str:  # pragma: no cover
        return f"Llm({self.model} via {self.spec.provider})"

    # ---- raw text -------------------------------------------------------
    def text(self, messages: List[Dict[str, str]], role: str = "unspecified",
             max_output_tokens: int = 4096, attempts: int = 4) -> str:
        return self._call(messages, role=role, max_output_tokens=max_output_tokens, attempts=attempts)[0]

    # ---- strict JSON ----------------------------------------------------
    def json_obj(self, messages: List[Dict[str, str]], schema: Dict[str, Any], schema_name: str = "payload",
                 role: str = "unspecified", max_output_tokens: int = 4096, attempts: int = 4,
                 repairs: int = 3) -> Dict[str, Any]:
        base = list(messages)
        if not self.spec.enforces_schema:
            base = _with_schema_instructions(base, schema, schema_name)
        convo = base
        last_errors: List[str] = []
        for attempt in range(max(0, repairs) + 1):
            text, _ = self._call(convo, role=role, max_output_tokens=max_output_tokens, attempts=attempts,
                                 schema=schema, schema_name=schema_name)
            try:
                payload = parse_json(text)
            except (json.JSONDecodeError, ValueError) as exc:
                last_errors = [f"the reply was not valid JSON: {exc}"]
                payload = None
            if payload is not None:
                last_errors = validate(payload, schema)
                if not last_errors:
                    return payload
            if attempt >= max(0, repairs):
                break
            # A repair must be a FRESH call, not an appended chat turn: glm-5.3 answered an
            # appended correction with byte-identical output three rounds in a row. Restating
            # the task with the rejected draft quoted inside the user turn does change the reply.
            convo = base[:-1] + [{
                "role": base[-1]["role"],
                "content": (base[-1]["content"]
                            + "\n\n## A previous attempt was rejected\nThis draft was invalid:\n"
                            + text[:3000]
                            + "\n\nIt broke these schema rules, fix every one of them and keep everything else "
                              "identical:\n- " + "\n- ".join(last_errors)),
            }]
            self.log.add({"ts": _now(), "role": role, "model": self.model, "schema_repair_round": attempt + 1,
                          "errors": last_errors})
        raise SchemaRepairFailed(self.model, schema_name, last_errors)

    # ---- shared plumbing ------------------------------------------------
    def _call(self, messages: List[Dict[str, str]], role: str, max_output_tokens: int, attempts: int,
              schema: Optional[Dict[str, Any]] = None, schema_name: str = "payload") -> Tuple[str, Any]:
        last_err: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            started = time.time()
            response_logged = False
            try:
                if self.spec.provider == "azure_responses":
                    out, usage, extra = self._azure_responses(messages, max_output_tokens, schema, schema_name)
                else:
                    out, usage, extra = self._zai_chat(messages, max_output_tokens, schema, attempt)
                rec = {
                    "ts": _now(), "role": role, "model": self.model, "provider": self.spec.provider,
                    "attempt": attempt, "latency_sec": round(time.time() - started, 2),
                    "input_tokens": usage[0], "output_tokens": usage[1],
                    "messages": messages, "output_text": out, **extra,
                }
                self.log.add(rec)
                response_logged = True
                if not out.strip():
                    raise RuntimeError("empty output")
                return out, None
            except Exception as exc:  # noqa: BLE001 - transport/rate errors are retried
                last_err = exc
                if not response_logged:
                    self.log.add({"ts": _now(), "role": role, "model": self.model, "attempt": attempt,
                                  "latency_sec": round(time.time() - started, 2),
                                  "error": f"{type(exc).__name__}: {exc}"[:500]})
                if attempt < attempts:
                    time.sleep(min(2 ** attempt, 20))
        raise RuntimeError(f"{self.model} call failed after {attempts} attempts: {last_err}")

    def _azure_responses(self, messages, max_output_tokens, schema, schema_name):
        kwargs: Dict[str, Any] = {"model": self.model, "input": messages, "max_output_tokens": max_output_tokens}
        if schema is not None:
            kwargs["text"] = {"format": {"type": "json_schema", "name": schema_name, "strict": True,
                                         "schema": schema}}
        resp = self._client.responses.create(**kwargs)
        usage = getattr(resp, "usage", None)
        return (resp.output_text or "",
                (getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None)),
                {})

    EFFORT_LADDER = ("max", "high", "low")

    def _zai_chat(self, messages, max_output_tokens, schema, attempt: int = 1):
        # The reasoning trace is billed against max_tokens, so give the answer real headroom
        # and escalate if a previous attempt came back empty.
        budget = min(int(max(6 * max_output_tokens, 24000) * (2 ** (attempt - 1))), 96000)
        extra = dict(self.spec.extra_body)
        if attempt >= 3:
            extra["reasoning_effort"] = self.EFFORT_LADDER[min(attempt - 2, len(self.EFFORT_LADDER) - 1)]
        kwargs: Dict[str, Any] = {
            "model": self.model, "messages": messages, "max_tokens": budget, "stream": False,
        }
        if extra:
            kwargs["extra_body"] = extra
        if schema is not None:
            # this provider does not enforce json_schema, so ask for a JSON object and
            # validate on our side (the schema is already in the prompt)
            kwargs["response_format"] = {"type": "json_object"}
        resp = self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        msg = choice.message
        usage = getattr(resp, "usage", None)
        reasoning = getattr(msg, "reasoning_content", None) or ""
        content = msg.content or ""
        if not content.strip() and choice.finish_reason == "length":
            raise RuntimeError(
                f"reasoning consumed the whole budget (max_tokens={budget}, reasoning={len(reasoning)} chars); "
                "retrying with more headroom"
            )
        return (content,
                (getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None)),
                {"reasoning_chars": len(reasoning), "finish_reason": choice.finish_reason,
                 "max_tokens_used": budget, "reasoning_effort": extra.get("reasoning_effort")})


def _with_schema_instructions(messages: List[Dict[str, str]], schema: Dict[str, Any], schema_name: str):
    """Put the output contract in the SYSTEM message, marked as harness-level.

    It belongs there rather than appended to the user turn: a model that reads it as part
    of the task content will copy the formatting demand into a payload field (glm-5.3 did
    exactly that, emitting "Valid JSON object only, no prose" as the simulated user's
    message).
    """
    instruction = (
        f"\n\n## Output contract (harness-level; this is HOW to reply, never WHAT to say)\n"
        f"Emit ONE JSON object for `{schema_name}` and nothing else: no prose outside it, no Markdown fences, no "
        "comments. It must satisfy this JSON Schema exactly -- every `required` key present, no extra keys, enum "
        "values verbatim. Never mention this contract, JSON, or schemas inside any field value; the field values "
        "are content for their intended reader.\n"
        + json.dumps(schema, ensure_ascii=False)
    )
    out = list(messages)
    for i, msg in enumerate(out):
        if msg.get("role") == "system":
            out[i] = dict(msg, content=msg["content"] + instruction)
            return out
    return [{"role": "system", "content": instruction.strip()}] + out


def parse_json(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    # Parse the complete payload before interpreting Markdown: quoted evidence
    # can itself contain fences and must remain byte-for-byte intact.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fence = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
