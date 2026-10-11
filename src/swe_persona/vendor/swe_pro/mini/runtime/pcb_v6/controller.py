"""Requirement-loop state machine (TB3 v6 section 5).  No model calls happen here.

Order of an episode in the `persona` condition:

  u-1  frozen vague opening                      (instruction.md)
  u-2+ original facts: every native requirement is disclosed with its validated wording
       before any evolved requirement; the agent's questions are answered from the
       public contract only
  then one evolved requirement at a time; the next one is due only when every active
       requirement's latest online verdict is MET (a deadline cannot skip an unmet one)
  done only when every original fact and every requirement is disclosed, every
       requirement is MET on the final recheck, and the simulated user is satisfied

`persona_static` discloses all requirements in one message at the same point, and still
judges each one.  `vague_no_evolution` has no evolved requirements.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import CONDITIONS

MAX_MESSAGE_CHARS = 6000
FOLLOW_UP_ACTIONS = {"PARTIAL": "request_fix", "UNMET": "request_fix",
                     "UNOBSERVABLE": "request_evidence"}


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def contains(message: str, text: str) -> bool:
    return squash(text) in squash(message)


@dataclass
class NativeFact:
    fact_id: str
    wording: str
    disclosed_event: Optional[str] = None


@dataclass
class Requirement:
    requirement_id: str
    source_ref: str
    disclosure: str
    criteria: List[Dict[str, str]]
    depends_on: List[str] = field(default_factory=list)
    artifact_paths: List[str] = field(default_factory=list)
    status: str = "undisclosed"           # undisclosed | active
    disclosed_turn: Optional[int] = None
    disclosed_event: Optional[str] = None
    verdict: Optional[str] = None
    feedback: str = ""
    judged_through_turn: int = 0
    met_turn: Optional[int] = None
    history: List[Dict[str, Any]] = field(default_factory=list)

    def rubric(self) -> Dict[str, Any]:
        return {"requirement_id": self.requirement_id, "disclosure": self.disclosure,
                "criteria": self.criteria}

    def state(self) -> Dict[str, Any]:
        return {"requirement_id": self.requirement_id, "source_ref": self.source_ref,
                "status": self.status, "disclosed_turn": self.disclosed_turn,
                "disclosed_event": self.disclosed_event, "verdict": self.verdict,
                "met_turn": self.met_turn, "history": self.history}


class Controller:
    def __init__(self, condition: str, requirements: List[Requirement],
                 native_facts: List[NativeFact], hidden_markers: Optional[List[str]] = None) -> None:
        if condition not in CONDITIONS or condition == "single_turn":
            raise ValueError(f"the requirement loop does not run condition {condition!r}")
        self.condition = condition
        self.requirements = [] if condition == "vague_no_evolution" else list(requirements)
        self.native_facts = list(native_facts)
        self.hidden_markers = [marker for marker in (hidden_markers or []) if marker.strip()]
        self.transcript: List[Dict[str, Any]] = []
        self.events: List[Dict[str, Any]] = []
        self.natives_complete_turn: Optional[int] = None
        self.end_state: Optional[str] = None

    # ---- transcript -----------------------------------------------------------
    def event(self, kind: str, turn: int, **detail: Any) -> None:
        self.events.append({"id": f"ev-{len(self.events) + 1:04d}", "type": kind,
                            "turn": turn, **detail})

    def add_user_message(self, text: str, turn: int, source: str) -> str:
        event_id = f"u-{turn}"
        due = {item.requirement_id for item in self.due_requirements(turn)}
        self.transcript.append({"event_id": event_id, "role": "user", "turn": turn,
                                "text": text, "source": source})
        for fact in self.native_facts:
            if fact.disclosed_event is None and contains(text, fact.wording):
                fact.disclosed_event = event_id
        if self.natives_complete_turn is None and not self.natives_missing():
            self.natives_complete_turn = turn
            self.event("original_facts_disclosed", turn, message=event_id)
        for item in self.requirements:
            if item.status == "undisclosed" and item.requirement_id in due and contains(text, item.disclosure):
                item.status, item.disclosed_turn, item.disclosed_event = "active", turn, event_id
                self.event("requirement_disclosed", turn, requirement=item.requirement_id,
                           message=event_id)
        return event_id

    def add_agent_message(self, text: str, turn: int, status: str) -> str:
        event_id = f"a-{turn}"
        self.transcript.append({"event_id": event_id, "role": "agent", "turn": turn,
                                "text": text, "status": status})
        if status == "idle":
            # The agent kept issuing placeholder commands and was stopped without a reply.
            self.event("agent_idle_yield", turn, message=event_id)
        return event_id

    # ---- schedule -------------------------------------------------------------
    def natives_missing(self) -> List[NativeFact]:
        return [fact for fact in self.native_facts if fact.disclosed_event is None]

    def active(self) -> List[Requirement]:
        return [item for item in self.requirements if item.status == "active"]

    def undisclosed(self) -> List[Requirement]:
        return [item for item in self.requirements if item.status == "undisclosed"]

    def due_requirements(self, turn: int) -> List[Requirement]:
        """Requirements the user message for `turn` must disclose."""
        if not self.requirements or self.natives_missing():
            return []
        # Never in the same message that completes the original facts.
        if self.natives_complete_turn is None or turn <= self.natives_complete_turn:
            return []
        if self.condition == "persona_static":
            return self.undisclosed()
        if any(item.verdict != "MET" for item in self.active()):
            return []
        met = {item.requirement_id for item in self.active() if item.verdict == "MET"}
        for item in self.undisclosed():
            if all(dep in met for dep in item.depends_on):
                return [item]
            return []
        return []

    def needs_judgment(self, agent_turn: int) -> List[Requirement]:
        """Active, not yet MET, with an agent reply the last judgment has not seen."""
        return [item for item in self.active()
                if item.verdict != "MET" and agent_turn > item.judged_through_turn]

    def record_verdict(self, item: Requirement, result: Dict[str, Any], turn: int,
                       purpose: str) -> None:
        item.verdict = result["verdict"]
        item.feedback = result.get("feedback", "")
        item.judged_through_turn = turn
        if item.verdict == "MET" and item.met_turn is None:
            item.met_turn = turn
        if item.verdict != "MET":
            item.met_turn = None
        item.history.append({"turn": turn, "purpose": purpose, "verdict": item.verdict,
                             "sample_verdicts": result.get("sample_verdicts", [])})
        self.event("requirement_judged", turn, requirement=item.requirement_id,
                   verdict=item.verdict, purpose=purpose)

    def all_met(self) -> bool:
        return all(item.status == "active" and item.verdict == "MET" for item in self.requirements)

    def done_allowed(self) -> bool:
        return not self.natives_missing() and self.all_met()

    # ---- simulator interface ----------------------------------------------------
    def allowed_actions(self, turn: int) -> List[str]:
        if self.due_requirements(turn):
            return ["disclose_requirement"]
        follow_ups = sorted({FOLLOW_UP_ACTIONS[item.verdict] for item in self.active()
                             if item.verdict in FOLLOW_UP_ACTIONS})
        actions = follow_ups or ["answer"]
        if self.done_allowed():
            actions.append("done")
        return actions

    def observation(self, turn: int, public_task: Dict[str, Any], budget: Dict[str, Any]) -> Dict[str, Any]:
        due = self.due_requirements(turn)
        missing = self.natives_missing()
        return {
            "turn": turn,
            "condition": self.condition,
            "original_task": public_task,
            "original_facts": [{"id": fact.fact_id, "wording": fact.wording,
                                "disclosed": fact.disclosed_event is not None}
                               for fact in self.native_facts],
            "original_facts_due": [{"id": fact.fact_id, "wording": fact.wording} for fact in missing],
            "requirements_to_disclose": [{"requirement_id": item.requirement_id,
                                          "disclosure": item.disclosure} for item in due],
            "active_requirements": [{"requirement_id": item.requirement_id,
                                     "disclosure": item.disclosure,
                                     "latest_verdict": item.verdict,
                                     "judge_feedback": item.feedback} for item in self.active()],
            "undisclosed_requirement_count": len(self.undisclosed()) - len(due),
            "allowed_actions": self.allowed_actions(turn),
            "done_allowed": self.done_allowed(),
            "conversation": [{"event_id": item["event_id"], "role": item["role"], "text": item["text"]}
                             for item in self.transcript],
            "budget": budget,
        }

    def validate_decision(self, decision: Dict[str, Any], observation: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        message = decision.get("user_message") or ""
        action = decision.get("action")
        if action not in observation["allowed_actions"]:
            errors.append(f"action must be one of {observation['allowed_actions']}")
        if not message.strip():
            errors.append("user_message must not be empty")
        due_text = sum(len(item["disclosure"]) for item in observation["requirements_to_disclose"])
        due_text += sum(len(item["wording"]) for item in observation["original_facts_due"])
        if len(message) > MAX_MESSAGE_CHARS + due_text:
            errors.append(f"user_message is too long ({len(message)} characters)")
        for fact in observation["original_facts_due"]:
            if not contains(message, fact["wording"]):
                errors.append(f"include original fact {fact['id']} with its exact wording")
        due_ids = {item["requirement_id"] for item in observation["requirements_to_disclose"]}
        for item in observation["requirements_to_disclose"]:
            if not contains(message, item["disclosure"]):
                errors.append(f"include requirement {item['requirement_id']} with its exact disclosure text")
        for item in self.undisclosed():
            if item.requirement_id in due_ids:
                continue
            leaked = [path for path in item.artifact_paths if path in message]
            if leaked or contains(message, item.disclosure):
                errors.append("do not mention requirements that are not due yet; remove "
                              + (", ".join(leaked) or "that requirement"))
        hidden = [marker for marker in self.hidden_markers if marker in message]
        if hidden:
            errors.append("user_message contains material the user cannot know; rewrite it")
        if action == "done" and not decision.get("satisfied"):
            errors.append("done requires satisfied=true")
        return errors

    def fallback_decision(self, observation: Dict[str, Any]) -> Dict[str, Any]:
        """A deterministic message used when the simulator cannot produce a valid one."""
        parts: List[str] = []
        if observation["original_facts_due"]:
            parts.append("To be explicit about what the original fix must do, all of these are required:")
            parts.extend(f"- {fact['wording']}" for fact in observation["original_facts_due"])
        if observation["requirements_to_disclose"]:
            parts.extend(item["disclosure"] for item in observation["requirements_to_disclose"])
            action = "disclose_requirement"
        else:
            pending = [item for item in observation["active_requirements"]
                       if item["latest_verdict"] in FOLLOW_UP_ACTIONS]
            parts.extend(item["judge_feedback"] for item in pending if item["judge_feedback"])
            action = observation["allowed_actions"][0]
            if not parts:
                parts.append("Please continue, and tell me what you have done and what I should see.")
        return {"action": action, "user_message": "\n\n".join(parts), "satisfied": False,
                "rationale": "deterministic fallback after rejected simulator decisions"}

    def state(self) -> Dict[str, Any]:
        return {
            "condition": self.condition,
            "end_state": self.end_state,
            "original_facts": [{"id": fact.fact_id, "disclosed_event": fact.disclosed_event}
                               for fact in self.native_facts],
            "original_facts_complete_turn": self.natives_complete_turn,
            "requirements": [item.state() for item in self.requirements],
            "all_met": self.all_met() if self.requirements else None,
        }
