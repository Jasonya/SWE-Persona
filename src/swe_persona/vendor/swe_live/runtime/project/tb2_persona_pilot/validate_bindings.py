#!/usr/bin/env python3
"""Offline validation gate (plan section 3): before an episode may disclose a
bound requirement, the harness must show that

  1. the checker discriminates -- a deliberately bad implementation FAILS it;
  2. the requirement is achievable -- a legitimate implementation inside the
     real container limits PASSES it, and still passes the ORIGINAL tests;
  3. the requirement is compatible with the frozen contract -- the original
     Terminal-Bench tests pass in exactly the same snapshot.

Four synthetic arms are pushed through the real checker path (fresh container
from a committed snapshot).  Written as a standalone script so the validation
report is an artifact of the run, not a claim in a prompt.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import dockerx
from checkers import CheckerRegistry
from dockerx import Container, Mount
from registry import BULK_WORKLOAD, CAP_PEAK_RSS_KIB, CAP_WALL_SEC, SEMANTIC_REF_DATE
from task_adapter import TaskManifest

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
BULK_VOLUME = "tb2pilot-bulk-v1"

GOLD = "bash /opt/solution/solve.sh"

NAIVE_ENTRYPOINT = r'''
import argparse, os, re
from datetime import date, timedelta
ap = argparse.ArgumentParser()
ap.add_argument("--logs", default="/app/logs"); ap.add_argument("--reference-date", default="2025-08-12")
ap.add_argument("--out", default="/app/summary.csv")
a = ap.parse_args()
SEV = ("ERROR","WARNING","INFO"); PER = ("today","last_7_days","last_30_days","month_to_date","total")
rx = re.compile(r"\[(ERROR|WARNING|INFO|DEBUG)\]")
# deliberately naive: hold every line of every file in memory at once
store = {}
for name in sorted(os.listdir(a.logs)):
    with open(os.path.join(a.logs, name), encoding="utf-8", errors="replace") as fh:
        store[name] = fh.read().splitlines()
y, m, d = (int(x) for x in getattr(a, "reference_date").split("-")); ref = date(y, m, d)
win = {"today": (ref, ref), "last_7_days": (ref - timedelta(days=6), ref),
       "last_30_days": (ref - timedelta(days=29), ref), "month_to_date": (ref.replace(day=1), ref)}
tot = {p: {s: 0 for s in SEV} for p in PER}
for name, lines in store.items():
    fd = date(*(int(x) for x in name[:10].split("-")))
    for line in lines:
        hit = rx.search(line)
        if not hit or hit.group(1) not in SEV: continue
        s = hit.group(1); tot["total"][s] += 1
        for p,(lo,hi) in win.items():
            if lo <= fd <= hi: tot[p][s] += 1
with open(a.out, "w", encoding="utf-8") as fh:
    fh.write("period,severity,count\n")
    for p in PER:
        for s in SEV: fh.write(f"{p},{s},{tot[p][s]}\n")
'''

HARDCODED_ENTRYPOINT = r'''
import argparse, os, re
from datetime import date, timedelta
ap = argparse.ArgumentParser()
ap.add_argument("--logs", default="/app/logs"); ap.add_argument("--reference-date", default="2025-08-12")
ap.add_argument("--out", default="/app/summary.csv")
a = ap.parse_args()
SEV = ("ERROR","WARNING","INFO"); PER = ("today","last_7_days","last_30_days","month_to_date","total")
rx = re.compile(r"\[(ERROR|WARNING|INFO|DEBUG)\]")
ref = date(2025, 8, 12)   # the bug this probe is meant to catch: argument ignored
win = {"today": (ref, ref), "last_7_days": (ref - timedelta(days=6), ref),
       "last_30_days": (ref - timedelta(days=29), ref), "month_to_date": (ref.replace(day=1), ref)}
tot = {p: {s: 0 for s in SEV} for p in PER}
for name in sorted(os.listdir(a.logs)):
    fd = date(*(int(x) for x in name[:10].split("-")))
    with open(os.path.join(a.logs, name), encoding="utf-8", errors="replace") as fh:
        for line in fh:
            hit = rx.search(line)
            if not hit or hit.group(1) not in SEV: continue
            s = hit.group(1); tot["total"][s] += 1
            for p,(lo,hi) in win.items():
                if lo <= fd <= hi: tot[p][s] += 1
with open(a.out, "w", encoding="utf-8") as fh:
    fh.write("period,severity,count\n")
    for p in PER:
        for s in SEV: fh.write(f"{p},{s},{tot[p][s]}\n")
'''

SUBSTRING_ENTRYPOINT = r'''
import argparse, os
from datetime import date, timedelta
ap = argparse.ArgumentParser()
ap.add_argument("--logs", default="/app/logs"); ap.add_argument("--reference-date", default="2025-08-12")
ap.add_argument("--out", default="/app/summary.csv")
a = ap.parse_args()
SEV = ("ERROR","WARNING","INFO"); PER = ("today","last_7_days","last_30_days","month_to_date","total")
y, m, d = (int(x) for x in getattr(a, "reference_date").split("-")); ref = date(y, m, d)
win = {"today": (ref, ref), "last_7_days": (ref - timedelta(days=6), ref),
       "last_30_days": (ref - timedelta(days=29), ref), "month_to_date": (ref.replace(day=1), ref)}
tot = {p: {s: 0 for s in SEV} for p in PER}
for name in sorted(os.listdir(a.logs)):
    fd = date(*(int(x) for x in name[:10].split("-")))
    with open(os.path.join(a.logs, name), encoding="utf-8", errors="replace") as fh:
        for line in fh:
            for s in SEV:
                if s in line:          # substring match: the trap line is double counted
                    tot["total"][s] += 1
                    for p,(lo,hi) in win.items():
                        if lo <= fd <= hi: tot[p][s] += 1
with open(a.out, "w", encoding="utf-8") as fh:
    fh.write("period,severity,count\n")
    for p in PER:
        for s in SEV: fh.write(f"{p},{s},{tot[p][s]}\n")
'''


def arm_scripts(manifest: TaskManifest):
    """(arm name, setup script run inside the container, note)."""
    return [
        ("gold_solution", GOLD, "the task's own solve.sh: base task only, no evolved deliverable"),
        (
            "reference_streaming",
            "cp /harness_full/reference_impl.py /app/summarize.py && "
            "python3 /app/summarize.py --logs /app/logs --reference-date 2025-08-12 --out /app/summary.csv",
            "legitimate streaming implementation + the required entry point",
        ),
        (
            "naive_in_memory",
            "cat > /app/summarize.py <<'EOF'\n" + NAIVE_ENTRYPOINT + "EOF\n"
            "python3 /app/summarize.py --logs /app/logs --reference-date 2025-08-12 --out /app/summary.csv",
            "correct numbers but holds every line in memory: must break the resource budget",
        ),
        (
            "hardcoded_reference_date",
            "cat > /app/summarize.py <<'EOF'\n" + HARDCODED_ENTRYPOINT + "EOF\n"
            "python3 /app/summarize.py --logs /app/logs --reference-date 2025-08-12 --out /app/summary.csv",
            "ignores --reference-date: the event probe must fire and the semantic matrix must fail",
        ),
        (
            "substring_severity",
            "cat > /app/summarize.py <<'EOF'\n" + SUBSTRING_ENTRYPOINT + "EOF\n"
            "python3 /app/summarize.py --logs /app/logs --reference-date 2025-08-12 --out /app/summary.csv",
            "counts severities by substring: must fail the original tests and the semantic matrix",
        ),
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "runs" / "validation"))
    ap.add_argument("--arms", default="all")
    args = ap.parse_args()
    run_dir = Path(args.out)
    run_dir.mkdir(parents=True, exist_ok=True)

    manifest = TaskManifest.from_harbor_task()
    registry = CheckerRegistry(FIXTURES, BULK_VOLUME, Path(manifest.hidden["tests_dir"]), run_dir)
    wanted = None if args.arms == "all" else set(args.arms.split(","))
    report = {"task": manifest.task_id, "image": manifest.docker_image, "frozen_digest": manifest.frozen_digest(), "arms": {}}

    for name, setup, note in arm_scripts(manifest):
        if wanted and name not in wanted:
            continue
        print(f"\n=== arm {name}: {note}", flush=True)
        cname = f"tb2pilot-val-{name}"
        container = Container(
            name=cname,
            image=manifest.docker_image,
            mounts=[
                Mount(str(FIXTURES), "/harness_full", read_only=True),
                Mount(BULK_VOLUME, "/app/logs_bulk", read_only=True),
                Mount(str(Path(manifest.hidden["solution_path"]).parent), "/opt/solution", read_only=True),
            ],
            network="bridge",  # gold solve.sh apt-get installs; the agent arm runs with none
        )
        container.start()
        try:
            t0 = time.time()
            rc, out, err = container.sh(setup, timeout=1800.0)
            digest = container.snapshot_digest()
            tag = f"tb2pilot-val-{name}:latest"
            container.commit(tag)
        finally:
            container.remove()
        checks = {}
        try:
            for cid, params in [
                ("entrypoint_present", {}),
                ("deliverable_shape", {"deliverable": "/app/summary.csv"}),
                ("resource_budget", {
                    "workload": BULK_WORKLOAD, "reference_date": "2025-08-12",
                    "cap_peak_rss_kib": CAP_PEAK_RSS_KIB, "cap_wall_sec": CAP_WALL_SEC}),
                ("semantic_matrix", {"reference_date": SEMANTIC_REF_DATE}),
                ("probe_reference_date", {"reference_date": SEMANTIC_REF_DATE, "alt_reference_date": "2025-08-06"}),
                ("clean_room_repro", {"workload": "/app/logs", "reference_date": "2025-08-12"}),
                ("official_tests", {}),
            ]:
                res = registry.run(cid, "1", params, tag, f"{name}:{digest}")
                checks[cid] = {"status": res.status, "reason_code": res.reason_code,
                               "evidence": res.evidence if cid != "official_tests" else
                               {"reward": res.evidence.get("reward")}}
                print(f"  {cid:24s} {res.status:8s} {res.reason_code}", flush=True)
        finally:
            dockerx.remove_image(tag)
        report["arms"][name] = {
            "note": note, "setup_exit_code": rc, "setup_tail": (out or "")[-500:],
            "setup_stderr_tail": (err or "")[-500:], "snapshot_digest": digest,
            "seconds": round(time.time() - t0, 1), "checks": checks,
        }
        (run_dir / "validation_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {run_dir / 'validation_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
