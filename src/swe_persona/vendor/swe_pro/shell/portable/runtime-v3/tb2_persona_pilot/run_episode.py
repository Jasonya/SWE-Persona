#!/usr/bin/env python3
"""Run one episode of the persona-interactive Terminal-Bench 2.0 case.

Arms:
  interactive             vague first request + persona overlay + evolved requirements
  interactive_no_evolution  same loop, zero evolved requirements (control)
  single_turn             the original instruction, one turn, no persona (baseline)

Every LLM role -- first-message adaptation, user simulator, agent under test,
interaction judge -- is GPT6 (`gpt-6-astra`).  Checkers, the ledger and the
outcome verifier contain no LLM at all.
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

import dockerx
from agent import TerminalAgent
from checkers import CheckerRegistry
from controller import COMMUNICATION_STYLE, ROLE_OVERLAYS, Controller
from dockerx import Container, Mount
from llm import CallLog, Llm, SchemaRepairFailed
from judge import run_judge
from ledger import Ledger
from registry import BULK_WORKLOAD, ENTRYPOINT, applicable_candidates, build_candidates, load_persona
from simulator import UserSimulator, render_system_prompt
from task_adapter import TaskManifest, build_environment_overlay
from verifier import compute_metrics, verify_outcome

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
BULK_VOLUME = "tb2pilot-bulk-v1"
AGENT_HARNESS_FILES = ("measure.py", "gen_bulk_logs.py", "gen_semantic_logs.py")

VAGUE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["vague_request", "kept", "omitted"],
    "properties": {
        "vague_request": {"type": "string"},
        "kept": {"type": "array", "items": {"type": "string"}},
        "omitted": {"type": "array", "items": {"type": "string"}},
    },
}
VAGUE_SYSTEM = """You turn a benchmark task statement into the FIRST message a real person would actually send a coding agent.

Keep: the goal, why it matters to them, and what "better" would look like.
Remove: output paths, file names, exact labels, schemas, row orders, the reference date, counting rules, and anything that reads like a written spec. The agent is supposed to have to ASK for those.
Style: first person, at most 70 words, no bullet points, no code, no numbered requirements.
Also list what you kept and what you deliberately left out."""
FORBIDDEN_IN_FIRST_MESSAGE = [
    "summary.csv", "period,severity,count", "last_7_days", "last_30_days", "month_to_date",
    "2025-08-12", "/app/summary", "15 rows", "summarize.py", "logs_bulk", "128 mib", "peak rss",
]


def make_first_message(llm: Llm, manifest: TaskManifest) -> Dict[str, Any]:
    rejections: List[str] = []
    for _attempt in range(4):
        prompt = "## Task statement\n" + manifest.instruction
        if rejections:
            prompt += "\n\n## Your previous draft leaked spec details\nRemove these and rewrite: " + ", ".join(rejections)
        out = llm.json_obj(
            [{"role": "system", "content": VAGUE_SYSTEM}, {"role": "user", "content": prompt}],
            VAGUE_SCHEMA, schema_name="vague_request", role="adapter.first_message", max_output_tokens=1200,
        )
        text = out["vague_request"]
        leaks = [tok for tok in FORBIDDEN_IN_FIRST_MESSAGE if tok in text.lower()]
        if not leaks:
            manifest.assert_no_leak(text, "first user message")
            out["leak_check"] = "clean"
            return out
        rejections = leaks
    raise RuntimeError(f"could not generate a leak-free first message (last leaks: {rejections})")


def _assert_rubric_model_gated(model: str, candidates_need_rubric: bool) -> None:
    """A model may only grade requirements after passing validate_rubric.py.

    Without this check an ungated model can silently write `met` into the ledger, which is the
    one failure mode the rubric gate exists to prevent (glm-5.3 passed 5/7 arms: it called a
    prose-only handoff and an overclaimed status `met`).
    """
    if not candidates_need_rubric:
        return
    name = "rubric_report.json" if model == "gpt-6-astra" else f"rubric_report.{model}.json"
    path = HERE / "runs" / "validation" / name
    if not path.is_file():
        raise SystemExit(
            f"{model} has no rubric gate result ({path}). Run:\n"
            f"  python3 validate_rubric.py --judge-model {model}\n"
            "and only use it as --rubric-model if every arm behaves as required."
        )
    report = json.loads(path.read_text(encoding="utf-8"))
    if not report.get("gate_passed"):
        failures = report.get("failures") or ["gate not marked as passed"]
        raise SystemExit(
            f"{model} did not pass the rubric gate, so it may not grade requirements:\n  - "
            + "\n  - ".join(failures)
            + f"\nUse --rubric-model with a gated model (e.g. gpt-6-astra) while keeping "
              f"--simulator-model/--judge-model {model}."
        )


def environment_card(overlay: Dict[str, Any]) -> str:
    lines = [
        f"- container: {overlay['container_image']} ({overlay['cpus']} cpu, {overlay['memory']} memory)",
        f"- network: {overlay['network']}",
        f"- runtime: {overlay['runtime']}",
        "- permissions: " + "; ".join(overlay["permissions"]),
        "- registered inputs:",
    ]
    for path, note in overlay["registered_fixtures"].items():
        lines.append(f"    {path} -- {note}")
    lines.append("- registered tools:")
    for path, note in overlay["registered_tools"].items():
        lines.append(f"    {path} -- {note}")
    return "\n".join(lines)


def build_agent_harness(dest: Path) -> Path:
    """The agent sees only the tools, never the oracle or the checker driver."""
    dest.mkdir(parents=True, exist_ok=True)
    for name in AGENT_HARNESS_FILES:
        shutil.copy2(FIXTURES / name, dest / name)
    return dest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", default="interactive", choices=["interactive", "interactive_no_evolution", "single_turn"])
    ap.add_argument("--persona", default="laptop_data_scientist")
    ap.add_argument("--max-turns", type=int, default=8)
    ap.add_argument("--max-steps", type=int, default=14)
    ap.add_argument("--max-evolved", type=int, default=2)
    ap.add_argument("--single-turn-steps", type=int, default=40)
    ap.add_argument("--run-id", default=None)
    # One model per role, so the system under test can be graded and interviewed by another
    # model. Defaults keep the single-model behaviour (and make self-play explicit).
    ap.add_argument("--agent-model", default="gpt-6-astra", help="the system under test")
    ap.add_argument("--simulator-model", default=None, help="user simulator (default: --agent-model)")
    ap.add_argument("--judge-model", default=None, help="interaction rubric judge (default: --agent-model)")
    ap.add_argument("--rubric-model", default=None, help="grader for LLM-judged requirements (default: --judge-model)")
    ap.add_argument("--adapter-model", default=None, help="first-message rewriter (default: --agent-model)")
    ap.add_argument("--reasoning-effort", default=None, choices=["low", "high", "max"],
                    help="override the provider default for thinking models (glm-*)")
    ap.add_argument("--keep-container", action="store_true")
    args = ap.parse_args()

    run_id = args.run_id or f"{args.arm}-{time.strftime('%Y%m%dT%H%M%S')}"
    run_dir = HERE / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    call_log = CallLog(path=run_dir / "llm_calls.jsonl")
    clients: Dict[str, Llm] = {}

    def llm_for(name: str) -> Llm:
        if name not in clients:
            clients[name] = Llm(model=name, call_log=call_log, reasoning_effort=args.reasoning_effort)
        return clients[name]

    models = {
        "agent": args.agent_model,
        "simulator": args.simulator_model or args.agent_model,
        "judge": args.judge_model or args.agent_model,
        "adapter": args.adapter_model or args.agent_model,
    }
    models["rubric_judge"] = args.rubric_model or models["judge"]
    _assert_rubric_model_gated(models["rubric_judge"], candidates_need_rubric=(args.arm == "interactive"))
    agent_llm = llm_for(models["agent"])
    simulator_llm = llm_for(models["simulator"])
    judge_llm = llm_for(models["judge"])
    adapter_llm = llm_for(models["adapter"])
    rubric_llm = llm_for(models["rubric_judge"])
    print("models: " + json.dumps(models), flush=True)

    manifest = TaskManifest.from_harbor_task()
    harness_dir = build_agent_harness(run_dir / "agent_harness")
    manifest.environment = build_environment_overlay(manifest, BULK_WORKLOAD, "/harness")
    checkers = CheckerRegistry(FIXTURES, BULK_VOLUME, Path(manifest.hidden["tests_dir"]), run_dir,
                               judge_llm=rubric_llm)
    ledger = Ledger()

    max_evolved = 0 if args.arm != "interactive" else args.max_evolved
    candidates = build_candidates(args.persona, max_evolved=max_evolved)
    selected = applicable_candidates(candidates, max_evolved)
    controller = Controller(
        manifest=manifest, ledger=ledger, checkers=checkers, candidates=candidates, selected=selected,
        run_dir=run_dir, max_turns=args.max_turns if args.arm != "single_turn" else 1,
        snapshot_tag_prefix=f"tb2pilot-snap-{run_id}".replace("_", "-").lower()[:70],
    )
    validation_report_path = HERE / "runs" / "validation" / "validation_report.json"
    validation_report = json.loads(validation_report_path.read_text()) if validation_report_path.is_file() else None
    controller.register_candidates(validation_report)

    container = Container(
        name=f"tb2pilot-{run_id}".replace("_", "-")[:60],
        image=manifest.docker_image,
        mounts=[Mount(str(harness_dir), "/harness", read_only=True), Mount(BULK_VOLUME, BULK_WORKLOAD, read_only=True)],
        memory="2g", cpus="1", network="none",
    )
    container.start()
    controller.snapshot(container, 0)  # pristine baseline, so `first_patch` is real

    persona = load_persona(args.persona)
    role_overlay = ROLE_OVERLAYS[args.persona]
    task_binding = {
        "task_id": manifest.task_id,
        "source": f"{manifest.dataset}@{manifest.dataset_version}",
        "applicable_families": ["data-processing", "measurable-resource-use"],
        "frozen_contract": manifest.public_view()["frozen_contract"],
        "bound_requirement_refs": [f"{c.requirement_id}@1" for c in selected],
        "entrypoint_contract": ENTRYPOINT,
        "turn_budget": args.max_turns,
    }
    simulator = UserSimulator(
        simulator_llm,
        render_system_prompt(persona, role_overlay, task_binding, manifest.environment, COMMUNICATION_STYLE),
    )
    agent = TerminalAgent(
        agent_llm, container, environment_card(manifest.environment),
        max_steps_per_turn=args.max_steps if args.arm != "single_turn" else args.single_turn_steps,
    )

    transcript: List[Dict[str, Any]] = []
    turn_records: List[Dict[str, Any]] = []
    started = time.time()
    error: Optional[str] = None

    try:
        if args.arm == "single_turn":
            first = {"vague_request": manifest.instruction, "kept": ["everything"], "omitted": [],
                     "leak_check": "n/a (baseline uses the original statement)"}
        else:
            first = make_first_message(adapter_llm, manifest)
        (run_dir / "first_message.json").write_text(json.dumps(first, ensure_ascii=False, indent=2), encoding="utf-8")

        user_message = first["vague_request"]
        turn = 1
        max_turns = controller.max_turns
        while True:
            transcript.append({"role": "user", "message_id": f"u-{turn}", "turn": turn, "text": user_message,
                               "evidence_refs": []})
            agent_turn = agent.run_turn(user_message, turn)
            transcript.append({"role": "agent", "message_id": agent_turn.message_id, "turn": turn,
                               "status": agent_turn.status, "text": agent_turn.reply,
                               "shell_steps": len(agent_turn.steps)})
            print(f"[turn {turn}] agent status={agent_turn.status} steps={len(agent_turn.steps)}", flush=True)

            image, digest, changed = controller.snapshot(container, turn)
            files = container.file_list()
            has_artifact = digest != controller.initial_digest
            if has_artifact and "first_patch" not in controller.fired:
                controller.add_event("first_patch", turn, {"snapshot": digest})
            if not has_artifact and "first_plan" not in controller.fired:
                controller.add_event("first_plan", turn, {"reply_id": agent_turn.message_id})
            if "approach_visible" not in controller.fired:
                controller.add_event("approach_visible", turn, {"via": "artifact" if has_artifact else "plan"})
            if agent_turn.status == "done" and "completion_claim" not in controller.fired:
                controller.add_event("completion_claim", turn, {"reply_id": agent_turn.message_id})
            controller.last_transcript = transcript
            probes = controller.run_probes(turn)
            checks = controller.verify_active(turn, transcript)

            if args.arm == "single_turn" or turn >= max_turns or controller.end_state:
                break

            agent_view = {"id": agent_turn.message_id, "status": agent_turn.status, "text": agent_turn.reply,
                          "shell_steps_used": len(agent_turn.steps)}
            obs = controller.build_observation(turn + 1, agent_view, files, changed, probes, checks)
            rejections_log: List[List[str]] = []
            decision = None
            for attempt in range(4):
                try:
                    decision = simulator.decide(obs, rejections_log[-1] if rejections_log else None)
                except SchemaRepairFailed as exc:
                    rejections_log.append([f"decision was not schema-valid: {exc.errors}"])
                    print(f"  [simulator schema failure, attempt {attempt + 1}] {exc.errors}", flush=True)
                    continue
                errs = controller.validate_decision(decision, obs)
                if not errs:
                    break
                rejections_log.append(errs)
                print(f"  [simulator rejected, attempt {attempt + 1}] {errs}", flush=True)
            else:
                # A simulator that cannot stay inside policy (or cannot emit a valid decision object)
                # invalidates the episode; it does not crash the run, because the outcome verifier
                # still has something to say.
                controller.invalid_reasons.append(f"simulator_policy_violation:{rejections_log[-1][0][:80]}")
                turn_records.append({"turn": turn + 1, "observation": obs, "decision": decision,
                                     "rejections": rejections_log, "extra_checks": [],
                                     "agent_turn": agent_turn.to_json()})
                print("  [simulator abandoned this turn: episode marked evaluation_invalid]", flush=True)
                break

            if decision is None:
                controller.invalid_reasons.append("simulator_no_valid_decision")
                break
            extra_checks = controller.apply_decision(decision, obs, turn + 1)
            turn_records.append({
                "turn": turn + 1, "observation": obs, "decision": decision,
                "rejections": rejections_log,
                "extra_checks": [c.public_summary() for c in extra_checks],
                "agent_turn": agent_turn.to_json(),
            })
            (run_dir / "turns.json").write_text(json.dumps(turn_records, ensure_ascii=False, indent=2), encoding="utf-8")

            if decision["action"] == "end":
                transcript.append({"role": "user", "message_id": f"u-{turn + 1}", "turn": turn + 1,
                                   "text": decision["user_message"], "evidence_refs": decision.get("evidence_refs", []),
                                   "closing": True})
                controller.end_state = decision.get("end_state")
                break
            user_message = decision["user_message"]
            turn += 1

        agent_turns_used = turn
        final_image, final_digest, _ = controller.snapshot(container, 99)
        reference_specs = []
        if args.arm != "interactive":
            for cand in candidates[:2]:
                reference_specs.append({"id": cand.checker["id"], "version": cand.checker["version"],
                                        "parameters": cand.checker["parameters"]})
        outcome = verify_outcome(ledger, checkers, final_image, final_digest, turn=99,
                                reference_checkers=reference_specs, transcript=transcript)
        judge_out = None
        if args.arm != "single_turn":
            judge_out = run_judge(
                judge_llm,
                [{k: v for k, v in t.items() if k in ("role", "message_id", "turn", "status", "text")} for t in transcript],
                ledger.events,
                [c.public_summary() for c in checkers.results],
            )
        metrics = compute_metrics(args.arm, outcome, ledger, controller, transcript, judge_out,
                                 max_turns=max_turns, agent_turns_used=agent_turns_used)
    except Exception:
        error = traceback.format_exc()
        print(error, flush=True)
        outcome, metrics, judge_out = {}, {"arm": args.arm, "error": "episode_failed"}, None
    finally:
        if not args.keep_container:
            container.remove()

    ledger.dump(run_dir / "ledger.json")
    episode = {
        "run_id": run_id,
        "arm": args.arm,
        "persona": args.persona,
        "task": manifest.public_view(),
        "frozen_digest": manifest.frozen_digest(),
        "seconds": round(time.time() - started, 1),
        "models": models,
        "self_play": len(set(models.values())) == 1,
        "llm": call_log.totals(),
        "gpt6": call_log.totals(),  # legacy key, same payload
        "transcript": transcript,
        "events": controller.events,
        "outcome": outcome,
        "metrics": metrics,
        "judge": judge_out,
        "error": error,
    }
    (run_dir / "episode.json").write_text(json.dumps(episode, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(run_dir / "transcript.md", episode)
    for tag in {f"{controller.snapshot_tag_prefix}-t{t}:latest" for t in list(range(0, args.max_turns + 2)) + [99]}:
        dockerx.remove_image(tag)
    print(f"\n=== {run_id} ===")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    print(f"artifacts: {run_dir}")
    return 0 if error is None else 1


def write_markdown(path: Path, episode: Dict[str, Any]) -> None:
    lines = [f"# {episode['run_id']} ({episode['arm']})", ""]
    m = episode.get("metrics") or {}
    lines += [
        f"- task: `{episode['task']['task_id']}` ({episode['task']['source']}), frozen digest `{episode['frozen_digest']}`",
        f"- persona: `{episode['persona']}`",
        f"- base task: **{'PASS' if m.get('base_task_success') else 'FAIL'}**, joint success: **{m.get('joint_success')}**",
        f"- models: {json.dumps(episode.get('models', {}))}"
        + (" (self-play)" if episode.get("self_play") else " (cross-model)"),
        f"- LLM calls: {episode['llm'].get('calls')} ({episode['llm'].get('input_tokens')} in / {episode['llm'].get('output_tokens')} out)",
        f"- wall time: {episode['seconds']}s",
        "",
        "## Transcript",
        "",
    ]
    for item in episode["transcript"]:
        who = "USER" if item["role"] == "user" else f"AGENT ({item.get('status', '')})"
        lines += [f"### {item['message_id']} - {who}", "", item["text"].strip(), ""]
    lines += ["## Evolved requirements", ""]
    for req in (episode.get("outcome", {}).get("evolved_requirements") or []):
        lines += [
            f"### {req['requirement']} - {req['status']} ({req['reason_code']})",
            f"- disclosed at turn {req['introduced_turn']}, agent claimed: {req['claimed_by_agent']}",
            f"- checker: `{req['checker']}`, evidence `{req['evidence_ref']}`",
            "",
            "> " + req["statement"].replace("\n", "\n> "),
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
