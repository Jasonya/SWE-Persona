#!/usr/bin/env python3
"""Harness-side reference implementation / oracle for log-summary-date-ranges.

Two uses, deliberately the same code:
  * oracle  -- `compute_rows()` is what every checker compares an agent's
               output against (never shown to the agent or the simulator);
  * reference agent -- run as a CLI it *is* a conforming
               `/app/summarize.py`, which is how the pilot proves offline that
               the bound evolved requirements are achievable inside the real
               resource budget (streaming, stdlib only).

Semantics (frozen from the original task instruction):
  * log files are `YYYY-MM-DD_<source>.log`, non-recursive, date from filename;
  * a line's severity is its FIRST bracketed severity token -- so
    "[WARNING] ... will ERROR. Retrying..." counts as WARNING, not ERROR;
  * windows are inclusive: today=[ref,ref], last_7_days=[ref-6,ref],
    last_30_days=[ref-29,ref], month_to_date=[ref.replace(day=1),ref],
    total=every file regardless of date;
  * output is `period,severity,count` with the 15 rows in fixed order.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

SEVERITIES = ("ERROR", "WARNING", "INFO")
PERIODS = ("today", "last_7_days", "last_30_days", "month_to_date", "total")
FILENAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_.+\.log$")
SEVERITY_RE = re.compile(r"\[(ERROR|WARNING|INFO|DEBUG|TRACE|CRITICAL|FATAL)\]")
HEADER = ["period", "severity", "count"]


def parse_date(text: str) -> date:
    y, m, d = (int(x) for x in text.split("-"))
    return date(y, m, d)


def file_counts(path: Path) -> Dict[str, int]:
    """Stream one log file; return counts of the three reported severities."""
    counts = {s: 0 for s in SEVERITIES}
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            hit = SEVERITY_RE.search(line)
            if hit and hit.group(1) in counts:
                counts[hit.group(1)] += 1
    return counts


def windows(reference: date) -> Dict[str, Tuple[date, date]]:
    return {
        "today": (reference, reference),
        "last_7_days": (reference - timedelta(days=6), reference),
        "last_30_days": (reference - timedelta(days=29), reference),
        "month_to_date": (reference.replace(day=1), reference),
    }


def compute_rows(log_dir: Path, reference: date) -> List[List[str]]:
    totals = {p: {s: 0 for s in SEVERITIES} for p in PERIODS}
    win = windows(reference)
    for entry in sorted(log_dir.iterdir()):
        if not entry.is_file():
            continue
        hit = FILENAME_RE.match(entry.name)
        if not hit:
            continue
        fday = date(int(hit.group(1)), int(hit.group(2)), int(hit.group(3)))
        counts = file_counts(entry)
        for sev, n in counts.items():
            totals["total"][sev] += n
            for period, (lo, hi) in win.items():
                if lo <= fday <= hi:
                    totals[period][sev] += n
    return [[p, s, str(totals[p][s])] for p in PERIODS for s in SEVERITIES]


def rows_to_csv(rows: List[List[str]]) -> str:
    return "\n".join([",".join(HEADER)] + [",".join(r) for r in rows]) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs", default="/app/logs")
    ap.add_argument("--reference-date", default="2025-08-12")
    ap.add_argument("--out", default="/app/summary.csv")
    args = ap.parse_args()
    rows = compute_rows(Path(args.logs), parse_date(args.reference_date))
    Path(args.out).write_text(rows_to_csv(rows), encoding="utf-8")
    print(f"wrote {args.out} ({len(rows)} rows) from {args.logs} ref={args.reference_date}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
