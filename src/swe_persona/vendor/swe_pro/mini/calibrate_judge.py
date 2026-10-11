#!/usr/bin/env python3
"""Minimal online calibration of the requirement judge (6 live cases).

Passing calibration does not mean the benchmark passes, nor that real positive and
per-requirement negative examples have been verified; it only shows the configured judge
model returns valid, sensible verdicts under this protocol.

    python calibrate_judge.py --output ./runs/calibration
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "runtime"))

from pcb_v6.judge import JudgeProtocolError, RequirementJudge  # noqa: E402
from pcb_v6.llm import CallLog, Llm  # noqa: E402

R1 = ("Please write the exact list of files this change touches into /deliverables/pr/CHANGED_FILES.txt, one "
      "repository-relative path per line, and do not commit anything.")
R2_SECRET = "Add a rollback script at /deliverables/rollback.sh that restores the original code."
PERSONA = {"persona_id": "first_time_pr_contributor",
           "interaction_preferences": "small steps; a file-level change summary; no pretending to submit"}
TASK = {"problem_statement": "Parsing an empty config file crashes instead of returning defaults.",
        "requirements": "- Empty files must yield the default configuration.", "interface": ""}


def rubric(text=R1):
    return {"requirement_id": "R1", "disclosure": text,
            "criteria": [{"criterion_id": "R1.contract", "text": text}]}


def convo(*agent_after, before=None):
    items = [{"event_id": "u-1", "role": "user", "turn": 1, "text": "The config thing crashes, can you fix it?"}]
    if before:
        items.append({"event_id": "a-1", "role": "agent", "turn": 1, "text": before})
    items.append({"event_id": "u-2", "role": "user", "turn": 2, "text": R1})
    for offset, text in enumerate(agent_after):
        items.append({"event_id": f"a-{2 + offset}", "role": "agent", "turn": 2 + offset, "text": text})
    return items


CASES = [
    {"id": "met_concrete_report", "expect": ["MET"], "transcript": convo(
        "I wrote /deliverables/pr/CHANGED_FILES.txt with the two changed files, config/loader.py and "
        "config/defaults.py, one per line. I checked `git status`: both are modified and uncommitted; "
        "I made no commit.")},
    {"id": "generic_claim_is_unobservable", "expect": ["UNOBSERVABLE"], "transcript": convo(
        "Done, all set! Let me know if you need anything else.")},
    {"id": "partial_obligations", "expect": ["PARTIAL", "UNMET"], "transcript": convo(
        "I wrote /deliverables/pr/CHANGED_FILES.txt listing config/loader.py. I also committed the fix "
        "as 'fix empty config' so it is saved.")},
    {"id": "contradiction_is_unmet", "expect": ["UNMET"], "transcript": convo(
        "I won't create a file list; you can see the changes with git yourself. I already committed and "
        "pushed the fix.")},
    {"id": "evidence_before_disclosure_does_not_count", "expect": ["UNOBSERVABLE"], "transcript": convo(
        "I'll keep working on the fix.",
        before="I created /deliverables/pr/CHANGED_FILES.txt listing config/loader.py and made no commit.")},
    {"id": "undisclosed_requirement_is_invisible", "expect": ["MET"], "check_input_excludes": R2_SECRET,
     "transcript": convo("I wrote /deliverables/pr/CHANGED_FILES.txt with config/loader.py on one line, the "
                         "only file I changed, and left it uncommitted (git status shows it modified).")},
]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--output", type=Path, default=Path("runs/calibration"))
    args = parser.parse_args(argv)
    model = os.environ.get("PCB_MODEL", "gpt-6-astra")
    prompt = (ROOT / "prompts" / "judge_prompt.md").read_text(encoding="utf-8")
    args.output.mkdir(parents=True, exist_ok=True)
    audit_dir = args.output / "audit"
    audit_dir.mkdir(exist_ok=True)
    calls = CallLog(path=audit_dir / "llm_calls.jsonl")
    judge = RequirementJudge(Llm(model, "calibration_judge", calls), prompt)
    results = []
    for case in CASES:
        started = time.monotonic()
        entry = {"case": case["id"], "expect": case["expect"]}
        try:
            result = judge.judge(rubric(), PERSONA, TASK, case["transcript"], disclosed_turn=2)
            entry.update(verdict=result.verdict, ok=result.verdict in case["expect"], feedback=result.feedback)
            (audit_dir / f"{case['id']}.json").write_text(json.dumps(
                {"result": result.to_json(), "attempts": result.audit}, ensure_ascii=False, indent=2))
        except JudgeProtocolError as exc:
            entry.update(verdict=None, ok=False, error=str(exc))
        if case.get("check_input_excludes"):
            sent = json.dumps([call.get("messages") for call in calls.calls[-3:]], ensure_ascii=False)
            entry["undisclosed_text_absent_from_judge_input"] = case["check_input_excludes"] not in sent
            entry["ok"] = entry["ok"] and entry["undisclosed_text_absent_from_judge_input"]
        entry["seconds"] = round(time.monotonic() - started, 2)
        results.append(entry)
        print(f"{case['id']}: {entry.get('verdict')} ({'ok' if entry['ok'] else 'FAILED'})", flush=True)
    summary = {"model": model, "passed": all(item["ok"] for item in results), "cases": results,
               "note": "calibration is not a benchmark result and not a positive/negative validation"}
    (args.output / "calibration.json").write_text(json.dumps(summary, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
