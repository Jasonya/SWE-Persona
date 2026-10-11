"""Separate reference-protocol runner. Default is a zero-API configuration check.

--run explicitly enables paid calls. The old pilot runner and attempts are not
modified. Two preregistered persona templates are supported; coverage is reported.
"""
import bootstrap
from bootstrap import ROOT, REFERENCE
import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import shutil
import time
import uuid

from agent import TerminalAgent
from controller import ROLE_OVERLAYS
from judge import run_judge
from ledger import Ledger
from llm import CallLog, Llm, SchemaRepairFailed
from run_episode import VAGUE_SCHEMA, VAGUE_SYSTEM, _assert_rubric_model_gated
from simulator import UserSimulator, render_system_prompt
from verifier import compute_metrics, verify_outcome
from calibration import calibrate, calibrate_bound, valid_gate, gate_location
from support import (Checkers, Manifest, PERSONA, PROTOCOL, Runtime, Snapshots, SweController,
                     candidates, digest, file_hash, read, save, source_hashes)
from support import interaction_audit_errors, repository_revisions
from support import FULLSET_PROTOCOL
from execution_policy import BudgetClient, Deadline, BudgetExpired
from personas import PERSONAS, POOL_PERSONAS, PR_OVERLAY, binding_record, initial_notice
from message_guard import BOUNDARY, MessageGuard


def opening(llm, manifest, feedback=None):
    # The reference prompt is reused; TB-specific forbidden dates/paths are not.
    out = llm.json_obj([{"role": "system", "content": VAGUE_SYSTEM},
                       {"role": "user", "content": manifest.instruction +
                        ("\n\nPrevious draft rejected: " + "; ".join(feedback) if feedback else "")}],
                      VAGUE_SCHEMA, schema_name="vague_request", role="adapter.first_message", max_output_tokens=1200)
    text = out["vague_request"].strip()
    if not text or len(text.split()) > 70 or "```" in text:
        raise ValueError("Initial request violates the reference opening format")
    manifest.assert_no_leak(text, "initial request")
    out["leak_check"] = "public allowlist + private-payload guard; semantic review still required"
    return out


def gold_proof(directory, manifest, image_id):
    directory = Path(directory)
    stamp = directory / "validated.json"
    expected = {"image_id": image_id, "task_sha256": manifest.task_sha256}
    if not stamp.is_file() or read(stamp) != expected:
        raise ValueError("No matching successful gold preflight for this task and image")
    reports = sorted(directory.glob("attempt-*/evaluation/results.json"))
    passed = [p for p in reports if read(p).get("submitted") == 1 and read(p).get("success") == 1
              and read(p).get("error") == 0 and read(p).get("incomplete") == 0]
    if not passed:
        raise ValueError("Gold stamp has no successful underlying official report")
    report = passed[-1]
    return {"stamp": expected, "report": read(report), "report_sha256": file_hash(report)}


def build_simulator(llm, manifest, selected, max_turns, persona_id=PERSONA, workspace_binding=None, task_binding=None):
    persona, _, _ = candidates(persona_id, task_binding)
    style = {"verbosity": "terse", "explanation": "decision summary",
             "ordering": "recommendation -> work status -> decisions needed",
             "length": "at most 6 short sentences, no implementation code or command dumps"}
    binding = {"task_id": manifest.task_id, "source": manifest.public_view()["source"],
               "frozen_contract": manifest.public_view()["frozen_contract"],
               "bound_requirement_refs": [c.requirement_id + "@1" for c in selected],
               "turn_budget": max_turns}
    if persona_id == PERSONAS[1]:
        style = {"verbosity": "brief", "explanation": "one small patch-specific step at a time",
                 "ordering": "question -> evidence -> next step", "length": "at most 6 short sentences"}
        if workspace_binding is None:
            raise ValueError("PR simulator requires the observed initial workspace binding")
        binding["permissions_and_validation"] = workspace_binding
    if task_binding is not None:
        style = {"verbosity": "brief", "explanation": persona["attributes"]["interaction_preferences"],
                 "ordering": "Use only the currently active or due requirement", "length": "at most 6 short sentences"}
        binding["registered_public_task_facts"] = {
            "initial_notice": task_binding["initial_notice"], "facts": task_binding["public_facts"]}
    overlay = PR_OVERLAY if persona_id == PERSONAS[1] else ROLE_OVERLAYS.get(persona_id, {
        "id": persona_id + "_pool_overlay", "behaviour": persona["attributes"]["interaction_preferences"],
        "forbidden": persona["attributes"]["constraints"]})
    prompt = render_system_prompt(persona, overlay, binding, manifest.environment, style) + "\n\n" + BOUNDARY
    manifest.assert_no_leak(prompt, "simulator system")
    return UserSimulator(llm, prompt), prompt


def dialogue(agent, simulator, controller, manifest, first, run, arm, guard=None):
    transcript, records = [], []
    user_message, disclosure = first, None
    turn = 0
    error = None
    try:
        for turn in range(1, controller.max_turns + 1):
            user = {"role": "user", "message_id": f"u-{turn}", "turn": turn, "text": user_message,
                    "evidence_refs": records[-1]["decision"].get("evidence_refs", []) if records else []}
            if disclosure:
                user["disclosed_requirement"] = disclosure
            transcript.append(user)
            save(run / "transcript.json", transcript)
            fixture = vars(agent.container).get("task_fixture")
            if fixture is not None:
                fixture.before_turn(user)
            turn_started = time.monotonic()
            agent.container.command_source = "agent"
            agent.container.agent_turn = turn
            try:
                reply = agent.run_turn(user_message, turn)
            finally:
                agent.container.command_source = "harness"
            transcript.append({"role": "agent", "message_id": reply.message_id, "turn": turn,
                               "status": reply.status, "text": reply.reply, "shell_steps": len(reply.steps),
                               "seconds": round(time.monotonic() - turn_started, 3)})
            save(run / f"agent-turn-{turn}.json", reply.to_json())
            if fixture is not None:
                fixture.transcript = transcript
            image, snapshot, changed = controller.snapshot(agent.container, turn)
            has_patch = (bool(controller.checkers.snapshots.items[image]["patch"].strip()) if fixture is not None
                         else snapshot != controller.initial_digest)
            event = "first_patch" if has_patch else "first_plan"
            if event not in controller.fired:
                controller.add_event(event, turn, {"snapshot": snapshot})
            if "approach_visible" not in controller.fired:
                controller.add_event("approach_visible", turn, {"via": "artifact" if has_patch else "plan"})
            if reply.status == "done":
                if "completion_claim" not in controller.fired:
                    controller.add_event("completion_claim", turn, {"message_id": reply.message_id})
                for entry in controller.ledger.active():
                    controller.ledger.record_claim(entry.ref, turn, reply.message_id)
            controller.last_transcript = transcript
            probes = controller.run_probes(turn)
            checks = controller.verify_active(turn, transcript)
            controller.ledger.dump(run / "ledger.json")
            print(f"[turn {turn}/{controller.max_turns}] {reply.status}; checks={[c.status for c in checks]}", flush=True)
            if arm == "single_turn" or turn == controller.max_turns:
                break
            agent_view = {"id": reply.message_id, "status": reply.status, "text": reply.reply,
                          "shell_steps_used": len(reply.steps)}
            files = controller.checkers.snapshots.items[image]["listing"]
            obs = controller.build_observation(turn + 1, agent_view, files, changed, probes, checks)
            rejections, decision = [], None
            for _ in range(4):
                try:
                    decision = simulator.decide(obs, rejections[-1] if rejections else None)
                    if fixture is not None and hasattr(fixture, 'decorate_decision'):
                        fixture.decorate_decision(decision, turn + 1)
                    errors = controller.validate_decision(decision, obs)
                    if not errors and guard is not None:
                        errors = guard.check(decision["user_message"], turn + 1, decision["action"], obs, transcript)
                except SchemaRepairFailed:
                    errors = ["decision was not schema-valid"]
                if not errors:
                    break
                rejections.append(errors)
            else:
                controller.invalid_reasons.append("simulator_policy_violation")
                records.append({"turn": turn + 1, "observation": obs, "decision": decision, "rejections": rejections})
                break
            # Validate the actual text immediately before it enters agent history.
            manifest.assert_no_leak(decision["user_message"], "simulator reply")
            extra = controller.apply_decision(decision, obs, turn + 1)
            records.append({"turn": turn + 1, "observation": obs, "decision": decision,
                            "rejections": rejections, "extra_checks": [c.public_summary() for c in extra]})
            save(run / "turns.json", records)
            if decision["action"] == "end":
                transcript.append({"role": "user", "message_id": f"u-{turn + 1}", "turn": turn + 1,
                                   "text": decision["user_message"], "closing": True,
                                   "evidence_refs": decision["evidence_refs"]})
                break
            user_message = decision["user_message"]
            disclosure = obs["requirement_to_disclose"]["requirement_ref"] if obs["requirement_to_disclose"] else None
    except Exception as exc:
        error = type(exc).__name__
        if isinstance(exc, BudgetExpired):
            controller.invalid_reasons.append("interaction_budget_exhausted")
            controller.end_state = "interaction_budget_exhausted"
        else:
            controller.infra_failures.append("dialogue:" + error)
    finally:
        save(run / "transcript.json", transcript)
        save(run / "turns.json", records)
        save(run / "agent_history.json", agent.history)
        controller.ledger.dump(run / "ledger.json")
    return transcript, turn, error


def write_transcript(run, episode):
    metrics = episode["metrics"]
    lines = [f"# {episode['run_id']}", "", f"Protocol: {episode['protocol']}; arm: {episode['arm']}",
             f"Task: {episode['task']['task_id']}; persona: {episode['persona']}",
             f"Base success: {metrics['base_task_success']}; joint success: {metrics['joint_success']}",
             f"Publication status: {episode['publication_status']}", ""]
    for message in episode["transcript"]:
        lines.extend([f"## {message['message_id']} ({message['role']})", "", message["text"], ""])
    (run / "transcript.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--language", choices=("c", "cpp", "go", "js", "rust", "java", "ts", "cs"), default="js")
    parser.add_argument("--batch-dir", type=Path, default=ROOT / ".local/multilang-batch/pilot-v1")
    parser.add_argument("--task", type=Path, help="Explicit frozen private task row for a study")
    parser.add_argument("--gold-dir", type=Path, help="Matching successful official gold preflight")
    parser.add_argument("--persona", choices=POOL_PERSONAS, default=PERSONA)
    parser.add_argument("--binding", type=Path, help="Frozen public task-specific persona binding with executed fixtures")
    parser.add_argument("--arm", choices=("interactive", "interactive_no_evolution", "single_turn"), default="interactive")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--max-steps", type=int, default=14)
    parser.add_argument("--single-turn-steps", type=int, default=40)
    parser.add_argument("--interaction-seconds", type=float, default=900,
                        help="Model dialogue budget, including its shell/checker calls; 0 disables")
    parser.add_argument("--verification-seconds", type=float, default=900,
                        help="Separate final review budget, including official tests and final judges; 0 disables")
    parser.add_argument("--request-seconds", type=float, default=90,
                        help="One model request timeout; no more than two transport attempts")
    parser.add_argument("--sample-seconds", type=float,
                        help="One deadline for initialization, interaction and final evaluation; overrides phase budgets")
    parser.add_argument("--deadline-monotonic", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--fixed-schedule", action="store_true",
                        help="Disclose the preregistered requirement at turn 2 independent of agent events")
    parser.add_argument("--opening-file", type=Path, help="Frozen public opening shared across matched pair arms")
    parser.add_argument("--agent-model", default="gpt-6-astra")
    for role in ("simulator", "judge", "rubric", "adapter"):
        parser.add_argument("--" + role + "-model")
    args = parser.parse_args()
    if args.sample_seconds is not None and (not math.isfinite(args.sample_seconds) or args.sample_seconds <= 0):
        parser.error("Whole-sample budget must be positive")
    if args.deadline_monotonic is not None and args.sample_seconds is None:
        parser.error("Absolute deadline requires --sample-seconds")
    sample_budget = Deadline(args.sample_seconds, "sample", args.deadline_monotonic) if args.sample_seconds else None
    protocol = FULLSET_PROTOCOL if sample_budget or args.binding else PROTOCOL
    if args.max_turns < 5 or min(args.max_steps, args.single_turn_steps) < 1:
        parser.error("Use at least five turns so the required deadline leaves an implementation opportunity")
    if (not all(math.isfinite(x) for x in (args.interaction_seconds, args.verification_seconds, args.request_seconds))
            or min(args.interaction_seconds, args.verification_seconds) < 0 or args.request_seconds <= 0):
        parser.error("Phase budgets must be nonnegative and request timeout positive")
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PATH"] = str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Docker/Docker/resources/bin") + os.pathsep + os.environ["PATH"]
    manifest = Manifest(args.task or args.batch_dir / "tasks" / args.language / "task.private.jsonl")
    task_binding = None
    if args.binding:
        from task_binding import load_binding
        task_binding = load_binding(args.binding, manifest, args.persona)
    elif args.persona not in PERSONAS:
        parser.error("This persona requires --binding with validated task-specific public fixtures")
    # Validate the actual checker registration even for a zero-API config check.
    persona, candidate, coverage = candidates(args.persona, task_binding)
    models = {"agent": args.agent_model, "simulator": args.simulator_model or args.agent_model,
              "judge": args.judge_model or args.agent_model, "adapter": args.adapter_model or args.agent_model}
    models["rubric_judge"] = args.rubric_model or models["judge"]
    models["message_guard"] = models["judge"]
    plan = {"protocol": protocol, "task": manifest.task_id, "arm": args.arm, "models": models,
            "persona": args.persona if args.arm != "single_turn" else None, "bound_requirements": 1 if args.arm == "interactive" else 0,
            "max_turns": args.max_turns if args.arm != "single_turn" else 1,
            "max_steps": args.max_steps, "single_turn_steps": args.single_turn_steps,
            "episode_wall_time_cap": args.sample_seconds,
            "schedule_policy": "fixed_turn_2" if args.fixed_schedule else "event_with_deadline",
            "interaction_seconds": args.interaction_seconds, "verification_seconds": args.verification_seconds,
            "request_seconds": args.request_seconds, "transport_attempts": 2, "schema_repairs": 0,
            "rubric_samples": 3, "rubric_concurrency": 3,
            "reference": "tb2_persona_pilot", "patch_baseline": "observed_worktree_tree_v2",
            "message_guard": "public_only_pre_delivery_v1" if args.arm != "single_turn" else "not_applicable",
            "command_evidence": "index_recent_and_bounded_history_v2",
            "patch_transport": "base64_utf8_exact_v1", "run_enabled": args.run}
    if task_binding is not None:
        plan["task_binding_digest"] = digest(task_binding)
        if task_binding["fixture_kind"] in ("nemoclaw_credential_v1", "docsify_recovery_v1", "otel_review_v1"):
            plan["snapshot_fingerprint"] = "patch_and_fixture_state_v1"
    print(json.dumps(plan, ensure_ascii=False, indent=2), flush=True)
    if not args.run:
        print("Configuration check only; no Docker work or API calls. Add --run to execute.")
        return 0
    if (args.batch_dir / "batch.lock").exists():
        raise SystemExit("The old batch is still locked. Wait for it to exit before starting another 16 GiB container.")
    _assert_rubric_model_gated(models["rubric_judge"], args.arm == "interactive")
    import docker
    client = docker.from_env()
    try:
        if client.info()["OSType"] != "linux":
            raise SystemExit("Linux containers required")
        image = client.images.get(manifest.docker_image)
        image_id = image.id
    finally:
        client.close()
    gold = gold_proof(args.gold_dir or args.batch_dir / "gold" / args.language, manifest, image_id)
    run = args.output_dir.resolve() if args.output_dir else ROOT / ".local/multilang-reference/runs" / (
        datetime.now().strftime("%Y%m%d-%H%M%S-") + args.language + "-" + uuid.uuid4().hex[:6])
    run.mkdir(parents=True, exist_ok=False)
    if task_binding is not None:
        target = run / "task-binding"
        target.mkdir()
        save(target / "binding.json", task_binding)
        for item in task_binding["evidence"]:
            destination = target / item["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(args.binding.parent / item["path"], destination)
    frozen_task = run / "task.private.jsonl"
    frozen_task.write_bytes(manifest.task_path.read_bytes())
    if file_hash(frozen_task) != manifest.task_sha256:
        raise RuntimeError("Task changed while freezing the run input")
    manifest.task_path = frozen_task
    total_started = time.monotonic()
    log = CallLog(path=run / "llm_calls.jsonl")
    clients = {model: BudgetClient(Llm(model=model, call_log=log), args.request_seconds) for model in set(models.values())}
    gate = {"gate_passed": True, "not_applicable": True}
    if args.arm == "interactive":
        cache, cached = gate_location(ROOT, models["rubric_judge"], args.persona, task_binding)
        if sample_budget and not cached:
            raise RuntimeError("Calibrate the persona checker before starting a timed sample")
        if cached:
            gate = read(cache / "report.json")
        else:
            gate = (calibrate_bound(clients[models["rubric_judge"]], cache, args.binding) if task_binding is not None
                    else calibrate(clients[models["rubric_judge"]], cache, args.persona))
        save(run / "checker_gate.json", gate)
        shutil.copytree(cache, run / "checker_validation")
        if not gate["gate_passed"]:
            save(run / "summary.json", {"status": "checker_gate_failed", "evaluation_valid": False})
            print("Checker gate failed; no task agent calls. Evidence:", cache)
            return 2
    calibration_seconds = time.monotonic() - total_started
    if args.fixed_schedule:
        candidate.schedule = dict(candidate.schedule, trigger_event="fixed_schedule", earliest_turn=2, deadline_turn=2)
    selected = [candidate] if args.arm == "interactive" else []
    save(run / "protocol.json", plan | {"sources": source_hashes(), "image_id": image_id,
         "repository_revisions": repository_revisions(),
         "frozen_digest": manifest.frozen_digest(), "task_sha256": manifest.task_sha256,
         "template_digest": digest(candidate.template), "gold_preflight": gold})
    save(run / "coverage.json", coverage)
    save(run / "task.public.json", manifest.public_view())
    runtime = Runtime("swe-reference-" + uuid.uuid4().hex[:12], image_id, memory="16g", cpus="4", network="none")
    public_path = None
    if task_binding is not None and task_binding.get('public_evidence'):
        from dockerx import Mount
        public_directory = run / 'public-fixture'
        public_directory.mkdir()
        public_path = public_directory / 'public.json'
        shutil.copyfile(run / 'task-binding' / task_binding['public_evidence'], public_path)
        runtime.mounts.append(Mount(str(public_directory), '/persona-fixture', read_only=True))
    if sample_budget:
        runtime.deadline = sample_budget
        for client in clients.values():
            client.deadline = sample_budget
    started = time.monotonic()
    episode = None
    fixture = None
    try:
        runtime.start()
        from credential_session import create_session
        fixture = create_session(task_binding, runtime, public_path)
        runtime.task_fixture = fixture
        if fixture is not None:
            fixture.start()
        rc, actual, _ = runtime.sh("command -v timeout && git rev-parse HEAD", timeout=15)
        if rc or actual.strip().splitlines()[-1] != manifest.base_commit:
            raise RuntimeError("Image base commit or timeout executable mismatch")
        snapshots = Snapshots(runtime, manifest, run)
        registry = Checkers(snapshots, clients[models["rubric_judge"]], run, manifest, args.verification_seconds)
        registry.task_binding = task_binding
        ledger = Ledger()
        controller = SweController(manifest, ledger, registry, [candidate] if selected else [], selected, run,
                                   max_turns=plan["max_turns"])
        controller.register_candidates(gate)
        if controller.invalid_reasons:
            raise RuntimeError("Candidate registration failed: " + ";".join(controller.invalid_reasons))
        controller.snapshot(runtime, 0)
        if snapshots.items[controller.current_image]["patch"].strip():
            raise RuntimeError("Initial model diff must be empty relative to the observed image baseline")
        binding = binding_record(manifest, snapshots.initial, snapshots.baseline)
        binding['task_persona_initial_notice'] = (task_binding['initial_notice'] if task_binding is not None
                                                 else initial_notice(args.persona, binding))
        if task_binding is not None:
            binding["task_persona_public_facts"] = task_binding["public_facts"]
            if task_binding['fixture_kind'] == 'otel_review_v1':
                from fixtures.review_permissions import AUTHORIZATION
                binding['preregistered_phase_authorization'] = {'earliest_turn': 3, 'text': AUTHORIZATION}
        save(run / "binding.json", binding)
        interaction_budget = sample_budget or Deadline(args.interaction_seconds, "interaction")
        runtime.deadline = interaction_budget
        for client in clients.values():
            client.deadline = interaction_budget
        simulator, prompt = build_simulator(clients[models["simulator"]], manifest, selected, args.max_turns, args.persona, binding, task_binding)
        (run / "simulator_prompt.txt").write_text(prompt, encoding="utf-8")
        guard = None
        if args.arm == "single_turn":
            first = {"vague_request": manifest.instruction, "kept": ["everything"], "omitted": []}
        else:
            guard = MessageGuard(clients[models["message_guard"]], manifest, binding, run)
            feedback = None
            for _ in range(4):
                if args.opening_file:
                    first = read(args.opening_file)
                    if first.get("task_id") != manifest.task_id or first.get("persona") != args.persona:
                        raise ValueError("Frozen opening belongs to a different task or persona")
                    manifest.assert_no_leak(first["vague_request"], "frozen opening")
                else:
                    first = opening(clients[models["adapter"]], manifest, feedback)
                    first["vague_request"] = ((task_binding["initial_notice"] + "\n\n") if task_binding is not None
                                               else initial_notice(args.persona, binding)) + first["vague_request"]
                feedback = guard.check(first["vague_request"], 1, "opening")
                if not feedback:
                    break
                if args.opening_file:
                    raise ValueError("Frozen opening failed the public-contract guard; do not regenerate one arm")
            else:
                raise ValueError("Opening failed the public-contract screen after four drafts")
        save(run / "first_message.json", first)
        steps = args.single_turn_steps if args.arm == "single_turn" else args.max_steps
        agent = TerminalAgent(clients[models["agent"]], runtime, json.dumps(manifest.environment),
                              max_steps_per_turn=steps, workdir="/testbed", command_timeout=900)
        transcript, turns, error = dialogue(agent, simulator, controller, manifest, first["vague_request"], run, args.arm, guard)
        final_image, final_digest, _ = controller.snapshot(runtime, 99)
        rollout_seconds = time.monotonic() - started
        # Release the working container before creating a fresh official verifier.
        if fixture is not None:
            fixture.remove()
            fixture = None
        runtime.remove()
        audit_evidence = [r.public_summary() for r in registry.results]
        final_started = time.monotonic()
        review_budget = sample_budget or Deadline(args.verification_seconds, "final_review")
        registry.review_deadline = review_budget
        for client in clients.values():
            client.deadline = review_budget
        outcome = verify_outcome(ledger, registry, final_image, final_digest, turn=99, transcript=transcript)
        judge = None
        if args.arm != "single_turn":
            try:
                # Bind the reference interaction-style question to this persona.
                import judge as judge_module
                style_question = ("Did the user keep the manager's concise decision-oriented style without code dumps?"
                                  if args.persona == PERSONA else
                                  "Did the user follow the registered persona's interaction preferences: " + persona["attributes"]["interaction_preferences"])
                judge_module.ITEMS = [(key, style_question if key == "persona_style" else text)
                                      for key, text in judge_module.ITEMS]
                judge = run_judge(clients[models["judge"]], transcript, ledger.events, audit_evidence)
                controller.infra_failures.extend(interaction_audit_errors(judge, transcript, audit_evidence, controller.events))
            except Exception as exc:
                controller.infra_failures.append("interaction_judge:" + type(exc).__name__)
        final_seconds = time.monotonic() - final_started
        if outcome["base_task"]["status"] == "unknown":
            controller.infra_failures.append("official_verifier_unknown")
        for entry in outcome["evolved_requirements"]:
            if entry["reason_code"] == "checker_error":
                controller.infra_failures.append("final_checker_error")
        metrics = compute_metrics(args.arm, outcome, ledger, controller, transcript, judge,
                                  max_turns=controller.max_turns, agent_turns_used=turns)
        # Preserve unknown infrastructure outcomes instead of turning them into failures.
        if outcome["base_task"]["status"] == "unknown":
            metrics["base_task_success"] = None
        if not metrics["valid_evaluation_coverage"]["episode_valid"]:
            metrics["joint_success"] = None
        if sample_budget:
            sample_budget.remaining()
        episode = {"run_id": run.name, "protocol": protocol, "arm": args.arm,
                   "persona": plan["persona"], "task": manifest.public_view(), "frozen_digest": manifest.frozen_digest(),
                   "seconds": round(time.monotonic() - total_started, 1),
                   "timing": {"checker_gate_seconds": round(calibration_seconds, 1),
                              "interaction_seconds": round(rollout_seconds, 1), "final_review_seconds": round(final_seconds, 1)},
                   "models": models, "self_play": len(set(models.values())) == 1, "llm": log.totals(),
                   "transcript": transcript, "events": controller.events, "outcome": outcome,
                   "metrics": metrics, "judge": judge, "error": error,
                   "publication_status": "awaiting_review" if metrics["valid_evaluation_coverage"]["episode_valid"] else "blocked"}
        ledger.dump(run / "ledger.json")
        save(run / "episode.json", episode)
        write_transcript(run, episode)
        from audit_sample import audit
        release = audit(run)
        save(run / "release_check.json", release)
        episode["publication_status"] = release["publication_status"]
        save(run / "episode.json", episode)
        write_transcript(run, episode)
        print(json.dumps(metrics, ensure_ascii=False, indent=2), flush=True)
        print("Review artifacts:", run)
        return 0 if release["ready_for_review"] else 2
    except BudgetExpired:
        save(run / "summary.json", {"status": "timeout", "joint_success": 0,
             "base_task_success": None, "completion_within_budget": False})
        return 124
    finally:
        if fixture is not None:
            fixture.remove()
        runtime.remove()
        save(run / "commands.json", getattr(runtime, "commands", []))
        save(run / "usage.json", log.totals())
        if episode is None and not (run / "summary.json").exists():
            save(run / "summary.json", {"status": "infrastructure_or_protocol_error", "evaluation_valid": False})


if __name__ == "__main__":
    raise SystemExit(main())
