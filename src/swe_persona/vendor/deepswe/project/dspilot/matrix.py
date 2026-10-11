"""Plan or execute a task/persona matrix with one baseline per task.

Control and pivot are task/persona conditions.  Baseline has no persona
treatment, so repeating it once for every persona would mislabel duplicate
task-only samples.  For each repeat this planner emits T baselines plus two
episodes for each of the P registered pairs: T + 2P.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from .specs import ROOT, compile_episode, pairing_plan
from .matrix_report import write_report
from .interaction_policy import VERSION as PROTOCOL_VERSION


BLOCK_ORDERS = (
    ("baseline", "control", "pivot"),
    ("control", "pivot", "baseline"),
    ("pivot", "baseline", "control"),
)


def selected_pairs(task_ids=None, persona_ids=None):
    task_ids = set(task_ids or [])
    persona_ids = set(persona_ids or [])
    pairs = []
    registered = [(item["task_id"], item["persona_id"])
                  for item in pairing_plan() if item["status"] == "runnable"]
    for task_id, persona_id in sorted(registered):
        if task_ids and task_id not in task_ids:
            continue
        if persona_ids and persona_id not in persona_ids:
            continue
        compile_episode(task_id, persona_id)
        pairs.append((task_id, persona_id))
    if not pairs:
        raise ValueError("No registered bindings match the requested filters")
    return pairs


def make_plan(prefix, repeats=1, task_ids=None, persona_ids=None):
    if not prefix or not prefix.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Invalid prefix")
    if repeats < 1:
        raise ValueError("Repeats must be positive")
    pairs = selected_pairs(task_ids, persona_ids)
    tasks = sorted({task_id for task_id, _ in pairs})
    # A baseline still needs a binding to provide the task environment.  The
    # persona is a launcher implementation detail and is not a baseline factor.
    representatives = {
        task_id: next(persona_id for candidate, persona_id in pairs if candidate == task_id)
        for task_id in tasks
    }
    episodes = []
    for repeat in range(1, repeats + 1):
        blocks = {
            "baseline": [
                {
                    "repeat": repeat,
                    "arm": "baseline",
                    "task_id": task_id,
                    "persona_id": representatives[task_id],
                    "persona_factor": None,
                    "run_id": f"{prefix}-r{repeat}-{task_id}-baseline",
                }
                for task_id in tasks
            ],
            "control": [
                {
                    "repeat": repeat,
                    "arm": "control",
                    "task_id": task_id,
                    "persona_id": persona_id,
                    "persona_factor": persona_id,
                    "run_id": f"{prefix}-r{repeat}-{task_id}-{persona_id}-control",
                }
                for task_id, persona_id in pairs
            ],
            "pivot": [
                {
                    "repeat": repeat,
                    "arm": "pivot",
                    "task_id": task_id,
                    "persona_id": persona_id,
                    "persona_factor": persona_id,
                    "run_id": f"{prefix}-r{repeat}-{task_id}-{persona_id}-pivot",
                }
                for task_id, persona_id in pairs
            ],
        }
        for block in BLOCK_ORDERS[(repeat - 1) % len(BLOCK_ORDERS)]:
            episodes.extend(blocks[block])
    expected = repeats * (len(tasks) + 2 * len(pairs))
    if len(episodes) != expected or len({item["run_id"] for item in episodes}) != expected:
        raise AssertionError("Matrix planner produced an invalid or duplicate episode plan")
    return {
        "protocol_version": PROTOCOL_VERSION,
        "prefix": prefix,
        "repeats": repeats,
        "task_count": len(tasks),
        "pair_count": len(pairs),
        "episode_count": len(episodes),
        "baseline_count": repeats * len(tasks),
        "control_count": repeats * len(pairs),
        "pivot_count": repeats * len(pairs),
        "pairs": [{"task_id": task, "persona_id": persona} for task, persona in pairs],
        "episodes": episodes,
    }


def launch_command(item):
    return [
        sys.executable,
        "-m",
        "dspilot.launch",
        "run",
        "--task-id",
        item["task_id"],
        "--persona-id",
        item["persona_id"],
        "--arm",
        item["arm"],
        "--run-id",
        item["run_id"],
    ]


def calibration_command(pair):
    return [
        sys.executable,
        "-m",
        "dspilot.launch",
        "calibrate",
        "--task-id",
        pair["task_id"],
        "--persona-id",
        pair["persona_id"],
    ]


def gate_is_current(pair):
    env = os.environ.copy()
    env.update(DS_TASK_ID=pair['task_id'], DS_PERSONA_ID=pair['persona_id'])
    result = subprocess.run([sys.executable, '-c',
                             'from dspilot.calibrate_v3 import require_gate; require_gate()'],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(
        description="Traverse registered bindings with a deduplicated task baseline"
    )
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--task-id", action="append", dest="task_ids")
    parser.add_argument("--persona-id", action="append", dest="persona_ids")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--calibrate", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Resume the exact preregistered plan; never overwrite existing episodes")
    args = parser.parse_args()
    plan = make_plan(args.prefix, args.repeats, args.task_ids, args.persona_ids)
    payload = plan | {"execute": args.execute, "calibrate": args.calibrate}
    print(json.dumps(payload, indent=2, ensure_ascii=False), flush=True)
    if not args.execute:
        return
    plan_dir = ROOT / "runs" / "matrices"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_path = plan_dir / f"{args.prefix}.json"
    if plan_path.exists():
        if not args.resume:
            raise SystemExit("Matrix prefix already exists; use --resume for the exact same plan or choose a new prefix")
        registered = json.loads(plan_path.read_text())
        if registered.get('protocol_version') != PROTOCOL_VERSION:
            raise SystemExit('Matrix uses a different interaction protocol; choose a new prefix and recalibrate')
        if any(registered.get(key) != payload.get(key) for key in
               ("prefix", "repeats", "task_count", "pair_count", "episode_count", "pairs", "episodes")):
            raise SystemExit("Existing matrix plan differs from the requested plan; refusing to reuse its prefix")
    elif args.resume:
        raise SystemExit("Cannot resume a matrix prefix that has no saved plan")
    else:
        plan_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    report_dir = write_report(ROOT, args.prefix)
    print(json.dumps({'matrix_report': str(report_dir/'report.md')}), flush=True)
    try:
        if args.calibrate:
            for pair in plan["pairs"]:
                if gate_is_current(pair):
                    print(json.dumps({'calibration':'current', **pair}),flush=True)
                else:
                    subprocess.run(calibration_command(pair), cwd=ROOT, check=True)
        for item in plan["episodes"]:
            existing = ROOT / 'runs' / item['run_id']
            if existing.exists():
                if not args.resume or not (existing/'result.json').exists():
                    raise SystemExit(f"Episode directory already exists without a resumable complete result: {existing}")
                saved = json.loads((existing/'result.json').read_text())
                if saved.get('protocol_version') != PROTOCOL_VERSION:
                    raise SystemExit(f"Cannot mix interaction protocol versions: {existing}")
                if any(saved.get(key) != item[key] for key in ('run_id','arm','task_id','persona_id')):
                    raise SystemExit(f"Existing episode result differs from the registered plan: {existing}")
                from .integrity import verify_receipt
                if saved.get('evaluation_valid') is not True or verify_receipt(
                        existing, saved, required=PROTOCOL_VERSION == 'matched-total-budget-judge-v3.3') not in ('passed', 'not_checked'):
                    raise SystemExit(f"Existing episode requires inspection before resume: {existing}")
                print(json.dumps({'episode':'already_recorded', 'run_id':item['run_id']}),flush=True)
                continue
            subprocess.run(launch_command(item), cwd=ROOT, check=True)
            write_report(ROOT, args.prefix)
    finally:
        # Retain missing/invalid rows even after calibration/episode failure or Ctrl-C.
        write_report(ROOT, args.prefix)


if __name__ == "__main__":
    main()
