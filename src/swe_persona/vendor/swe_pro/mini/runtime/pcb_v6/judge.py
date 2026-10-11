"""Online requirement judge (TB3 v6 sections 4-5).

The judge reads only the persona card, the original task's public constraints, the
current requirement's rubric and the user-visible conversation.  It never sees tool
output, code, hidden tests, the reference solution, checker results or requirements
that have not been disclosed yet -- the caller passes nothing else in.

Every reply is checked by program before it can move the episode:
  * requirement and criterion IDs match the rubric exactly;
  * each evidence item names an agent-visible message sent after the disclosure, and
    its quote occurs verbatim in that message;
  * a MET criterion carries at least one evidence item;
  * the overall verdict is the deterministic aggregate of the criterion statuses.
A reply that still fails after the repair rounds raises `JudgeProtocolError`, which the
episode records as a protocol error, never as UNMET.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import VERDICTS
from .llm import Llm, LlmSchemaError

# Lower is less favourable; used to break sample ties conservatively.
ORDER = {"UNMET": 0, "PARTIAL": 1, "UNOBSERVABLE": 2, "MET": 3}
MIN_QUOTE_CHARS = 8


class JudgeProtocolError(RuntimeError):
    def __init__(self, requirement_id: str, errors: List[str]) -> None:
        super().__init__(f"{requirement_id}: judge reply failed validation: {errors[:3]}")
        self.requirement_id = requirement_id
        self.errors = errors


def response_schema(requirement_id: Optional[str] = None,
                    criterion_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Strict-mode schema.  With IDs it is narrowed to one requirement."""
    rid: Dict[str, Any] = {"type": "string"}
    cid: Dict[str, Any] = {"type": "string"}
    if requirement_id:
        rid["enum"] = [requirement_id]
    if criterion_ids:
        cid["enum"] = list(criterion_ids)
    verdict = {"type": "string", "enum": list(VERDICTS)}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["requirement_id", "verdict", "criteria", "feedback", "reason"],
        "properties": {
            "requirement_id": rid,
            "verdict": verdict,
            "criteria": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["criterion_id", "status", "evidence", "reason"],
                    "properties": {
                        "criterion_id": cid,
                        "status": verdict,
                        "evidence": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["event_id", "quote"],
                                "properties": {"event_id": {"type": "string"},
                                               "quote": {"type": "string"}},
                            },
                        },
                        "reason": {"type": "string"},
                    },
                },
            },
            "feedback": {"type": "string"},
            "reason": {"type": "string"},
        },
    }


def aggregate(statuses: List[str]) -> str:
    """All MET -> MET; any UNMET -> UNMET; any PARTIAL -> PARTIAL; otherwise UNOBSERVABLE.

    A mix of MET and UNOBSERVABLE is UNOBSERVABLE: some obligation still lacks visible
    evidence, so the next step is to ask for it.
    """
    if statuses and all(status == "MET" for status in statuses):
        return "MET"
    if "UNMET" in statuses:
        return "UNMET"
    if "PARTIAL" in statuses:
        return "PARTIAL"
    return "UNOBSERVABLE"


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def quote_in(quote: str, message: str) -> bool:
    """Verbatim, tolerating only differences in whitespace runs."""
    quote = quote.strip()
    if len(quote.replace(" ", "")) < MIN_QUOTE_CHARS:
        return False
    return quote in message or _squash(quote) in _squash(message)


def eligible_messages(transcript: List[Dict[str, Any]], disclosed_turn: int) -> Dict[str, str]:
    """Agent messages answering the disclosure turn or later."""
    return {
        item["event_id"]: item["text"]
        for item in transcript
        if item["role"] == "agent" and item["turn"] >= disclosed_turn
    }


def validate_judgment(payload: Dict[str, Any], rubric: Dict[str, Any],
                      eligible: Dict[str, str]) -> List[str]:
    errors: List[str] = []
    rid = rubric["requirement_id"]
    if payload.get("requirement_id") != rid:
        errors.append(f"requirement_id must be {rid}")
    expected = [item["criterion_id"] for item in rubric["criteria"]]
    got = [item.get("criterion_id") for item in payload.get("criteria", [])]
    if sorted(got) != sorted(expected) or len(set(got)) != len(got):
        errors.append(f"criteria must contain each of {expected} exactly once, got {got}")
    for criterion in payload.get("criteria", []):
        cid = criterion.get("criterion_id")
        evidence = criterion.get("evidence") or []
        if criterion.get("status") == "MET" and not evidence:
            errors.append(f"{cid}: MET needs at least one evidence item quoting an eligible agent message")
        for item in evidence:
            event_id = item.get("event_id")
            if event_id not in eligible:
                errors.append(
                    f"{cid}: event_id {event_id!r} is not an agent message sent after this "
                    f"requirement was disclosed; eligible event_ids: {sorted(eligible)}")
            elif not quote_in(item.get("quote", ""), eligible[event_id]):
                errors.append(
                    f"{cid}: the quote for {event_id} does not occur verbatim in that message "
                    f"(copy at least {MIN_QUOTE_CHARS} characters exactly)")
    if not errors:
        want = aggregate([item["status"] for item in payload["criteria"]])
        if payload.get("verdict") != want:
            errors.append(f"verdict must be {want}, the aggregate of the criterion statuses")
    if not str(payload.get("feedback", "")).strip():
        errors.append("feedback must not be empty")
    return errors


@dataclass
class JudgeResult:
    requirement_id: str
    verdict: str
    criteria: List[Dict[str, Any]]
    feedback: str
    reason: str
    purpose: str
    eligible_event_ids: List[str]
    sample_verdicts: List[str] = field(default_factory=list)
    audit: List[Dict[str, Any]] = field(default_factory=list)

    def public(self) -> Dict[str, Any]:
        """What the simulator may see: the verdict and the user-facing feedback."""
        return {"requirement_id": self.requirement_id, "verdict": self.verdict,
                "feedback": self.feedback}

    def to_json(self) -> Dict[str, Any]:
        return {
            "requirement_id": self.requirement_id, "verdict": self.verdict,
            "criteria": self.criteria, "feedback": self.feedback, "reason": self.reason,
            "purpose": self.purpose, "eligible_event_ids": self.eligible_event_ids,
            "sample_verdicts": self.sample_verdicts,
        }


def render_messages(prompt: str, rubric: Dict[str, Any], persona_card: Dict[str, Any],
                    public_constraints: Dict[str, Any], transcript: List[Dict[str, Any]],
                    eligible: Dict[str, str],
                    rejected: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    conversation = [
        {"event_id": item["event_id"], "role": item["role"], "turn": item["turn"],
         "eligible_evidence": item["event_id"] in eligible, "text": item["text"]}
        for item in transcript
    ]
    body = {
        "persona": persona_card,
        "original_task_public_constraints": public_constraints,
        "requirement_rubric": rubric,
        "eligible_event_ids": sorted(eligible),
        "conversation": conversation,
    }
    user = ("Judge the requirement below.  Reply with one JSON object.\n\n"
            + json.dumps(body, ensure_ascii=False, indent=1))
    if rejected is not None:
        user += ("\n\n## Your previous reply was rejected\n"
                 + json.dumps(rejected["payload"], ensure_ascii=False)[:4000]
                 + "\n\nFix every problem and keep the rest unchanged:\n- "
                 + "\n- ".join(rejected["errors"]))
    return [{"role": "system", "content": prompt}, {"role": "user", "content": user}]


class RequirementJudge:
    def __init__(self, llm: Llm, prompt: str, max_repairs: int = 2) -> None:
        self.llm = llm
        self.prompt = prompt
        self.max_repairs = max_repairs

    def _one(self, rubric, persona_card, public_constraints, transcript, eligible,
             audit: List[Dict[str, Any]]) -> Dict[str, Any]:
        schema = response_schema(rubric["requirement_id"],
                                 [item["criterion_id"] for item in rubric["criteria"]])
        rejected = None
        errors: List[str] = []
        for attempt in range(self.max_repairs + 1):
            messages = render_messages(self.prompt, rubric, persona_card, public_constraints,
                                       transcript, eligible, rejected)
            try:
                payload = self.llm.json(messages, schema, "requirement_judgment")
            except LlmSchemaError as exc:
                errors = exc.errors
                audit.append({"attempt": attempt, "errors": errors, "raw": exc.text[:2000]})
                rejected = {"payload": exc.text[:2000], "errors": errors}
                continue
            errors = validate_judgment(payload, rubric, eligible)
            audit.append({"attempt": attempt, "errors": errors, "payload": payload})
            if not errors:
                return payload
            rejected = {"payload": payload, "errors": errors}
        raise JudgeProtocolError(rubric["requirement_id"], errors)

    def judge(self, rubric: Dict[str, Any], persona_card: Dict[str, Any],
              public_constraints: Dict[str, Any], transcript: List[Dict[str, Any]],
              disclosed_turn: int, samples: int = 1, purpose: str = "online") -> JudgeResult:
        eligible = eligible_messages(transcript, disclosed_turn)
        audit: List[Dict[str, Any]] = []
        # LlmError (transport) propagates: the episode records it as an infrastructure error.
        payloads = [
            self._one(rubric, persona_card, public_constraints, transcript, eligible, audit)
            for _ in range(max(1, samples))
        ]
        verdicts = [payload["verdict"] for payload in payloads]
        chosen = _majority(verdicts)
        payload = next(item for item in payloads if item["verdict"] == chosen)
        return JudgeResult(
            requirement_id=rubric["requirement_id"], verdict=chosen,
            criteria=payload["criteria"], feedback=payload["feedback"], reason=payload["reason"],
            purpose=purpose, eligible_event_ids=sorted(eligible), sample_verdicts=verdicts,
            audit=audit,
        )


def _majority(verdicts: List[str]) -> str:
    """Strict majority, otherwise the least favourable verdict among the samples."""
    for verdict in set(verdicts):
        if verdicts.count(verdict) * 2 > len(verdicts):
            return verdict
    return min(verdicts, key=lambda verdict: ORDER[verdict])
