#!/usr/bin/env python3
"""Deterministic inventory and audit trail for native requirement disclosure."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from task_adapter import decode_public_field

BULLET = re.compile(
    r"(?m)(?P<prefix>^[ \t]*|(?<=\.)[ \t]+)(?P<marker>[-*+]|\d+[.)])[ \t]+"
)
SPACE = re.compile(r"\s+")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalized(value: str) -> str:
    return SPACE.sub(" ", value).strip()


@dataclass(frozen=True)
class NativeRequirement:
    id: str
    order: int
    wording: str
    source_text: str
    source_start: int
    source_end: int
    source_sha256: str
    wording_sha256: str


def native_requirement_inventory(raw_requirements: Any) -> List[NativeRequirement]:
    """Split top-level Markdown requirements while retaining exact source spans."""
    source = decode_public_field(raw_requirements).strip()
    if not source:
        return []
    matches = list(BULLET.finditer(source))
    spans: List[tuple[int, int, int]] = []
    if matches:
        # Introductory constraints are part of the contract, not disposable prose.
        if source[:matches[0].start()].strip():
            spans.append((0, 0, matches[0].start()))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
            spans.append((match.start("marker"), match.end(), end))
    else:
        spans.append((0, 0, len(source)))
    records: List[NativeRequirement] = []
    for order, (source_start, wording_start, source_end) in enumerate(spans, start=1):
        while source_end > source_start and source[source_end - 1].isspace():
            source_end -= 1
        source_text = source[source_start:source_end]
        wording = source[wording_start:source_end].strip()
        # An empty Markdown item must never match every possible user message.
        if not wording:
            wording = source_text
        records.append(
            NativeRequirement(
                id=f"native.req.{order:03d}",
                order=order,
                wording=wording,
                source_text=source_text,
                source_start=source_start,
                source_end=source_end,
                source_sha256=_sha(source_text),
                wording_sha256=_sha(wording),
            )
        )
    return records


@dataclass
class NativeDisclosureTracker:
    inventory: List[NativeRequirement]
    messages: List[Dict[str, Any]] = field(default_factory=list)
    disclosed: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def missing_ids(self) -> List[str]:
        return [item.id for item in self.inventory if item.id not in self.disclosed]

    def record_agent_visible_message(
        self, *, message_id: str, turn: int, text: str, source: str,
    ) -> List[str]:
        normalized_message = _normalized(text)
        found: List[str] = []
        for item in self.inventory:
            if item.id in self.disclosed:
                continue
            if _normalized(item.wording) in normalized_message:
                record = {
                    "requirement_id": item.id,
                    "type": "original",
                    "message_id": message_id,
                    "turn": turn,
                    "visible_to_agent": True,
                    "source": source,
                    "message_sha256": _sha(text),
                    "wording_sha256": item.wording_sha256,
                }
                self.disclosed[item.id] = record
                found.append(item.id)
        self.messages.append(
            {
                "message_id": message_id,
                "turn": turn,
                "visible_to_agent": True,
                "source": source,
                "message_sha256": _sha(text),
                "disclosure_refs": found,
            }
        )
        return found

    def validation_errors(self, text: str) -> List[str]:
        normalized_message = _normalized(text)
        return [
            f"native requirement {item.id} must appear with its validated wording in this agent-visible message"
            for item in self.inventory
            if item.id not in self.disclosed and _normalized(item.wording) not in normalized_message
        ]

    def fallback_message(self, prefix: str = "") -> str:
        missing = [item for item in self.inventory if item.id not in self.disclosed]
        lines = []
        if prefix.strip():
            lines.extend([prefix.strip(), ""])
        lines.append("To make the original acceptance criteria explicit, all of these are required:")
        lines.extend(f"- {item.wording}" for item in missing)
        return "\n".join(lines)

    def summary(self) -> Dict[str, Any]:
        required = [item.id for item in self.inventory]
        disclosed = [item.id for item in self.inventory if item.id in self.disclosed]
        return {
            "inventory_version": "native-requirements-v2",
            "required_count": len(required),
            "disclosed_count": len(disclosed),
            "missing_count": len(required) - len(disclosed),
            "required_ids": required,
            "disclosed_ids": disclosed,
            "missing_ids": self.missing_ids(),
            "inventory_sha256": _sha("\n".join(item.wording_sha256 for item in self.inventory)),
        }

    def to_json(self) -> Dict[str, Any]:
        return {
            **self.summary(),
            "inventory": [asdict(item) for item in self.inventory],
            "disclosures": [self.disclosed[item.id] for item in self.inventory if item.id in self.disclosed],
            "messages": self.messages,
        }


def audit_delivery(transcript, calls, tracker, required_evolved, max_turns,
                   mini_receipts=None, mini_trajectory_path=None):
    """Audit actual model inputs, independent of agent claims or rubric scores."""
    errors = []
    receipts = []
    delivered_refs = set()
    messages = {m["message_id"]: m for m in transcript
                if m.get("role") == "user" and m.get("visible_to_agent")}
    visible_messages = [m for m in transcript
                        if m.get("role") == "user" and m.get("visible_to_agent")]
    if len(messages) != len(visible_messages):
        errors.append("duplicate_user_message_id")
    delivered_message_ids = set()
    raw_receipts = list(mini_receipts or [])
    receipt_turns = [int(item["turn"]) for item in raw_receipts]
    receipt_indices = [item.get("message_index") for item in raw_receipts]
    if len(receipt_turns) != len(set(receipt_turns)):
        errors.append("duplicate_mini_receipt_turn")
    if len(receipt_indices) != len(set(receipt_indices)):
        errors.append("duplicate_mini_receipt_message_index")
    if (not all(isinstance(index, int) for index in receipt_indices)
            or receipt_indices != sorted(receipt_indices)):
        errors.append("nonmonotonic_mini_receipt_message_index")
    receipt_by_turn = {int(item["turn"]): item for item in raw_receipts}
    if receipt_by_turn and len(raw_receipts) != len(visible_messages):
        errors.append("mini_receipt_count_mismatch")
    trajectory_messages = []
    if receipt_by_turn:
        try:
            trajectory_messages = json.loads(Path(mini_trajectory_path).read_text())["messages"]
        except Exception as exc:
            errors.append(f"mini_trajectory_unreadable:{type(exc).__name__}")
    for message in messages.values():
        delivered = False
        receipt = receipt_by_turn.get(int(message["turn"]))
        if receipt and receipt.get("receipt_version") == "mini-trajectory-input-v1":
            index = receipt.get("message_index")
            if isinstance(index, int) and 0 <= index < len(trajectory_messages):
                actual = trajectory_messages[index]
                content = str(actual.get("content") or "")
                content_hash = _sha(content)
                exact_hash = _sha(message["text"])
                match = receipt.get("match")
                delivered = bool(
                    actual.get("role") == "user"
                    and receipt.get("message_sha256") == exact_hash
                    and receipt.get("trajectory_content_sha256") == content_hash
                    and ((match == "exact" and content == message["text"])
                         or (message["turn"] == 1 and match == "embedded_instance_template"
                             and message["text"] in content))
                )
        elif not receipt_by_turn:
            delivered = any(
                call.get("role") == f"agent.turn{message['turn']}"
                and not call.get("error")
                and any(m.get("role") == "user" and m.get("content") == message["text"]
                        for m in call.get("messages", []))
                for call in calls
            )
        refs = message.get("disclosure_refs", [])
        receipts.append({"message_id": message["message_id"], "turn": message["turn"],
                         "message_sha256": _sha(message["text"]),
                         "visible_to_agent": delivered, "disclosure_refs": refs})
        if not delivered:
            errors.append(f"message_not_in_agent_input:{message['message_id']}")
        else:
            delivered_refs.update(refs)
            delivered_message_ids.add(message["message_id"])
        if refs and message["turn"] > max_turns - 1:
            errors.append(f"disclosure_too_late:{message['message_id']}")
    for item in tracker.inventory:
        record = tracker.disclosed.get(item.id)
        message = messages.get((record or {}).get("message_id"))
        if (not record or not message
                or message["message_id"] not in delivered_message_ids
                or item.id not in message.get("disclosure_refs", [])):
            errors.append(f"missing_native_disclosure:{item.id}")
        elif (record["message_sha256"] != _sha(message["text"])
              or record.get("turn") != message["turn"]
              or record.get("visible_to_agent") is not True
              or record.get("wording_sha256") != item.wording_sha256
              or _normalized(item.wording) not in _normalized(message["text"])):
            errors.append(f"ungrounded_native_disclosure:{item.id}")
    for ref in sorted(set(required_evolved) - delivered_refs):
        errors.append(f"missing_evolved_disclosure:{ref}")
    return {"valid": not errors, "errors": errors, "messages": receipts,
            "note": "Delivery evidence does not establish agent compliance."}
