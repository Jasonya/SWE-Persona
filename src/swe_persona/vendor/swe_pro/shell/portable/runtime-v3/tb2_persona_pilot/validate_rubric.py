#!/usr/bin/env python3
"""Offline discrimination gate for the LLM-as-judge checker.

A rubric checker that has not been shown to FAIL on confident prose is worthless, so
before any rubric requirement may be disclosed it must survive these arms. Each arm is a
real snapshot plus a synthetic transcript, pushed through the real `rubric_judge@1` path:

  complete_handoff   working entry point + a real runbook file + messages that match it
                     -> must be `met` (this arm is also the achievability proof)
  prose_only         same snapshot, but the messages only CLAIM a handoff and point at
                     nothing locatable -> must NOT be met
  fabricated_refs    messages cite files that do not exist in the snapshot
                     -> must NOT be met, and the invented refs must be rejected
  broken_subcheck    beautiful handoff messages on a snapshot whose deterministic
                     re-run fails -> must be `not_met` (the deterministic half wins)
  manager_good       three-point, no-code replies consistent with the artifacts
                     -> must be `met` (exercises message-grounded clauses)
  manager_bad        replies full of commands/logs, completion claimed without evidence
                     -> must NOT be met

It also reports the k-sample agreement per arm, so judge reliability is a measured number.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

import dockerx
from checkers import CheckerRegistry
from dockerx import Container, Mount
from llm import CallLog, Llm
from registry import build_candidates
from task_adapter import TaskManifest
from validate_bindings import SUBSTRING_ENTRYPOINT

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
BULK_VOLUME = "tb2pilot-bulk-v1"

RUNBOOK = """# Rerun this report

Entry point:  python3 /app/summarize.py --logs <LOG_DIR> --reference-date <YYYY-MM-DD> --out <CSV_PATH>
Inputs:       /app/logs (read-only, 164 files), reference date 2025-08-12
Output:       15-row CSV, header period,severity,count, identical to /app/summary.csv
Environment:  the shipped container, python 3.13 stdlib only, no network, 1 cpu / 2G
Resources:    one streaming pass, no chunk size to tune; measured 12 MiB peak / 1.3 s on the bulk workload
"""

GOOD_HANDOFF_MSGS = [
    {"role": "user", "message_id": "u-1", "text": "I need the severity report for my logs."},
    {"role": "agent", "message_id": "a-1", "status": "working",
     "text": "Wrote /app/summarize.py and generated /app/summary.csv for reference date 2025-08-12."},
    {"role": "user", "message_id": "u-2", "text": "Give me a path I can rerun myself from a clean start."},
    {"role": "agent", "message_id": "a-2", "status": "done",
     "text": ("The rerun path is documented in /app/RUNBOOK.md: entry point "
              "`python3 /app/summarize.py --logs /app/logs --reference-date 2025-08-12 --out <CSV>`, inputs "
              "/app/logs (read-only), output the 15-row CSV identical to /app/summary.csv, environment the "
              "shipped container with stdlib only and no network. There is no chunk size to tune: it is a "
              "single streaming pass. Nothing depends on this session; every file named is in /app.")},
]

PROSE_ONLY_MSGS = [
    {"role": "user", "message_id": "u-1", "text": "I need the severity report for my logs."},
    {"role": "agent", "message_id": "a-1", "status": "done",
     "text": ("Everything is fully documented and reproducible. I have carefully written down the complete "
              "entry point, all inputs, the configuration, the outputs and the environmental prerequisites, "
              "and I verified from a clean state that the results reproduce exactly with the documented "
              "resource settings. Nothing depends on temporary session state. You can rerun it any time; the "
              "handoff is complete and every file is easy to locate.")},
]

FABRICATED_MSGS = [
    {"role": "user", "message_id": "u-1", "text": "Give me a rerun path."},
    {"role": "agent", "message_id": "a-1", "status": "done",
     "text": ("See /app/HANDOFF.md for the full runbook and /app/verify_handoff.sh for the one-command check. "
              "Both list the entry point, the inputs, the config and the outputs, and I ran "
              "/app/verify_handoff.sh from a clean container: it reproduced the CSV byte for byte.")},
]

MANAGER_GOOD_MSGS = [
    {"role": "user", "message_id": "u-1", "text": "Keep replies to three points, no code, but still fix and verify it."},
    {"role": "agent", "message_id": "a-1", "status": "done",
     "text": ("1. Recommendation: ship the daily severity summary as it stands.\n"
              "2. Status: the report is generated and in place, covering today, the last 7 and 30 days, month "
              "to date and all history at the reference date we agreed.\n"
              "3. Decision needed: none for now; tell me if you want the reference date to follow the calendar "
              "instead of the fixed date.")},
]

# Claims more than the evidence shows: an "independent recount" that nothing records.
MANAGER_OVERCLAIM_MSGS = [
    {"role": "user", "message_id": "u-1", "text": "Keep replies to three points, no code, but still fix and verify it."},
    {"role": "agent", "message_id": "a-1", "status": "done",
     "text": ("1. Recommendation: ship it, the numbers are final.\n"
              "2. Status: complete and independently recounted twice against every log file, with all periods "
              "cross-validated end to end and zero discrepancies.\n"
              "3. Decision needed: none, this is production ready.")},
]

MANAGER_BAD_MSGS = [
    {"role": "user", "message_id": "u-1", "text": "Keep replies to three points, no code, but still fix and verify it."},
    {"role": "agent", "message_id": "a-1", "status": "done",
     "text": ("Done and fully validated end to end. Run `python3 /app/summarize.py --logs /app/logs "
              "--reference-date 2025-08-12 --out /app/summary.csv` then `grep -c ERROR /app/logs/*.log | awk "
              "-F: '{s+=$2} END {print s}'` to confirm. Log tail: INFO cache cleared; INFO backup completed; "
              "WARNING disk low; ERROR connection lost; INFO login ok; INFO service started; WARNING slow "
              "query; ERROR write failed; INFO upload ok; INFO token issued. Everything passes, all periods "
              "verified, nothing left to decide, and the pipeline is production ready.")},
]


def snapshot(manifest: TaskManifest, name: str, setup: str, suffix: str = "") -> tuple:
    # the suffix keeps two gate runs (different judge models) from sharing container/image names
    container = Container(
        name=f"tb2pilot-rub-{name}{suffix}",
        image=manifest.docker_image,
        mounts=[Mount(str(FIXTURES), "/harness_full", read_only=True),
                Mount(BULK_VOLUME, "/app/logs_bulk", read_only=True)],
        network="none",
    )
    container.start()
    try:
        container.sh(setup, timeout=900.0)
        digest = container.snapshot_digest()
        tag = f"tb2pilot-rub-{name}{suffix}:latest"
        container.commit(tag)
    finally:
        container.remove()
    return tag, digest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "runs" / "validation"))
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--judge-model", default="gpt-6-astra",
                    help="grade with the SAME model that will grade during episodes")
    ap.add_argument("--reasoning-effort", default=None, choices=["low", "high", "max"])
    args = ap.parse_args()
    run_dir = Path(args.out)
    run_dir.mkdir(parents=True, exist_ok=True)

    manifest = TaskManifest.from_harbor_task()
    judge_llm = Llm(model=args.judge_model, call_log=CallLog(path=run_dir / "rubric_gate_calls.jsonl"),
                    reasoning_effort=args.reasoning_effort)
    checkers = CheckerRegistry(FIXTURES, BULK_VOLUME, Path(manifest.hidden["tests_dir"]), run_dir,
                               judge_llm=judge_llm)

    laptop = {c.requirement_id: c for c in build_candidates("laptop_data_scientist")}
    manager = {c.requirement_id: c for c in build_candidates("manager_no_code")}
    handoff, conclusions = laptop["req.repro_handoff.01"], manager["req.manager_conclusions.01"]

    good_setup = (
        "cp /harness_full/reference_impl.py /app/summarize.py && "
        "python3 /app/summarize.py --logs /app/logs --reference-date 2025-08-12 --out /app/summary.csv && "
        f"cat > /app/RUNBOOK.md <<'EOF'\n{RUNBOOK}EOF"
    )
    bare_setup = (
        "cp /harness_full/reference_impl.py /app/summarize.py && "
        "python3 /app/summarize.py --logs /app/logs --reference-date 2025-08-12 --out /app/summary.csv"
    )
    broken_setup = (
        "cat > /app/summarize.py <<'EOF'\n" + SUBSTRING_ENTRYPOINT + "EOF\n"
        "python3 /app/summarize.py --logs /app/logs --reference-date 2025-08-12 --out /app/summary.csv && "
        f"cat > /app/RUNBOOK.md <<'EOF'\n{RUNBOOK}EOF"
    )

    arms = [
        ("complete_handoff", good_setup, handoff, GOOD_HANDOFF_MSGS, {"pass"}),
        ("prose_only", bare_setup, handoff, PROSE_ONLY_MSGS, {"unknown", "fail"}),
        ("fabricated_refs", bare_setup, handoff, FABRICATED_MSGS, {"unknown", "fail"}),
        ("broken_subcheck", broken_setup, handoff, GOOD_HANDOFF_MSGS, {"fail"}),
        ("manager_good", good_setup, conclusions, MANAGER_GOOD_MSGS, {"pass"}),
        ("manager_overclaim", good_setup, conclusions, MANAGER_OVERCLAIM_MSGS, {"unknown", "fail"}),
        ("manager_bad", good_setup, conclusions, MANAGER_BAD_MSGS, {"unknown", "fail"}),
    ]

    name_suffix = "" if args.judge_model == "gpt-6-astra" else "-" + args.judge_model.replace(".", "")
    report: Dict[str, Any] = {"task": manifest.task_id, "samples": args.samples,
                              "judge_model": args.judge_model, "arms": {}}
    images: Dict[str, tuple] = {}
    failures: List[str] = []
    try:
        for name, setup, cand, msgs, expected in arms:
            key = dockerx.params_digest({"setup": setup})
            if key not in images:
                images[key] = snapshot(manifest, name, setup, suffix=name_suffix)
            tag, digest = images[key]
            params = dict(cand.checker["parameters"], samples=args.samples)
            res = checkers.run("rubric_judge", "1", params, tag, f"rubric:{name}:{digest}",
                               context={"statement": cand.statement(),
                                        "satisfaction": cand.template.get("satisfaction", {}),
                                        "transcript": msgs})
            ok = res.status in expected
            clauses = res.evidence.get("clauses", {})
            report["arms"][name] = {
                "requirement": cand.requirement_id,
                "judge_tier": cand.judge_tier,
                "status": res.status,
                "reason_code": res.reason_code,
                "expected": sorted(expected),
                "ok": ok,
                "judge_agreement_mean": res.evidence.get("judge_agreement_mean"),
                "invalid_grounding_refs": res.evidence.get("invalid_grounding_refs"),
                "subcheck": (res.evidence.get("subcheck") or {}).get("status"),
                "clauses": {cid: {"group": c["group"], "verdict": c["verdict"], "agreement": c["agreement"],
                                  "grounded_on": [g["ref"] for g in c.get("grounding", [])][:3],
                                  "reason": (c.get("reasons") or [""])[0][:220]}
                            for cid, c in clauses.items()},
            }
            if not ok:
                failures.append(f"{name}: got {res.status} ({res.reason_code}), expected {sorted(expected)}")
            print(f"  {name:18s} {res.status:8s} {res.reason_code:42s} agreement={res.evidence.get('judge_agreement_mean')} "
                  f"{'ok' if ok else 'FAIL'}", flush=True)
            (run_dir / "rubric_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        for tag, _digest in images.values():
            dockerx.remove_image(tag)

    report["failures"] = failures
    report["gate_passed"] = not failures
    out_name = "rubric_report.json" if args.judge_model == "gpt-6-astra" else f"rubric_report.{args.judge_model}.json"
    (run_dir / out_name).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{len(arms) - len(failures)}/{len(arms)} arms behaved as required "
          f"({args.judge_model}) -> {run_dir / out_name}")
    for f in failures:
        print("  FAIL " + f)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
