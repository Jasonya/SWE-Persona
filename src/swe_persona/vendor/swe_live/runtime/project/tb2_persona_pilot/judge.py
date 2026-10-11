#!/usr/bin/env python3
"""LLM-as-judge over the INTERACTION only (plan section 5).

Scores the six observable interaction items 0/1/2 with evidence message ids.
Program behaviour, resources, permissions and correctness are never judged here
-- those come from checkers, and the metrics keep the two apart so a style score
can never offset a functional failure.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from llm import Llm

ITEMS = [
    ("answers_agent_questions", "Did the user answer the agent's actual open questions, from the public contract?"),
    ("discloses_only_activated", "Did the user disclose only requirements that were due and bound, one per turn, without hinting at future ones?"),
    ("insists_on_evidence", "Did the user point at verified gaps instead of accepting the agent's verbal claims?"),
    ("persona_style", "Did the user keep the persona's terse, resource-first style with no tutorials?"),
    ("leaves_room_to_act", "Did the user leave the agent a realistic chance to implement and verify each disclosure?"),
    ("ends_correctly", "Did the user end only once the evidence supported it, and otherwise keep the status unknown?"),
]

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["scores", "overall_note"],
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["item", "score", "evidence_message_ids", "reason"],
                "properties": {
                    "item": {"type": "string", "enum": [k for k, _ in ITEMS]},
                    "score": {"type": "integer", "enum": [0, 1, 2]},
                    "evidence_message_ids": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
            },
        },
        "overall_note": {"type": "string"},
    },
}

SYSTEM = """You are grading a SIMULATED USER's side of a coding session, not the coding agent, and not whether the code works.

Score each of the six items 0, 1 or 2:
  0 = clearly violated, 1 = partially met, 2 = met.
Cite the message ids you relied on (u-<turn> for user messages, a-<turn> for agent messages, chk-### for checker evidence, ev-### for events).

Never score program behaviour, resource usage, permissions, performance or security: those are decided by checkers, and are given to you only as context. Judge only what is observable in the interaction."""


def run_judge(judge_llm: Llm, transcript: List[Dict[str, Any]], ledger_events: List[Dict[str, Any]], checks: List[Dict[str, Any]]) -> Dict[str, Any]:
    payload = {
        "rubric": [{"item": k, "question": q} for k, q in ITEMS],
        "transcript": transcript,
        "ledger_events": ledger_events,
        "checker_results": checks,
    }
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)[:120000]},
    ]
    out = judge_llm.json_obj(messages, SCHEMA, schema_name="interaction_judgement", role="judge", max_output_tokens=3000)
    total = sum(s["score"] for s in out.get("scores", []))
    out["total"] = total
    out["max_total"] = 2 * len(ITEMS)
    out["judge_model"] = judge_llm.model
    return out
