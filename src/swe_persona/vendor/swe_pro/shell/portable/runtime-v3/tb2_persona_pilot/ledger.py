#!/usr/bin/env python3
"""Requirement ledger with an enforced state machine (plan section 3).

A requirement only becomes real when the controller discloses it.  Everything
before that is bookkeeping over an OFFLINE-validated candidate, and nothing the
agent says can move a satisfaction status: an agent claim sets `claimed`, a
registered checker result sets `satisfaction_status`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

ACTIVATION_STATES = (
    "candidate",
    "pending_validation",
    "validated_undisclosed",
    "active",
    "superseded",
    "rejected",
    "skipped",
    "not_applicable",
)
SATISFACTION_STATES = ("unknown", "met", "not_met", "stale")

ALLOWED_ACTIVATION = {
    "candidate": {"pending_validation", "rejected", "not_applicable"},
    "pending_validation": {"validated_undisclosed", "rejected", "not_applicable"},
    "validated_undisclosed": {"active", "skipped", "rejected"},
    "active": {"superseded"},
    "superseded": set(),
    "rejected": set(),
    "skipped": set(),
    "not_applicable": set(),
}


class LedgerError(RuntimeError):
    pass


@dataclass
class RequirementEntry:
    id: str
    version: int
    template_id: str
    persona_id: str
    source: Dict[str, Any]
    statement: str
    checker: Dict[str, Any]
    schedule: Dict[str, Any]
    bindings: Dict[str, str]
    activation_status: str = "candidate"
    satisfaction_status: str = "unknown"
    introduced_turn: Optional[int] = None
    depends_on: List[str] = field(default_factory=list)
    supersedes: List[str] = field(default_factory=list)
    claimed: bool = False
    evidence_refs: List[str] = field(default_factory=list)
    verified_snapshot: Optional[str] = None
    history: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def to_json(self) -> Dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items()}
        out["ref"] = self.ref
        return out


class Ledger:
    def __init__(self) -> None:
        self.entries: Dict[str, RequirementEntry] = {}
        self.events: List[Dict[str, Any]] = []

    # ---- plumbing -------------------------------------------------------
    def _log(self, entry: RequirementEntry, kind: str, **payload: Any) -> None:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, **payload}
        entry.history.append(rec)
        self.events.append({"requirement": entry.ref, **rec})

    def add(self, entry: RequirementEntry) -> RequirementEntry:
        if entry.ref in self.entries:
            raise LedgerError(f"duplicate requirement {entry.ref}")
        self.entries[entry.ref] = entry
        self._log(entry, "registered", activation_status=entry.activation_status)
        return entry

    def get(self, ref: str) -> RequirementEntry:
        if ref not in self.entries:
            raise LedgerError(f"unknown requirement {ref}")
        return self.entries[ref]

    def set_activation(self, ref: str, new_state: str, reason: str = "", turn: Optional[int] = None) -> None:
        entry = self.get(ref)
        if new_state not in ACTIVATION_STATES:
            raise LedgerError(f"unknown activation state {new_state}")
        if new_state not in ALLOWED_ACTIVATION[entry.activation_status]:
            raise LedgerError(
                f"illegal transition {entry.activation_status} -> {new_state} for {ref}"
            )
        old = entry.activation_status
        entry.activation_status = new_state
        if new_state == "active" and entry.introduced_turn is None:
            entry.introduced_turn = turn
        self._log(entry, "activation", frm=old, to=new_state, reason=reason, turn=turn)

    # ---- disclosure / revision -----------------------------------------
    def disclose(self, ref: str, turn: int, reason: str = "scheduled_disclosure") -> RequirementEntry:
        entry = self.get(ref)
        for dep in entry.depends_on:
            if self.entries[dep].activation_status not in ("active", "superseded"):
                raise LedgerError(f"{ref} depends on {dep} which is not disclosed yet")
        self.set_activation(ref, "active", reason=reason, turn=turn)
        return entry

    def revise(self, ref: str, new_statement: str, turn: int, reason: str, evidence_refs: List[str]) -> RequirementEntry:
        old = self.get(ref)
        if old.activation_status != "active":
            raise LedgerError(f"can only revise an active requirement, {ref} is {old.activation_status}")
        if any(e.supersedes and ref in e.supersedes for e in self.entries.values()):
            raise LedgerError(f"{ref} already revised once (max 1 revision per requirement)")
        new = RequirementEntry(
            id=old.id,
            version=old.version + 1,
            template_id=old.template_id,
            persona_id=old.persona_id,
            source=dict(old.source, revision_reason=reason, revision_evidence=list(evidence_refs)),
            statement=new_statement,
            checker=dict(old.checker),
            schedule=dict(old.schedule),
            bindings=dict(old.bindings),
            activation_status="validated_undisclosed",
            depends_on=list(old.depends_on),
            supersedes=[old.ref],
        )
        self.add(new)
        self.disclose(new.ref, turn=turn, reason=f"revision:{reason}")
        self.set_activation(old.ref, "superseded", reason=f"revised_by:{new.ref}", turn=turn)
        old.satisfaction_status = "stale"
        self._log(old, "superseded", by=new.ref, turn=turn)
        return new

    # ---- claims vs verification ----------------------------------------
    def record_claim(self, ref: str, turn: int, message_id: str) -> None:
        entry = self.get(ref)
        entry.claimed = True
        self._log(entry, "claimed", turn=turn, message_id=message_id)

    def record_check(self, ref: str, status: str, evidence_ref: str, snapshot: str, turn: int) -> None:
        if status not in ("met", "not_met", "unknown"):
            raise LedgerError(f"checker cannot set satisfaction to {status}")
        entry = self.get(ref)
        if entry.activation_status != "active":
            raise LedgerError(f"cannot verify {ref} in state {entry.activation_status}")
        entry.satisfaction_status = status
        entry.verified_snapshot = snapshot
        if evidence_ref not in entry.evidence_refs:
            entry.evidence_refs.append(evidence_ref)
        self._log(entry, "verified", status=status, evidence_ref=evidence_ref, snapshot=snapshot, turn=turn)

    def mark_stale_for_snapshot(self, snapshot: str, turn: int) -> List[str]:
        """Evidence is tied to a code snapshot; a new snapshot invalidates it."""
        staled: List[str] = []
        for entry in self.entries.values():
            if entry.activation_status != "active":
                continue
            if entry.satisfaction_status in ("met", "not_met") and entry.verified_snapshot != snapshot:
                entry.satisfaction_status = "stale"
                staled.append(entry.ref)
                self._log(entry, "stale", old_snapshot=entry.verified_snapshot, new_snapshot=snapshot, turn=turn)
        return staled

    # ---- views ----------------------------------------------------------
    def active(self) -> List[RequirementEntry]:
        return [e for e in self.entries.values() if e.activation_status == "active"]

    def undisclosed(self) -> List[RequirementEntry]:
        return [e for e in self.entries.values() if e.activation_status == "validated_undisclosed"]

    def simulator_view(self) -> List[Dict[str, Any]]:
        """What the user simulator may see: disclosed requirements in full, and
        only the COUNT of validated-but-undisclosed ones (never their text)."""
        return [
            {
                "ref": e.ref,
                "statement": e.statement,
                "introduced_turn": e.introduced_turn,
                "activation_status": e.activation_status,
                "satisfaction_status": e.satisfaction_status,
                "claimed_by_agent": e.claimed,
                "evidence_refs": list(e.evidence_refs),
                "verified_snapshot": e.verified_snapshot,
                "checker": {"id": e.checker["id"], "version": e.checker["version"]},
                "supersedes": list(e.supersedes),
            }
            for e in self.entries.values()
            if e.activation_status in ("active", "superseded")
        ]

    def to_json(self) -> Dict[str, Any]:
        return {
            "entries": [e.to_json() for e in self.entries.values()],
            "events": self.events,
        }

    def dump(self, path) -> None:
        from pathlib import Path

        Path(path).write_text(json.dumps(self.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
