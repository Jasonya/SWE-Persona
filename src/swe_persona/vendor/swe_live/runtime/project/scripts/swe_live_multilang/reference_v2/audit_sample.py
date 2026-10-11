"""Read-only release checks; exporting preserves all failures and unknowns.

No API calls. No git operations. A review bundle is not automatically a paper
result: human inspection of the opening/binding/transcript remains necessary.
"""
import bootstrap
import argparse
from pathlib import Path
import json
import shutil

from support import PROTOCOL, LEGACY_PROTOCOL, FULLSET_PROTOCOL, digest, file_hash, read, save, interaction_audit_errors


def audit(run):
    run = Path(run)
    problems = []
    required = ("episode.json", "protocol.json", "ledger.json", "turns.json", "transcript.json",
                "checks.jsonl", "predictions.json", "coverage.json", "first_message.json",
                "evaluation/results.json", "snapshots/turn-99/patch.diff", "snapshots/turn-99/commands.json")
    missing = [name for name in required if not (run / name).is_file()]
    if missing:
        return {"ready_for_review": False, "problems": ["missing:" + n for n in missing],
                "publication_status": "blocked", "note": "Pilot summaries cannot be relabelled as reference episodes"}
    episode, protocol = read(run / "episode.json"), read(run / "protocol.json")
    if episode.get("protocol") not in (PROTOCOL, LEGACY_PROTOCOL, FULLSET_PROTOCOL) or protocol.get("protocol") != episode.get("protocol"):
        problems.append("wrong_protocol")
    if protocol.get("protocol") in (PROTOCOL, FULLSET_PROTOCOL):
        if protocol.get("patch_baseline") != "observed_worktree_tree_v2":
            problems.append("missing_worktree_baseline_protocol")
        if episode["arm"] != "single_turn":
            from message_guard import coverage_errors
            if protocol.get("message_guard") != "public_only_pre_delivery_v1" or not (run / "message_guard.json").is_file():
                problems.append("missing_message_guard")
            else:
                problems.extend(coverage_errors(read(run / "transcript.json"), read(run / "message_guard.json")))
                if episode.get("transcript") != read(run / "transcript.json"):
                    problems.append("guarded_transcript_mismatch")
    if protocol.get("frozen_digest") != episode.get("frozen_digest"):
        problems.append("frozen_contract_mismatch")
    if not protocol.get("sources") or not protocol.get("image_id"):
        problems.append("missing_source_or_image_provenance")
    # Earlier attempts retain their original audit interpretation and source
    # hashes. New image-baseline attempts must retain both attribution records.
    baseline_file = run / "initial_baseline.json"
    if protocol.get("patch_baseline") in ("observed_image_tree_v1", "observed_worktree_tree_v2") and not baseline_file.is_file():
        problems.append("missing_image_baseline")
    if baseline_file.is_file():
        baseline = read(baseline_file)
        if protocol.get("patch_baseline") == "observed_worktree_tree_v2" and not baseline.get("worktree_tree"):
            problems.append("missing_worktree_tree")
        initial_patch = run / "initial_tracked.diff"
        if not initial_patch.is_file() or file_hash(initial_patch) != baseline.get("patch_sha256"):
            problems.append("image_baseline_digest_mismatch")
        initial_model = run / "snapshots/turn-0/patch.diff"
        if not initial_model.is_file() or initial_model.read_text(encoding="utf-8").strip():
            problems.append("initial_model_diff_not_empty")
    if not protocol.get("gold_preflight", {}).get("report_sha256"):
        problems.append("missing_gold_preflight")
    if episode.get("models") != protocol.get("models"):
        problems.append("model_provenance_mismatch")
    metrics = episode.get("metrics", {})
    validity = metrics.get("valid_evaluation_coverage", {})
    if validity.get("episode_valid") is not True:
        problems.append("evaluation_invalid")
    if episode.get("error"):
        problems.append("episode_error")
    if metrics.get("interaction_delivery", {}).get("out_of_policy_disclosure_audit"):
        problems.append("interaction_audit_detected_policy_violation")
    report = read(run / "evaluation/results.json")
    if report.get("submitted") != 1 or report.get("error") != 0 or report.get("incomplete") != 0:
        problems.append("official_verifier_incomplete")
    if metrics.get("base_task_success") is None:
        problems.append("base_task_unknown")
    elif metrics["base_task_success"] != int(report.get("success") == 1):
        problems.append("base_score_report_mismatch")
    checks = [json.loads(line) for line in (run / "checks.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    official = [c for c in checks if c["checker_id"] == "official_tests"]
    if not official or official[-1]["evidence"].get("report_sha256") != file_hash(run / "evaluation/results.json"):
        problems.append("official_report_digest_mismatch")
    task_id = episode["task"]["task_id"]
    prediction = read(run / "predictions.json").get(task_id, {}).get("model_patch")
    snapshot_file = run / "snapshots/turn-99/patch.diff"
    # Preserve the original interpretation of older Windows-written snapshots.
    # Byte-preserving transport is an explicit amendment on new protocols only.
    snapshot = (snapshot_file.read_bytes().decode("utf-8")
                if protocol.get("patch_transport") == "base64_utf8_exact_v1"
                else snapshot_file.read_text(encoding="utf-8"))
    if prediction != snapshot:
        problems.append("final_patch_mismatch")
    final_digest = digest(snapshot)
    if protocol.get("snapshot_fingerprint") == "patch_and_fixture_state_v1":
        auxiliary_path = run / "snapshots/turn-99/auxiliary.json"
        if not auxiliary_path.is_file():
            problems.append("missing_final_fixture_state")
        else:
            auxiliary = read(auxiliary_path)
            if auxiliary.get("state_digest") != digest(auxiliary.get("state")):
                problems.append("fixture_state_digest_mismatch")
            final_digest = digest({"patch": snapshot, "fixture_state": auxiliary.get("state")})
    if protocol.get("task_binding_digest"):
        binding_path = run / "task-binding/binding.json"
        if not binding_path.is_file() or digest(read(binding_path)) != protocol["task_binding_digest"]:
            problems.append("task_binding_digest_mismatch")
        else:
            try:
                from task_binding import safe_relative
                binding = read(binding_path)
                for artifact in binding["evidence"]:
                    file = binding_path.parent / safe_relative(artifact["path"])
                    if not file.is_file() or file_hash(file) != artifact["sha256"]:
                        problems.append("task_binding_evidence_digest_mismatch")
            except (KeyError, TypeError, ValueError):
                problems.append("malformed_task_binding")
    if episode["arm"] != "single_turn" and episode.get("judge") is None:
        problems.append("interaction_judge_missing")
    elif episode["arm"] != "single_turn":
        problems.extend(interaction_audit_errors(episode.get("judge"), episode.get("transcript", []),
                                                 checks, episode.get("events", [])))
    if episode["arm"] == "interactive":
        gate_path = run / "checker_gate.json"
        if not gate_path.is_file() or read(gate_path).get("gate_passed") is not True:
            problems.append("checker_gate_missing_or_failed")
        elif read(gate_path).get("identity", {}).get("sources") != protocol.get("sources"):
            problems.append("checker_gate_source_mismatch")
        elif read(gate_path).get("identity", {}).get("model") != episode.get("models", {}).get("rubric_judge"):
            problems.append("checker_gate_model_mismatch")
        elif protocol.get("task_binding_digest") and read(gate_path).get("identity", {}).get("task_binding_digest") != protocol["task_binding_digest"]:
            problems.append("checker_gate_task_binding_mismatch")
        requirements = episode.get("outcome", {}).get("evolved_requirements", [])
        if not requirements:
            problems.append("no_evolved_requirement")
        for requirement in requirements:
            if requirement.get("status") not in ("met", "not_met"):
                problems.append("requirement_unknown:" + requirement.get("requirement", "?"))
            cited = [c for c in checks if c.get("evidence_ref") == requirement.get("evidence_ref")]
            if not cited or cited[-1]["snapshot"] != "final:" + final_digest:
                problems.append("stale_final_requirement_evidence")
    return {"ready_for_review": not problems, "problems": problems,
            "publication_status": "awaiting_transcript_review" if not problems else "blocked",
            "base_task_success": metrics.get("base_task_success"), "joint_success": metrics.get("joint_success"),
            "note": "A valid functional/persona failure is publishable evidence; this check does not require success"}


def export_review(run, output):
    report = audit(run)
    if not report["ready_for_review"]:
        raise ValueError("Cannot export: " + "; ".join(report["problems"]))
    run, output = Path(run).resolve(), Path(output).resolve()
    if output == run or run in output.parents:
        raise ValueError("Export outside the source run to preserve immutable evidence")
    if output.exists():
        raise ValueError("Output exists; exports never overwrite prior evidence")
    output.mkdir(parents=True)
    # Explicit public/review allowlist: no .env, private dataset row, provider
    # request logs, agent thought history, or hidden test patches are exported.
    names = ("episode.json", "protocol.json", "ledger.json", "turns.json", "transcript.json", "transcript.md",
             "checks.jsonl", "predictions.json", "coverage.json", "first_message.json", "task.public.json",
             "checker_gate.json", "simulator_prompt.txt", "binding.json", "initial_baseline.json", "message_guard.json",
             "initial_tracked.diff", "initial_untracked.json", "evaluation/results.json",
             "evaluation/runtime-summary.json", "evaluation/runtime-events.jsonl")
    for name in names:
        source = run / name
        if source.is_file():
            target = output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    for name in ("snapshots", "checker_validation", "task-binding", "fixture-verification"):
        if (run / name).is_dir():
            shutil.copytree(run / name, output / name)
    save(output / "release_check.json", report)
    hashes = {p.relative_to(output).as_posix(): file_hash(p) for p in output.rglob("*") if p.is_file()}
    save(output / "files.sha256.json", hashes)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--export-review", type=Path)
    args = parser.parse_args()
    report = export_review(args.run, args.export_review) if args.export_review else audit(args.run)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready_for_review"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
