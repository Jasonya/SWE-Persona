#!/usr/bin/env python3
"""Registered bulk-workload fixture (the `resource_limits` workload binding).

Same generator, message pools and severity mix as the task image's own
`log_generator_deterministic.py`, only parameterized so the harness can register
a larger *representative* workload than the 6 MB shipped with the image.  Same
per-file deterministic seeding, so the fixture is byte-identical on every build.

    python3 gen_bulk_logs.py --out /app/logs_bulk --days-back 240 \
        --min-lines 2000 --max-lines 3000
"""

from __future__ import annotations

import argparse
import os
import random
from datetime import datetime, timedelta

INFO = [
    "User login successful for user: {user}",
    "Scheduled backup completed successfully",
    "Service {service} started",
    "Database connection established",
    "File uploaded: {file}",
    "Cache cleared successfully",
]
WARNING = [
    "Slow query detected: execution time {n}ms",
    "Disk space low: {n} remaining",
    "High memory usage detected",
    "API response time exceeded threshold",
    "Configuration file deprecated: {file}",
    "Failed login attempt for user: {user}. Retrying...",
    # trap for substring matchers, verbatim from the task's own generator
    "Second failed login attempt for user: {user}. Next attempt will ERROR. Retrying...",
]
ERROR = [
    "Deadlock detected in transaction ID {n}",
    "Database connection lost",
    "File write failed: {file}",
    "Service {service} crashed",
    "Unhandled exception: {exception}",
    "Email sending failed to recipient: {email}",
]
DEBUG = [
    "Entering function {func} with arguments: {args}",
    "Variable x has value: {n}",
    "Session token generated for user: {user}",
    "Query executed: {query}",
    "Temporary file created at: {file}",
]
SEVERITIES = [("INFO", INFO, 0.6), ("WARNING", WARNING, 0.2), ("ERROR", ERROR, 0.15), ("DEBUG", DEBUG, 0.05)]
USERS = ["alice", "bob", "charlie", "diana", "eve"]
SERVICES = ["auth", "payment", "inventory", "notification"]
FILES = ["report.pdf", "data.csv", "config.yml", "backup.zip"]
EXCEPTIONS = ["NullPointerException", "TimeoutError", "FileNotFoundError"]
EMAILS = ["user@gmail.com", "admin@google.com", "support@amazon.com"]
FUNCTIONS = ["process_payment", "generate_report", "send_email"]
QUERIES = ["SELECT * FROM users", "UPDATE orders SET status='shipped' WHERE id=42"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/app/logs_bulk")
    ap.add_argument("--reference-date", default="2025-08-12")
    ap.add_argument("--days-back", type=int, default=240)
    ap.add_argument("--min-lines", type=int, default=2000)
    ap.add_argument("--max-lines", type=int, default=3000)
    ap.add_argument("--sources", default="db,app,auth,api")
    args = ap.parse_args()

    current = datetime.strptime(args.reference_date, "%Y-%m-%d")
    sources = args.sources.split(",")
    os.makedirs(args.out, exist_ok=True)
    files = lines = 0
    for day_offset in range(args.days_back, -1, -1):
        day = current - timedelta(days=day_offset)
        for i, source in enumerate(sources):
            random.seed(day.year * 10000 + day.month * 100 + day.day + i)
            path = os.path.join(args.out, f"{day.strftime('%Y-%m-%d')}_{source}.log")
            num_lines = random.randint(args.min_lines, args.max_lines)
            with open(path, "w", encoding="utf-8") as fh:
                for _ in range(num_lines):
                    severity, messages, _w = random.choices(SEVERITIES, weights=[s[2] for s in SEVERITIES])[0]
                    message = random.choice(messages).format(
                        user=random.choice(USERS),
                        service=random.choice(SERVICES),
                        file=random.choice(FILES),
                        n=random.randint(1, 5000),
                        exception=random.choice(EXCEPTIONS),
                        email=random.choice(EMAILS),
                        func=random.choice(FUNCTIONS),
                        args=random.randint(1, 10),
                        query=random.choice(QUERIES),
                    )
                    ts = day.strftime("%Y-%m-%d") + (
                        f" {random.randint(0, 23):02}:{random.randint(0, 59):02}:{random.randint(0, 59):02}"
                    )
                    fh.write(f"{ts} [{severity}] {message}\n")
            files += 1
            lines += num_lines
    print(f"generated {files} files / {lines} lines in {args.out}")


if __name__ == "__main__":
    main()
