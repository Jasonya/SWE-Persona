#!/usr/bin/env python3
"""Registered semantic-case fixture (the `semantic_cases` binding).

Small, hand-built log set whose every line exercises one clause of the ORIGINAL
task contract -- nothing new is invented here:

  * severity comes from the bracketed field, so a [WARNING] line that contains
    the word ERROR counts as WARNING (the trap the task's own generator plants);
  * [DEBUG] is not reported;
  * the windows are inclusive on both ends: ref-6 is in last_7_days, ref-7 is
    not; ref-29 is in last_30_days, ref-30 is not;
  * month_to_date starts on the 1st of the reference month.

Every file name follows `YYYY-MM-DD_<source>.log`, as the instruction says we
may assume.  The expected counts are NOT written here: the harness gets them
from its own oracle (fixtures/reference_impl.py), so an agent that reads this
generator still has to implement the semantics.

    python3 gen_semantic_logs.py --out /tmp/logs_semantic
"""

from __future__ import annotations

import argparse
import os

# reference date for this fixture is 2025-08-13
FILES = {
    # today
    "2025-08-13_auth.log": [
        "2025-08-13 01:02:03 [ERROR] Database connection lost",
        "2025-08-13 01:03:03 [ERROR] Service payment crashed",
        "2025-08-13 02:00:00 [WARNING] Second failed login attempt for user: bob. Next attempt will ERROR. Retrying...",
        "2025-08-13 02:05:00 [WARNING] ERROR ERROR ERROR in the message body only",
        "2025-08-13 03:00:00 [INFO] User login successful for user: alice",
        "2025-08-13 03:30:00 [INFO] Cache cleared successfully; downstream [ERROR] mentioned in a quoted payload",
        "2025-08-13 04:00:00 [DEBUG] Variable x has value: 42",
        "2025-08-13 05:00:00 [debug] lowercase severity is not a reported severity",
        "2025-08-13 06:00:00 ERROR without brackets is not a severity field",
    ],
    # ref-6 -> inside last_7_days
    "2025-08-07_db.log": [
        "2025-08-07 10:00:00 [ERROR] Deadlock detected in transaction ID 7",
        "2025-08-07 10:01:00 [ERROR] File write failed: data.csv",
        "2025-08-07 10:02:00 [ERROR] Email sending failed to recipient: user@gmail.com",
        "2025-08-07 11:00:00 [INFO] Database connection established",
    ],
    # ref-7 -> outside last_7_days, inside last_30_days and month_to_date
    "2025-08-06_db.log": [
        "2025-08-06 10:00:00 [WARNING] High memory usage detected",
        "2025-08-06 10:01:00 [WARNING] Disk space low: 12 remaining",
        "2025-08-06 10:02:00 [WARNING] Slow query detected: execution time 900ms",
        "2025-08-06 10:03:00 [WARNING] API response time exceeded threshold",
        "2025-08-06 10:04:00 [WARNING] Configuration file deprecated: config.yml",
    ],
    # first day of the reference month -> inside month_to_date
    "2025-08-01_app.log": [
        "2025-08-01 09:00:00 [WARNING] High memory usage detected",
        "2025-08-01 09:01:00 [WARNING] Disk space low: 3 remaining",
        "2025-08-01 09:02:00 [INFO] Service auth started",
    ],
    # last day of the previous month -> outside month_to_date, inside last_30_days
    "2025-07-31_app.log": [
        "2025-07-31 23:00:00 [INFO] Scheduled backup completed successfully",
        "2025-07-31 23:01:00 [INFO] File uploaded: report.pdf",
        "2025-07-31 23:02:00 [INFO] Service inventory started",
        "2025-07-31 23:03:00 [INFO] Database connection established",
        "2025-07-31 23:04:00 [INFO] Cache cleared successfully",
        "2025-07-31 23:05:00 [INFO] User login successful for user: eve",
    ],
    # ref-29 -> inside last_30_days
    "2025-07-15_api.log": [
        "2025-07-15 08:00:00 [INFO] Service notification started",
        "2025-07-15 08:01:00 [INFO] Cache cleared successfully",
    ],
    # ref-30 -> outside last_30_days, counted only in total
    "2025-07-14_api.log": [
        "2025-07-14 08:00:00 [ERROR] Unhandled exception: TimeoutError",
        "2025-07-14 08:01:00 [ERROR] Unhandled exception: TimeoutError",
        "2025-07-14 08:02:00 [ERROR] Unhandled exception: TimeoutError",
        "2025-07-14 08:03:00 [ERROR] Unhandled exception: TimeoutError",
        "2025-07-14 08:04:00 [ERROR] Unhandled exception: TimeoutError",
        "2025-07-14 08:05:00 [ERROR] Unhandled exception: TimeoutError",
        "2025-07-14 08:06:00 [ERROR] Unhandled exception: TimeoutError",
    ],
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/logs_semantic")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    for name, lines in FILES.items():
        with open(os.path.join(args.out, name), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    print(f"generated {len(FILES)} semantic-case files in {args.out}")


if __name__ == "__main__":
    main()
