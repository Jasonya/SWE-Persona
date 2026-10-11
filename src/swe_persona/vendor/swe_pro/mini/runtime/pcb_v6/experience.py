"""Optional post-episode user-experience judge (TB3 v6 section 6, third result type).

It runs after the trial is complete and writes `persona-experience.json`; it never moves
the requirement loop and never changes `benchmark_outcome`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List

from .judge import quote_in
from .llm import Llm, LlmSchemaError


def _errors(payload: Dict[str, Any], rubric: Dict[str, Any], agent_messages: Dict[str, str]) -> List[str]:
    errors = []
    expected = sorted(item["id"] for item in rubric["items"])
    got = sorted(item["id"] for item in payload["items"])
    if got != expected:
        errors.append(f"items must be exactly {expected}")
    for item in payload["items"]:
        for evidence in item["evidence"]:
            text = agent_messages.get(evidence["event_id"])
            if text is None:
                errors.append(f"{item['id']}: {evidence['event_id']} is not an agent message")
            elif not quote_in(evidence["quote"], text):
                errors.append(f"{item['id']}: quote is not verbatim in {evidence['event_id']}")
        if item["status"] in ("satisfied", "violated") and not item["evidence"]:
            errors.append(f"{item['id']}: {item['status']} needs evidence")
    return errors


def run_experience(task, out: Path, llm: Llm, max_repairs: int = 2) -> Dict[str, Any]:
    closed = task.root / "tests" / "persona" / "closed_loop"
    rubric = json.loads((closed / "experience_rubric.json").read_text())
    schema = json.loads((closed / "experience_schema.json").read_text())
    prompt = (closed / "experience_prompt.md").read_text()
    episode = json.loads((out / "agent-private" / "episode.json").read_text())
    transcript = [{k: item[k] for k in ("event_id", "role", "text")} for item in episode["transcript"]]
    agent_messages = {item["event_id"]: item["text"] for item in transcript if item["role"] == "agent"}
    body = {"persona": task.persona_card, "experience_rubric": rubric, "conversation": transcript}
    messages = [{"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(body, ensure_ascii=False, indent=1)}]
    audit = []
    result: Dict[str, Any] = {"status": "error"}
    for attempt in range(max_repairs + 1):
        try:
            payload = llm.json(messages, schema, "experience_judgment")
        except LlmSchemaError as exc:
            audit.append({"attempt": attempt, "errors": exc.errors})
            continue
        errors = _errors(payload, rubric, agent_messages)
        audit.append({"attempt": attempt, "errors": errors, "payload": payload})
        if not errors:
            result = {"status": "scored", **payload}
            break
        messages = messages[:1] + [{"role": "user", "content": messages[1]["content"]
                                    + "\n\nFix these problems:\n- " + "\n- ".join(errors)}]
    shutil.copyfile(closed / "experience_rubric.json", out / "experience-rubric.json")
    (out / "ux-audit").mkdir(exist_ok=True)
    (out / "ux-audit" / "attempts.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2))
    (out / "persona-experience.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result
