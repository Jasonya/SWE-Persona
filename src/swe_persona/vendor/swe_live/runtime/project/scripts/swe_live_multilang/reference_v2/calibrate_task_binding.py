"""Validate one task-bound persona checker. --run explicitly enables model calls."""
import bootstrap
import argparse
import json
from pathlib import Path

from calibration import calibrate_bound, gate_location
from execution_policy import BudgetClient, Deadline
from llm import CallLog, Llm
from support import ROOT, Manifest, read
from task_binding import candidate_for, load_binding


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--model", default="gpt-6-astra")
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    binding = load_binding(args.binding, Manifest(args.task), read(args.binding)["persona"])
    _, candidate = candidate_for(binding)
    location, cached = gate_location(ROOT, args.model, binding["persona"], binding)
    if cached:
        report = read(location / "report.json")
    elif not args.run:
        report = {"status": "configuration_checked", "gate_passed": False}
    else:
        # Gate work is outside scored sample clocks and has its own safety cap.
        log_path = location.parent / (location.name + "-calls.jsonl")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if candidate.template["satisfaction"]["judge"] == "trusted_harness":
            from types import SimpleNamespace
            client = SimpleNamespace(model=args.model)
        else:
            client = BudgetClient(Llm(model=args.model, call_log=CallLog(path=log_path)), 90)
            client.deadline = Deadline(900, "checker_calibration")
        report = calibrate_bound(client, location, args.binding)
    print(json.dumps({"persona": binding["persona"], "instance_id": binding["instance_id"],
                      "gate_passed": report["gate_passed"], "cached": cached, "path": str(location),
                      "agent_episode_validated": False}, indent=2))
    return 0 if not args.run or report["gate_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
