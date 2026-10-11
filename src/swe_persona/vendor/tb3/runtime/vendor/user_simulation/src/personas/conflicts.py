"""Evaluate declarative persona conflict rules."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


@dataclass(frozen=True)
class ConflictMatch:
    """The first rejection rule matched by a persona record."""

    severity: str
    rule_id: str
    description: str


class ConflictRuleSet:
    """A loaded conflict-rule file with short-circuit matching."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self.version = str(payload["rule_set_version"])
        self._rules = [
            *(('hard_reject', rule) for rule in payload.get('hard_reject', [])),
            *(('highly_unlikely_reject', rule) for rule in payload.get('highly_unlikely_reject', [])),
        ]

    @classmethod
    def load(cls, path: str | Path) -> "ConflictRuleSet":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(payload)

    def first_match(self, record: Dict[str, Any]) -> Optional[ConflictMatch]:
        for severity, rule in self._rules:
            if _matches(rule["when"], record):
                return ConflictMatch(severity, rule["id"], rule["description"])
        return None

    def all_matches(self, record: Dict[str, Any]) -> Iterable[ConflictMatch]:
        for severity, rule in self._rules:
            if _matches(rule["when"], record):
                yield ConflictMatch(severity, rule["id"], rule["description"])


def _get_path(record: Dict[str, Any], path: str) -> tuple[Any, bool]:
    value: Any = record
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None, False
        value = value[part]
    return value, True


def _matches(node: Dict[str, Any], record: Dict[str, Any]) -> bool:
    if "path" in node:
        actual, found = _get_path(record, node["path"])
        if not found:
            return False
        operator = node["op"]
        expected = node["value"]
        if operator == "eq":
            return actual == expected
        if operator == "in":
            return actual in expected
        if operator == "not_in":
            return actual not in expected
        if operator == "lt":
            return actual < expected
        if operator == "lte":
            return actual <= expected
        if operator == "gt":
            return actual > expected
        if operator == "gte":
            return actual >= expected
        raise ValueError(f"unsupported conflict-rule operator: {operator}")
    if "all" in node:
        return all(_matches(child, record) for child in node["all"])
    if "any" in node:
        return any(_matches(child, record) for child in node["any"])
    if "not" in node:
        return not _matches(node["not"], record)
    raise ValueError(f"invalid conflict-rule condition: {node}")