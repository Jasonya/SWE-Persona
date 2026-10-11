"""Run separately from pilot tests to keep the two task_adapter modules isolated."""
import bootstrap
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import Mock, patch

from agent import AgentTurn
from ledger import Ledger
from support import (Checkers, ExactGrounding, Manifest, SweController, candidates, digest, save, file_hash, LEGACY_PROTOCOL as PROTOCOL)
from calibration import valid_gate
from run_reference import dialogue, gold_proof
from audit_sample import audit, export_review
from judge import ITEMS


def audited_scores(ref="u-2"):
    return {"scores": [{"item": key, "score": 2, "evidence_message_ids": [ref], "reason": "offline fixture"}
                        for key, _ in ITEMS], "total": 12, "max_total": 12}


def decision(obs, action=None):
    due = obs["requirement_to_disclose"]
    action = action or ("disclose" if due else "end" if obs["end_allowed"] else "feedback")
    return {"turn_id": obs["turn"], "observation_id": obs["observation_id"], "action": action,
            "user_message": due["statement"] if due else "Please continue.", "ledger_updates": [],
            "disclosures": [{k: due[k] for k in ("requirement_ref", "operation", "replaces")}] if due else [],
            "verification_requests": [], "evidence_refs": [], "branch_log": [],
            "end_state": "completed_pending_final" if action == "end" else None, "rationale": "test"}


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = self.root / "task.jsonl"
        save(self.task, {"instance_id": "owner__repo-1", "problem_statement": "Public repair goal.",
                         "base_commit": "base", "docker_image": "image", "patch": "PRIVATE_PATCH_" * 20,
                         "test_patch": "PRIVATE_TEST_" * 20})
        # save() is pretty JSON; dataset input is one JSON object per line.
        row = json.loads(self.task.read_text())
        self.task.write_text(json.dumps(row) + "\n")
        self.manifest = Manifest(self.task)
        _, self.candidate, _ = candidates()

    def controller(self):
        snapshots = Mock(items={})
        checkers = Checkers(snapshots, Mock(model="test"), self.root, self.manifest)
        controller = SweController(self.manifest, Ledger(), checkers, [self.candidate],
                                   [self.candidate], self.root, max_turns=8)
        controller.register_candidates({"gate_passed": True})
        controller.current_digest = "snapshot"
        controller.current_image = "image"
        return controller

    def test_only_public_fields_reach_prompts(self):
        public = json.dumps(self.manifest.public_view())
        self.assertNotIn("PRIVATE_PATCH", public)
        self.assertNotIn("PRIVATE_TEST", public)
        with self.assertRaises(ValueError):
            self.manifest.assert_no_leak("PRIVATE_PATCH_" * 20, "test")

    def test_reference_modules_are_actual_repository_sources(self):
        import controller, rubric_checker, simulator
        for module in (controller, rubric_checker, simulator):
            self.assertEqual(Path(module.__file__).parent.name, "tb2_persona_pilot")

    def test_candidate_preserves_pool_rubric_and_reports_coverage(self):
        _, candidate, coverage = candidates()
        self.assertEqual(candidate.statement(), candidate.template["user_requirement"])
        self.assertEqual(len(coverage), 30)
        self.assertEqual(sum(c["status"] == "bound_pending_validation" for c in coverage), 1)
        self.assertEqual(candidate.schedule["deadline_turn"], 4)

    def test_missing_validation_cannot_activate_candidate(self):
        c = self.controller()
        c.ledger = Ledger()
        c.invalid_reasons = []
        c.register_candidates({"gate_passed": False})
        self.assertTrue(c.invalid_reasons)
        self.assertFalse(c.ledger.active())

    def test_event_disclosure_and_no_early_end(self):
        c = self.controller()
        self.assertIsNone(c.due_item(2))
        c.add_event("approach_visible", 1, {})
        obs = c.build_observation(2, None, [], False, [], [])
        self.assertEqual(obs["allowed_actions"], ["disclose"])
        self.assertTrue(c.validate_decision(decision(obs, "end"), obs))
        d = decision(obs)
        self.assertEqual(c.validate_decision(d, obs), [])
        c.apply_decision(d, obs, 2)
        self.assertEqual(len(c.ledger.active()), 1)
        self.assertTrue(c.end_blockers(3))

    def test_checker_override_and_malformed_parameters_rejected(self):
        c = self.controller()
        c.add_event("approach_visible", 1, {})
        obs = c.build_observation(2, None, [], False, [], [])
        c.apply_decision(decision(obs), obs, 2)
        obs = c.build_observation(3, None, [], False, [], [])
        for params in ('{"samples":1}', '{broken'):
            d = decision(obs, "request_verification")
            d["verification_requests"] = [{"requirement_ref": self.candidate.requirement_id + "@1",
                                           "checker_id": "rubric_judge", "version": "1", "parameters": params}]
            self.assertTrue(c.validate_decision(d, obs))

    def test_fabricated_quote_downgraded_even_when_path_is_real(self):
        llm = Mock()
        llm.json_obj.return_value = {"clauses": [{"clause_id": "met_if[1]", "verdict": "satisfied",
            "grounding": [{"kind": "artifact", "ref": "patch.diff", "quote": "all tests passed"}]}]}
        guarded = ExactGrounding(llm, {"files": [{"path": "patch.diff", "text": "actual patch"}]}, [],
                                 {"checker": "probe@1"})
        self.assertEqual(guarded.json_obj()["clauses"][0]["verdict"], "unsupported")

    def test_rubric_only_grades_post_disclosure_and_uses_verbatim_clauses(self):
        c = self.controller()
        item = {"patch": "diff", "digest": "patchhash", "commands": [], "files": [], "listing": []}
        c.checkers.snapshots.items["image"] = item
        transcript = [{"role": "agent", "message_id": "a-1", "turn": 1, "text": "old code dump"},
                      {"role": "user", "message_id": "u-2", "turn": 2, "text": "requirement",
                       "disclosed_requirement": self.candidate.requirement_id + "@1"},
                      {"role": "agent", "message_id": "a-2", "turn": 2, "text": "summary"}]
        context = {"statement": self.candidate.statement(), "satisfaction": self.candidate.template["satisfaction"],
                   "transcript": transcript}
        with patch("support.grade", return_value=("pass", "test", {})) as grade:
            c.checkers.run("rubric_judge", "1", self.candidate.checker["parameters"], "image", "snapshot", 2, context)
            spec = grade.call_args.args[3]
            self.assertEqual(spec.met_if, self.candidate.template["satisfaction"]["met_if"])
            self.assertEqual([m["turn"] for m in grade.call_args.args[5]], [2, 2])

    def test_same_length_message_changes_invalidate_rubric_cache(self):
        c = self.controller()
        c.checkers.snapshots.items["image"] = {"patch": "diff", "digest": "hash", "commands": []}
        with patch.object(c.checkers, "rubric", return_value=("pass", "test", {})) as grade:
            for text in ("aaaa", "bbbb", "bbbb"):
                c.checkers.run("rubric_judge", "1", {}, "image", "same", 2, {"transcript": [{"text": text}]})
            self.assertEqual(grade.call_count, 2)

    def test_checker_error_is_unknown_not_failure(self):
        c = self.controller()
        c.checkers.snapshots.items["image"] = {"patch": "diff"}
        with patch.object(c.checkers, "rubric", side_effect=RuntimeError("offline")):
            result = c.checkers.run("rubric_judge", "1", {}, "image", "hash", 2)
        self.assertEqual((result.status, result.reason_code), ("unknown", "checker_error"))

    def test_official_error_is_unknown_and_has_no_reward(self):
        c = self.controller()
        with patch("support.subprocess.run", return_value=SimpleNamespace(returncode=1)):
            status, _, evidence = c.checkers.official("diff")
        self.assertEqual(status, "unknown")
        self.assertIsNone(evidence["reward"])

    def test_dialogue_exception_retains_transcript_and_marks_infrastructure(self):
        c = self.controller()
        agent = Mock(history=[], container=SimpleNamespace())
        agent.run_turn.side_effect = RuntimeError("offline test")
        transcript, turns, error = dialogue(agent, Mock(), c, self.manifest, "public", self.root, "interactive")
        self.assertEqual(error, "RuntimeError")
        self.assertTrue(c.infra_failures)
        self.assertEqual(len(transcript), 1)
        self.assertTrue((self.root / "ledger.json").is_file())

    def test_missing_gold_and_missing_gate_do_not_pass(self):
        with self.assertRaises(ValueError):
            gold_proof(self.root, self.manifest, "image-id")
        self.assertFalse(valid_gate(self.root / "absent.json", "gpt-6-astra"))

    def test_old_pilot_cannot_be_exported_as_finalized(self):
        save(self.root / "summary.json", {"status": "completed", "evaluation_valid": True})
        self.assertFalse(audit(self.root)["ready_for_review"])
        with self.assertRaises(ValueError):
            export_review(self.root, self.root / "export")
        self.assertFalse((self.root / "export").exists())

    def review_fixture(self):
        models = {"rubric_judge": "test"}
        report = {"submitted": 1, "success": 0, "failure": 1, "error": 0, "incomplete": 0}
        for directory in ("evaluation", "snapshots/turn-99"):
            (self.root / directory).mkdir(parents=True)
        save(self.root / "evaluation/results.json", report)
        save(self.root / "protocol.json", {"protocol": PROTOCOL, "frozen_digest": "frozen",
             "sources": {"source.py": "hash"}, "image_id": "image", "models": models,
             "gold_preflight": {"report_sha256": "goldhash"}})
        snapshot = self.root / "snapshots/turn-99/patch.diff"
        snapshot.write_text("diff", encoding="utf-8")
        save(self.root / "snapshots/turn-99/commands.json", [])
        save(self.root / "predictions.json", {"task": {"model_patch": "diff"}})
        for file in ("ledger.json", "turns.json", "transcript.json", "coverage.json", "first_message.json"):
            save(self.root / file, [])
        save(self.root / "checker_gate.json", {"gate_passed": True, "identity": {
            "sources": {"source.py": "hash"}, "model": "test"}})
        official = {"checker_id": "official_tests", "evidence_ref": "chk-1", "evidence": {
            "report_sha256": file_hash(self.root / "evaluation/results.json")}}
        rubric = {"checker_id": "rubric_judge", "evidence_ref": "chk-2", "snapshot": "final:" + digest("diff")}
        (self.root / "checks.jsonl").write_text(json.dumps(official) + "\n" + json.dumps(rubric) + "\n")
        episode = {"protocol": PROTOCOL, "frozen_digest": "frozen", "arm": "interactive", "models": models,
                   "task": {"task_id": "task"}, "error": None, "judge": audited_scores(),
                   "transcript": [{"message_id": "u-2"}],
                   "metrics": {"base_task_success": 0, "joint_success": 0,
                               "valid_evaluation_coverage": {"episode_valid": True}, "interaction_delivery": {}},
                   "outcome": {"evolved_requirements": [{"requirement": "req@1", "status": "not_met",
                                                        "evidence_ref": "chk-2"}]}}
        save(self.root / "episode.json", episode)

    def test_valid_failures_can_be_reviewed_and_secrets_are_not_exported(self):
        self.review_fixture()
        (self.root / ".env").write_text("DO_NOT_EXPORT=test-only")
        (self.root / "agent_history.json").write_text("DO_NOT_EXPORT=test-only")
        report = audit(self.root)
        self.assertEqual(report["problems"], [])
        self.assertTrue(report["ready_for_review"])
        with TemporaryDirectory() as temp:
            exported = Path(temp) / "sample"
            export_review(self.root, exported)
            self.assertFalse((exported / ".env").exists())
            self.assertFalse((exported / "agent_history.json").exists())
            self.assertFalse((exported / "task.jsonl").exists())
            self.assertTrue((exported / "files.sha256.json").exists())

    def test_release_detects_patch_and_verifier_tampering(self):
        self.review_fixture()
        save(self.root / "predictions.json", {"task": {"model_patch": "changed"}})
        save(self.root / "evaluation/results.json", {"submitted": 1, "success": 1, "error": 0, "incomplete": 0})
        problems = audit(self.root)["problems"]
        for expected in ("final_patch_mismatch", "official_report_digest_mismatch", "base_score_report_mismatch"):
            self.assertIn(expected, problems)

    def assert_patch_roundtrip(self, stored, prediction, exact):
        self.review_fixture()
        protocol_file = self.root / "protocol.json"
        protocol = json.loads(protocol_file.read_text(encoding="utf-8"))
        if exact:
            protocol["patch_transport"] = "base64_utf8_exact_v1"
        save(protocol_file, protocol)
        (self.root / "snapshots/turn-99/patch.diff").write_bytes(stored)
        save(self.root / "predictions.json", {"task": {"model_patch": prediction}})
        path = self.root / "checks.jsonl"
        checks = [json.loads(line) for line in path.read_text().splitlines()]
        checks[-1]["snapshot"] = "final:" + digest(prediction)
        path.write_text("\n".join(json.dumps(c) for c in checks) + "\n")
        self.assertTrue(audit(self.root)["ready_for_review"], audit(self.root))

    def test_audit_preserves_bare_cr_in_new_protocol(self):
        patch = "diff [\r] CRLF\r\n"
        self.assert_patch_roundtrip(patch.encode("utf-8"), patch, True)

    def test_legacy_windows_snapshots_keep_their_original_audit_interpretation(self):
        self.assert_patch_roundtrip(b"diff\r\n", "diff\n", False)

    def test_end_to_end_entrypoint_uses_shared_control_and_freezes_evidence(self):
        import run_reference
        from calibration import gate_identity
        batch = self.root / "batch"
        task = batch / "tasks/js/task.private.jsonl"
        task.parent.mkdir(parents=True)
        task.write_bytes(self.task.read_bytes())
        gold = batch / "gold/js"
        (gold / "attempt-001/evaluation").mkdir(parents=True)
        save(gold / "validated.json", {"image_id": "image-id", "task_sha256": file_hash(task)})
        save(gold / "attempt-001/evaluation/results.json", {"submitted": 1, "success": 1, "error": 0, "incomplete": 0})
        out = self.root / "run"
        runtime = Mock(commands=[])
        runtime.sh.return_value = (0, "/usr/bin/timeout\nbase\n", "")
        docker = Mock()
        docker.info.return_value = {"OSType": "linux"}
        docker.images.get.return_value.id = "image-id"
        llm = Mock(model="gpt-6-astra")
        response = {"clauses": [
            {"clause_id": "met_if[0]", "verdict": "satisfied", "reason": "fixture", "grounding": [
                {"kind": "agent_message", "ref": "a-2", "quote": "No decision is needed."}]},
            {"clause_id": "met_if[1]", "verdict": "satisfied", "reason": "fixture", "grounding": [
                {"kind": "artifact", "ref": "harness-observed/patch.diff", "quote": "diff"}]},
            {"clause_id": "not_met_if[0]", "verdict": "violated", "reason": "fixture", "grounding": [
                {"kind": "agent_message", "ref": "a-2", "quote": "No decision is needed."}]},
            {"clause_id": "unknown_if[0]", "verdict": "violated", "reason": "fixture", "grounding": [
                {"kind": "artifact", "ref": "harness-observed/patch.diff", "quote": "diff"}]}], "overall_note": "offline fixture"}
        def model_response(*a, **k):
            if k.get("schema_name") == "swe_message_guard":
                return dict(task_conflict=False, unauthorized_requirement=False, unsupported_fact=False,
                            requests_work=False, reason="offline guard fixture")
            return json.loads(json.dumps(response))
        llm.json_obj.side_effect = model_response
        simulator = Mock()
        simulator.decide.side_effect = lambda obs, rejections: decision(obs)
        agent = Mock(history=[], container=runtime)
        agent.run_turn.side_effect = [AgentTurn(1, "a-1", "Initial reply", "done"),
            AgentTurn(2, "a-2", "1. I recommend testing.\n2. The patch exists.\n3. No decision is needed.", "done")]
        def gate(model, output, persona):
            output.mkdir(parents=True)
            report = {"identity": gate_identity(model.model), "gate_passed": True, "cases": []}
            save(output / "report.json", report)
            return report
        def official(command, **kwargs):
            path = Path(command[command.index("--output_dir") + 1])
            path.mkdir()
            save(path / "results.json", {"submitted": 1, "success": 0, "failure": 1, "error": 0, "incomplete": 0})
            return SimpleNamespace(returncode=0)
        args = ["run_reference", "--run", "--batch-dir", str(batch), "--output-dir", str(out)]
        with patch("sys.argv", args), patch.object(run_reference, "ROOT", self.root), \
             patch("docker.from_env", return_value=docker), patch.object(run_reference, "Runtime", return_value=runtime), \
             patch.object(run_reference, "Llm", return_value=llm), patch.object(run_reference, "calibrate", side_effect=gate), \
             patch.object(run_reference, "repository_revisions", return_value={"team": "offline-test"}), \
             patch.object(run_reference, "opening", return_value={"vague_request": "Please fix it"}), \
             patch.object(run_reference, "build_simulator", return_value=(simulator, "public persona prompt")), \
             patch.object(run_reference, "TerminalAgent", return_value=agent), \
             patch.object(run_reference, "run_judge", return_value=audited_scores()), \
             patch("support.untracked_fingerprints", return_value={}), \
             patch("support.tracked_image_baseline", return_value={"tree": "image-tree", "base_commit": "base", "tracked_patch": "", "changed_paths": []}), \
             patch("support.observed_worktree_tree", return_value="worktree-tree"), \
             patch("support.export_worktree_prediction", side_effect=["", "diff", "diff", "diff"]), \
             patch("support.subprocess.run", side_effect=official):
            self.assertEqual(run_reference.main(), 0)
        episode = json.loads((out / "episode.json").read_text())
        self.assertEqual(episode["metrics"]["base_task_success"], 0)
        self.assertEqual(episode["metrics"]["evolved_requirement_success"]["met"], 1)
        self.assertEqual(episode["metrics"]["interaction_delivery"]["agent_turns_used"], 2)
        self.assertTrue(audit(out)["ready_for_review"])
        self.assertEqual((out / "task.private.jsonl").read_bytes(), task.read_bytes())
        from personas import initial_notice
        recorded_binding = json.loads((out / "binding.json").read_text())
        self.assertEqual(recorded_binding["task_persona_initial_notice"],
                         initial_notice("manager_no_code", recorded_binding))
        guard_calls = [call for call in llm.json_obj.call_args_list
                       if call.kwargs.get("schema_name") == "swe_message_guard"]
        opening_context = json.loads(guard_calls[0].args[0][1]["content"])
        self.assertEqual(opening_context["workspace_binding"]["task_persona_initial_notice"],
                         recorded_binding["task_persona_initial_notice"])


if __name__ == "__main__":
    unittest.main()
