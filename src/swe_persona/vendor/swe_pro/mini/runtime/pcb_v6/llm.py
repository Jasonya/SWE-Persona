"""Strict-JSON calls through the OpenAI Responses API (Azure or compatible).

Credentials come only from the host environment (TB3 v6 section 7):

  AZURE_OPENAI_BASE_URL + AZURE_OPENAI_API_KEY, or OPENAI_BASE_URL + OPENAI_API_KEY.

Transport errors are retried here.  A reply that is not valid JSON for the schema raises
`LlmSchemaError`; semantic validation and repair belong to the caller, which knows what a
valid judgment or decision is.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

DEFAULT_MODEL = "gpt-6-astra"


class LlmError(RuntimeError):
    """The model could not be reached; an infrastructure error, never a verdict."""


class LlmSchemaError(LlmError):
    def __init__(self, errors: List[str], text: str) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors
        self.text = text


def schema_errors(payload: Any, schema: Dict[str, Any]) -> List[str]:
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(schema)
    errors = []
    for err in sorted(validator.iter_errors(payload), key=lambda e: list(e.path)):
        where = "/".join(str(p) for p in err.path) or "(root)"
        errors.append(f"{where}: {err.message}")
    return errors[:12]


@dataclass
class CallLog:
    path: Optional[Path] = None
    calls: List[Dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, record: Dict[str, Any]) -> None:
        with self._lock:
            self.calls.append(record)
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def totals(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "by_role": {}}
        for call in self.calls:
            if "error" in call:
                continue
            out["calls"] += 1
            out["input_tokens"] += call.get("input_tokens") or 0
            out["output_tokens"] += call.get("output_tokens") or 0
            role = out["by_role"].setdefault(call.get("role", "?"), {"calls": 0, "seconds": 0.0})
            role["calls"] += 1
            role["seconds"] = round(role["seconds"] + (call.get("latency_sec") or 0.0), 3)
        return out


def _client_from_env():
    from openai import OpenAI

    for base_env, key_env in (("AZURE_OPENAI_BASE_URL", "AZURE_OPENAI_API_KEY"),
                              ("OPENAI_BASE_URL", "OPENAI_API_KEY")):
        base, key = os.environ.get(base_env), os.environ.get(key_env)
        if base and key:
            return OpenAI(base_url=base, api_key=key, timeout=600.0, max_retries=0), base_env
    raise LlmError("set AZURE_OPENAI_BASE_URL/AZURE_OPENAI_API_KEY or OPENAI_BASE_URL/OPENAI_API_KEY")


class Llm:
    """One model used for one role.  `transport` is injectable for tests."""

    def __init__(self, model: Optional[str] = None, role: str = "unspecified",
                 call_log: Optional[CallLog] = None,
                 transport: Optional[Callable[..., Dict[str, Any]]] = None,
                 attempts: int = 4) -> None:
        self.model = model or os.environ.get("PCB_MODEL") or DEFAULT_MODEL
        self.role = role
        self.log = call_log if call_log is not None else CallLog()
        self.attempts = attempts
        self._transport = transport
        self._client = None
        self.endpoint_env: Optional[str] = None

    def _responses(self, messages, schema, name, max_output_tokens) -> Dict[str, Any]:
        if self._transport is not None:
            return self._transport(messages=messages, schema=schema, name=name,
                                   max_output_tokens=max_output_tokens)
        if self._client is None:
            self._client, self.endpoint_env = _client_from_env()
        response = self._client.responses.create(
            model=self.model, input=messages, max_output_tokens=max_output_tokens,
            text={"format": {"type": "json_schema", "name": name, "strict": True, "schema": schema}},
        )
        usage = getattr(response, "usage", None)
        return {"text": response.output_text or "",
                "input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None)}

    def json(self, messages: List[Dict[str, str]], schema: Dict[str, Any], name: str,
             max_output_tokens: int = 4096) -> Dict[str, Any]:
        last: Optional[Exception] = None
        for attempt in range(1, self.attempts + 1):
            started = time.monotonic()
            try:
                reply = self._responses(messages, schema, name, max_output_tokens)
            except Exception as exc:  # noqa: BLE001 - transport and rate errors are retried
                last = exc
                self.log.add({"ts": time.time(), "role": self.role, "model": self.model,
                              "schema": name, "attempt": attempt,
                              "latency_sec": round(time.monotonic() - started, 3),
                              "error": f"{type(exc).__name__}: {exc}"[:500]})
                if self._transport is None:
                    time.sleep(min(2 ** attempt, 20))
                continue
            text = reply.get("text") or ""
            self.log.add({"ts": time.time(), "role": self.role, "model": self.model,
                          "schema": name, "attempt": attempt,
                          "latency_sec": round(time.monotonic() - started, 3),
                          "input_tokens": reply.get("input_tokens"),
                          "output_tokens": reply.get("output_tokens"),
                          "messages": messages, "output_text": text})
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise LlmSchemaError([f"reply is not JSON: {exc}"], text) from exc
            errors = schema_errors(payload, schema)
            if errors:
                raise LlmSchemaError(errors, text)
            return payload
        raise LlmError(f"{self.model} call failed after {self.attempts} attempts: {last}")
