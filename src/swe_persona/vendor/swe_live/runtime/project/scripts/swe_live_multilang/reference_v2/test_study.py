import bootstrap
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from support import candidates, digest, save, command_evidence, file_hash
from personas import PERSONAS, binding_record, initial_notice
from calibration import gate_identity, case_passed, gate_location
from run_study import select_tasks, LANGUAGES, verification_budget, prepare
from report_study import collect, report


class StudyTests(unittest.TestCase):
    def test_recent_test_evidence_survives_large_early_output_and_failures_stay_indexed(self):
        with TemporaryDirectory() as tmp:
            commands = [{"command": "inspect source", "exit_code": 0, "stdout": "early output " * 50000, "stderr": ""}]
            commands += [{"command": "old failing test", "exit_code": 1, "stdout": "EARLY_FAILURE_EVIDENCE", "stderr": "old diagnostic"}]
            commands += [{"command": "inspect more", "exit_code": 0, "stdout": "data", "stderr": ""}] * 30
            commands += [{"command": "run regression", "exit_code": 0, "stdout": "LATEST_REGRESSION_PASSED", "stderr": ""}]
            files = command_evidence(tmp, commands)
            rendered = "".join(f["text"] for f in files)
            self.assertIn("LATEST_REGRESSION_PASSED", rendered)
            self.assertIn("EARLY_FAILURE_EVIDENCE", rendered)
            self.assertIn("old diagnostic", rendered)
            index = json.loads(files[0]["text"])["commands"]
            self.assertEqual(len(index), len(commands))
            self.assertEqual(index[1]["exit_code"], 1)
            self.assertEqual(index[1]["command"], "old failing test")
            self.assertLess(len(rendered), 140000)
            for file in files:
                self.assertEqual(file["sha256"], file_hash(Path(tmp) / Path(file["path"]).name))

    def test_transport_error_cannot_pass_negative_calibration_case(self):
        self.assertFalse(case_passed("unknown", "checker_error", ["fail", "unknown"]))
        self.assertTrue(case_passed("unknown", "rubric_missing_grounding", ["fail", "unknown"]))

    def test_successful_gate_retry_is_reused_without_overwriting_failed_evidence(self):
        with TemporaryDirectory() as tmp:
            first, cached = gate_location(tmp, "test")
            first.mkdir(parents=True)
            save(first / "report.json", {"gate_passed": False, "identity": gate_identity("test")})
            retry, cached = gate_location(tmp, "test")
            self.assertFalse(cached)
            self.assertNotEqual(first, retry)
            retry.mkdir()
            save(retry / "report.json", {"gate_passed": True, "identity": gate_identity("test")})
            found, cached = gate_location(tmp, "test")
            self.assertTrue(cached)
            self.assertEqual(found, retry)
            self.assertFalse(json.loads((first / "report.json").read_text())["gate_passed"])

    def test_balanced_selection_is_fixed_and_does_not_look_at_hidden_solutions(self):
        splits = {lang: [{"instance_id": f"owner__{lang}-{i}", "patch": str(i)} for i in range(8)] for lang in LANGUAGES}
        first = [(l, r["instance_id"]) for l, r in select_tasks(splits, 20, "fixed")]
        for rows in splits.values():
            rows.reverse()
            for row in rows:
                row["patch"] = "different hidden solution"
        second = [(l, r["instance_id"]) for l, r in select_tasks(splits, 20, "fixed")]
        self.assertEqual(first, second)
        self.assertEqual(len(set(first)), 20)
        self.assertEqual([sum(l == lang for l, _ in first) for lang in LANGUAGES], [3, 3, 3, 3, 2, 2, 2, 2])

    def test_personas_have_distinct_verbatim_rubrics_and_gate_identities(self):
        for persona in PERSONAS:
            pool, candidate, coverage = candidates(persona)
            template = next(t for t in pool["attributes"]["evolved_requirements"] if t["id"] == candidate.template_id)
            self.assertEqual(candidate.template["satisfaction"], template["satisfaction"])
            self.assertNotIn("{{", candidate.statement())
            self.assertEqual(candidate.checker["parameters"]["samples"], 3)
            self.assertEqual(sum(c["status"] == "bound_pending_validation" for c in coverage), 1)
        self.assertNotEqual(gate_identity("test", PERSONAS[0]), gate_identity("test", PERSONAS[1]))

    def test_pr_initial_notice_discloses_bound_permissions_without_handoff_early(self):
        notice = initial_notice(PERSONAS[1], {"initial_untracked_sha256": {"existing": "hash"},
                                            "initial_tracked_patch": {"changed_paths": []}})
        self.assertIn("1 existing untracked", notice)
        self.assertIn("no permission to commit", notice)
        self.assertNotIn("reviewable PR draft", notice)
        self.assertNotIn("PR draft", notice)

    def test_pr_initial_notice_reports_dirty_image_without_claiming_it_is_clean(self):
        notice = initial_notice(PERSONAS[1], {"initial_untracked_sha256": {},
            "initial_tracked_patch": {"changed_paths": ["package-lock.json"]}})
        self.assertIn("tracked changes in 1 files", notice)
        self.assertNotIn("tracked diff is empty", notice)
        self.assertNotIn("PR draft", notice)

    def test_pr_simulator_uses_observed_workspace_not_an_empty_placeholder(self):
        from run_reference import build_simulator
        from types import SimpleNamespace
        manifest = SimpleNamespace(task_id="task", environment={}, assert_no_leak=Mock(),
            public_view=lambda: {"source": "public", "frozen_contract": {}})
        binding = {"initial_untracked_sha256": {"existing.txt": "observed-hash"}}
        with patch("run_reference.render_system_prompt", return_value="public prompt") as render:
            build_simulator(Mock(), manifest, [], 8, PERSONAS[1], binding)
            self.assertEqual(render.call_args.args[2]["permissions_and_validation"], binding)
        with self.assertRaises(ValueError):
            build_simulator(Mock(), manifest, [], 8, PERSONAS[1])

    def test_review_budget_frozen_from_gold_not_model_success(self):
        self.assertEqual(verification_budget(60, 5400), 900)
        self.assertEqual(verification_budget(2400, 5400), 3780)
        self.assertEqual(verification_budget(5000, 5400), 5400)

    def test_unknown_is_not_zero_and_all_planned_cells_are_reported(self):
        with TemporaryDirectory() as tmp:
            study = Path(tmp)
            manifest = {"tasks": [{"key": "01-js", "language": "js", "instance_id": "owner__repo-1"}]}
            rows = collect(study, manifest)
            self.assertEqual(len(rows[0]["cells"]), 3)
            self.assertTrue(all(c["base_success"] is None for c in rows[0]["cells"]))
            self.assertTrue(all(not c["valid"] for c in rows[0]["cells"]))

    def test_real_figures_and_html_render_for_incomplete_trajectory(self):
        with TemporaryDirectory() as tmp:
            study = Path(tmp)
            save(study / "manifest.json", {"tasks": [{"key": "01-js", "language": "js", "instance_id": "owner__repo-1"}]})
            run = study / "episodes/01-js/persona1/attempt-001"
            run.mkdir(parents=True)
            save(run / "transcript.json", [{"role": "user", "message_id": "u-1", "turn": 1, "text": "<script>not executable</script>"},
                {"role": "agent", "message_id": "a-1", "turn": 1, "text": "Investigating the issue", "status": "working", "seconds": 3.2, "shell_steps": 1}])
            summary = report(study)
            self.assertEqual(summary["planned"], 3)
            self.assertEqual(summary["attempted"], 1)
            self.assertGreater((study / "reports/summary.png").stat().st_size, 1000)
            transcript = (study / "reports/trajectories/01-js.html").read_text(encoding="utf-8")
            self.assertNotIn("<script>", transcript)
            self.assertIn("&lt;script&gt;", transcript)

    def test_batch_resume_does_not_repeat_paid_episodes_or_replace_failed_tasks(self):
        import run_study
        with TemporaryDirectory() as tmp:
            study = Path(tmp)
            task = study / "task.private.jsonl"
            task.write_text(json.dumps({"instance_id": "owner__repo-1", "problem_statement": "Fix public issue",
                                       "base_commit": "base", "docker_image": "image"}) + "\n")
            from support import source_hashes
            manifest = {"sources": source_hashes(), "tasks": [{"key": "01-js", "language": "js", "instance_id": "owner__repo-1", "path": task.name}]}
            client = Mock()
            client.info.return_value = {"OSType": "linux", "MemTotal": 24 * 1024**3}
            client.images.get.return_value.id = "image-id"
            calls = []
            def execute(command, log):
                calls.append(command)
                Path(command[command.index("--output-dir") + 1]).mkdir(parents=True)
                return 0
            argv = ["run_study", "--study-dir", str(study), "--run", "--tasks", "1"]
            with patch("sys.argv", argv), patch("run_study.prepare", return_value=manifest), \
                 patch("docker.from_env", return_value=client), patch("run_study.ensure_gates"), \
                 patch("run_study.ensure_gold", return_value=50), patch("run_study.execute", side_effect=execute), \
                 patch("run_study.can_skip", return_value=True) as completed, patch("report_study.report", return_value={"ready_for_review": 3}):
                self.assertEqual(run_study.main(), 0)
                self.assertEqual(run_study.main(), 0)
                completed.return_value = False
                argv.extend(["--retry-episode", "01-js/persona0"])
                self.assertEqual(run_study.main(), 0)
            self.assertEqual(len(calls), 4)
            self.assertEqual([c[c.index("--arm") + 1] for c in calls], ["single_turn", "interactive", "interactive", "interactive"])
            self.assertIn("persona0", calls[-1][calls[-1].index("--output-dir") + 1])
            self.assertEqual({c[c.index("--verification-seconds") + 1] for c in calls}, {"900"})
            self.assertFalse((study / "study.lock").exists())

    def test_frozen_configuration_rejects_source_changes(self):
        with TemporaryDirectory() as tmp:
            study = Path(tmp)
            from support import PROTOCOL
            save(study / "manifest.json", {"protocol": PROTOCOL, "config": {"tasks": 20}, "sources": {"old": "hash"}})
            with self.assertRaisesRegex(ValueError, "Production source changed"):
                prepare(study, {"tasks": 20})

    def test_diagnostic_subset_preserves_source_rows_and_rejects_changed_inputs(self):
        from support import REVISION
        with TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "old", Path(tmp) / "new"
            source.mkdir(); target.mkdir()
            row = {"instance_id": "owner__repo-1", "problem_statement": "Public issue",
                   "base_commit": "commit", "docker_image": "image"}
            task = source / "task.private.jsonl"
            task.write_text(json.dumps(row) + "\n")
            save(source / "manifest.json", {"dataset_revision": REVISION, "comparison": "diagnostic",
                "tasks": [{"key": "03-c", "language": "c", "instance_id": row["instance_id"],
                           "path": task.name, "sha256": file_hash(task)}]})
            config = {"tasks": 1, "diagnostic_selection": {"source_study": str(source),
                      "manifest_sha256": file_hash(source / "manifest.json"), "task_keys": ["03-c"]}}
            original = task.read_bytes()
            manifest = prepare(target, config)
            self.assertEqual(manifest["tasks"][0]["key"], "03-c")
            self.assertEqual((target / task.name).read_bytes(), original)
            self.assertEqual(task.read_bytes(), original)
            self.assertEqual(prepare(target, config), manifest)
            (target / task.name).write_text("changed")
            with self.assertRaisesRegex(ValueError, "Frozen task changed"):
                prepare(target, config)


if __name__ == "__main__":
    unittest.main()
