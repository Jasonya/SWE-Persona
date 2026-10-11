#!/usr/bin/env python3
"""Registered measurement tool -- the `measurement_protocol` binding.

    python3 /harness/measure.py [--json OUT] [--timeout SEC] -- <command> [args...]

Protocol (one cold-start run, whole process tree):
  * the command is started in its own session, so every descendant shares one
    process group even if it is re-parented;
  * every 10 ms the tool sums the current RSS of all processes in that group and
    keeps the maximum -- that sum is the tree's real memory footprint, so a
    single big Python process and a fan-out of greps are measured the same way;
  * `getrusage(RUSAGE_CHILDREN).ru_maxrss` is used as a cross-check for spikes
    shorter than the sampling interval.  It is read as a DELTA, because Linux
    lets that counter be inherited from the calling shell.

Reported `peak_rss_kib` is the max of the sampled tree footprint and the rusage
delta.  stdlib only, no network, runs inside the task image as shipped.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Tuple

PAGE_KIB = os.sysconf("SC_PAGE_SIZE") // 1024


def _proc_rows() -> List[Tuple[int, int, int]]:
    """(pid, pgrp, rss_kib) for every readable process."""
    rows: List[Tuple[int, int, int]] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
        except (OSError, UnicodeDecodeError):
            continue
        # comm may contain spaces/parens: split after the last ')'
        close = stat.rfind(")")
        if close < 0:
            continue
        fields = stat[close + 2 :].split()
        if len(fields) < 22:
            continue
        try:
            pgrp = int(fields[2])       # field 5 overall
            rss_pages = int(fields[21])  # field 24 overall
        except ValueError:
            continue
        rows.append((int(entry), pgrp, rss_pages * PAGE_KIB))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", dest="json_path", default=None)
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--interval", type=float, default=0.01)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd
    if not cmd:
        print("measure.py: no command given (use `-- <command>`)", file=sys.stderr)
        return 2

    rusage_before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    started = time.time()
    proc = subprocess.Popen(cmd, start_new_session=True)
    pgid = proc.pid  # start_new_session -> pgid == pid
    peak: Dict[str, int] = {"tree_kib": 0, "procs": 0}

    def sample() -> None:
        while proc.poll() is None:
            total = 0
            count = 0
            for pid, pgrp, rss_kib in _proc_rows():
                if pgrp == pgid:
                    total += rss_kib
                    count += 1
            if total > peak["tree_kib"]:
                peak["tree_kib"] = total
                peak["procs"] = count
            time.sleep(args.interval)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        rc = proc.wait(timeout=args.timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, 9)
        except OSError:
            proc.kill()
        rc, timed_out = 124, True
    wall = time.time() - started
    sampler.join(timeout=1.0)
    rusage_after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    rusage_delta = max(0, rusage_after - rusage_before)
    peak_kib = max(peak["tree_kib"], rusage_delta)
    report = {
        "command": cmd,
        "exit_code": rc,
        "timed_out": timed_out,
        "wall_sec": round(wall, 3),
        "peak_rss_kib": int(peak_kib),
        "peak_rss_mib": round(peak_kib / 1024.0, 1),
        "sampled_tree_peak_kib": peak["tree_kib"],
        "sampled_tree_procs_at_peak": peak["procs"],
        "rusage_children_delta_kib": rusage_delta,
        "sample_interval_sec": args.interval,
        "measured_by": "measure.py@2",
    }
    line = json.dumps(report, ensure_ascii=False)
    print("MEASUREMENT " + line)
    if args.json_path:
        Path(args.json_path).write_text(line + "\n", encoding="utf-8")
    return rc


if __name__ == "__main__":
    sys.exit(main())
