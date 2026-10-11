#!/usr/bin/env python3
"""Run a fork-layout task through the preserved portable interaction runtime.

This is a new execution profile, not a replay of historical HPC episodes.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True, help="Task ID from inventory.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--adapter", default="mini", help="mini or an installed module:factory")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--agent-model", default="gpt-5.6-luna")
    parser.add_argument("--simulator-model", default="gpt-6-astra")
    parser.add_argument("--judge-model", default="gpt-6-astra")
    parser.add_argument("--model-config", type=Path)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    tasks = json.loads((ROOT / "inventory.json").read_text())["tasks"]
    selected = next((task for task in tasks if task["task_id"] == args.task), None)
    if selected is None:
        parser.error("Unknown task ID")
    if not args.dry_run and args.output is None:
        parser.error("--output is required for execution")
    command = [
        sys.executable, str(ROOT / "portable/run_case.py"),
        "--case", selected["case_id"], "--adapter", args.adapter,
        "--agent-model", args.agent_model,
        "--simulator-model", args.simulator_model,
        "--judge-model", args.judge_model,
        "--image-ref", selected["pinned_oci_reference"],
    ]
    if args.dry_run:
        command.append("--dry-run")
    if args.output is not None:
        command += ["--output", str(args.output.resolve())]
    if args.model_config is not None:
        command += ["--model-config", str(args.model_config.resolve())]
    if args.run_id is not None:
        command += ["--run-id", args.run_id]
    return subprocess.call(command)


if __name__ == "__main__":
    raise SystemExit(main())
