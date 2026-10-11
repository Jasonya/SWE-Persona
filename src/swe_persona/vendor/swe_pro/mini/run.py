#!/usr/bin/env python3
"""Run one task under one condition (TB3 v6 section 7).

    python run.py --task TASK_ID --dry-run
    python run.py --task TASK_ID --condition persona --experience --output ./runs/single

Host environment: AZURE_OPENAI_BASE_URL + AZURE_OPENAI_API_KEY (or OPENAI_BASE_URL +
OPENAI_API_KEY); PCB_MODEL for the simulator and judges (default gpt-6-astra);
PCB_AGENT_MODEL for the coding agent (default gpt-5.6-luna); PCB_MINI_VENV for the frozen
mini-swe-agent 2.4.6 virtualenv.  All model calls and credentials stay on the host; the agent container has no network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "runtime"))

from pcb_v6 import CONDITIONS, PROTOCOL_VERSION, RUNTIME_VERSION  # noqa: E402
from pcb_v6.task import TaskBundle  # noqa: E402

DEFAULT_AGENT_MODEL = "gpt-5.6-luna"


def endpoint():
    for base, key in (("AZURE_OPENAI_BASE_URL", "AZURE_OPENAI_API_KEY"), ("OPENAI_BASE_URL", "OPENAI_API_KEY")):
        if os.environ.get(base) and os.environ.get(key):
            return os.environ[base], os.environ[key], base
    return None, None, None


def environment_image(task_root: Path) -> str:
    envdir = task_root / "environment"
    digest = hashlib.sha256()
    for path in sorted(p for p in envdir.rglob("*") if p.is_file()):
        digest.update(path.relative_to(envdir).as_posix().encode() + b"\0" + path.read_bytes())
    tag = f"pcb-v6-env-{task_root.name[:40]}:{digest.hexdigest()[:12]}"
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode != 0:
        subprocess.run(["docker", "build", "-t", tag, str(envdir)], check=True, capture_output=True, text=True,
                       timeout=3600)
    return tag


def plan(task: TaskBundle, condition: str, agent_model: str, judge_model: str) -> dict:
    base, key, source = endpoint()
    return {
        "protocol": PROTOCOL_VERSION, "task_id": task.task_id, "condition": condition,
        "agent_timeout_sec": task.timeout_sec, "budget": task.policy.get("budget"),
        "requirements": [item["requirement_id"] for item in task.requirements_spec],
        "original_facts": len(task.native_facts_spec),
        "models": {"agent": agent_model, "simulator": judge_model, "judge": judge_model},
        "endpoint_configured": bool(base and key), "endpoint_source": source,
        "docker": shutil.which("docker") is not None,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--task", required=True)
    parser.add_argument("--condition", choices=CONDITIONS, default="persona")
    parser.add_argument("--output", type=Path, default=Path("runs/single"))
    parser.add_argument("--trial-name", default=None)
    parser.add_argument("--dry-run", action="store_true", help="check configuration; start no model or container")
    parser.add_argument("--experience", action="store_true", help="also run the optional UX judge")
    parser.add_argument("--agent-model", default=os.environ.get("PCB_AGENT_MODEL", DEFAULT_AGENT_MODEL))
    parser.add_argument("--mini-venv", type=Path, default=Path(os.environ.get("PCB_MINI_VENV", sys.prefix)))
    parser.add_argument("--keep-image", action="store_true", help="keep the frozen final snapshot image")
    args = parser.parse_args(argv)

    task_root = ROOT / "tasks" / args.task
    task = TaskBundle.from_dir(task_root)
    judge_model = os.environ.get("PCB_MODEL", "gpt-6-astra")
    info = plan(task, args.condition, args.agent_model, judge_model)
    if args.dry_run:
        print(json.dumps(info, indent=2))
        return 0 if info["docker"] else 2
    base, key, _ = endpoint()
    if not (base and key):
        raise SystemExit("set AZURE_OPENAI_BASE_URL/AZURE_OPENAI_API_KEY or OPENAI_BASE_URL/OPENAI_API_KEY")

    from pcb_v6.agent_harbor import HarborMiniAgent
    from pcb_v6.episode import Episode
    from pcb_v6.evaluate import DockerEvaluator
    from pcb_v6.experience import run_experience
    from pcb_v6.judge import RequirementJudge
    from pcb_v6.llm import CallLog, Llm
    from pcb_v6.simulator import UserSimulator

    trial = args.trial_name or f"{args.condition}-{time.strftime('%Y%m%dT%H%M%S')}"
    out = (args.output / "trials" / args.task / trial).resolve()
    if (out / "persona-result.json").exists():
        raise SystemExit(f"{out} already has a result; choose another --trial-name")
    out.mkdir(parents=True, exist_ok=True)
    calls = CallLog(path=out / "agent-private" / "llm_calls.jsonl")
    image = environment_image(task_root)
    agent_base = os.environ.get("PCB_AGENT_BASE_URL", base)
    agent_key = os.environ.get("PCB_AGENT_API_KEY", key)
    (out / "execution-config.json").write_text(json.dumps({
        "protocol": PROTOCOL_VERSION, "runtime": RUNTIME_VERSION,
        "agent_model": args.agent_model, "agent_base_url": agent_base,
        "simulator_judge_model": judge_model, "simulator_judge_base_url": base,
        "python": sys.version, "environment_image": image,
        "release_manifest_sha256": hashlib.sha256((ROOT / "release-manifest.json").read_bytes()).hexdigest(),
    }, indent=2))
    agent = HarborMiniAgent(image=image, out_dir=out, model=args.agent_model, api_base=agent_base, api_key=agent_key,
                            agent_brief=task.agent_brief, mini_venv=args.mini_venv,
                            cpus=int(task.environment.get("cpus", 2)),
                            memory_mb=int(task.environment.get("memory_mb", 16384)))
    evaluator = DockerEvaluator(task_root, out / "agent-private" / "evaluation")
    interactive = args.condition != "single_turn"
    judge = RequirementJudge(Llm(judge_model, "requirement_judge", calls), task.judge_prompt) if interactive else None
    simulator = (UserSimulator(Llm(judge_model, "user_simulator", calls), task.simulator_prompt, task.persona_card)
                 if interactive else None)
    episode = Episode(task, args.condition, agent, evaluator, judge, simulator, out,
                      {"agent": args.agent_model, "simulator": judge_model, "judge": judge_model})
    try:
        result = episode.run()
    finally:
        if not args.keep_image:
            agent.discard_frozen()
    if args.experience and interactive:
        run_experience(task, out, Llm(judge_model, "experience_judge", calls))
    (out / "agent-private" / "llm_usage.json").write_text(json.dumps(calls.totals(), indent=2))
    print(json.dumps({k: result[k] for k in ("task_id", "condition", "benchmark_outcome", "outcome_reason",
                                             "original_reward")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
