#!/usr/bin/env python3
"""Backwards-compatible shim: `Gpt6` is now `Llm` pinned to the Azure GPT6 deployment.

New code should use `llm.Llm(model=...)` directly so each role can run on its own model
(see run_episode.py's --agent-model / --simulator-model / --judge-model).
"""

from __future__ import annotations

from typing import Optional

from llm import DEFAULT_MODEL, CallLog, Llm, load_local_env, parse_json, resolve_model  # noqa: F401

ENDPOINT = resolve_model(DEFAULT_MODEL).base_url
MODEL = DEFAULT_MODEL


class Gpt6(Llm):
    def __init__(self, call_log: Optional[CallLog] = None, model: str = MODEL) -> None:
        super().__init__(model=model, call_log=call_log)
