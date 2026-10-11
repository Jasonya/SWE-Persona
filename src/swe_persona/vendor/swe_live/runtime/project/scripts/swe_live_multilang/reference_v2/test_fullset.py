import bootstrap
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from fullset_protocol import p20, task_time_status, fastest_selection, metric_summary, preliminary_inventory
from run_fullset import prepare, readiness, freeze_threshold, episode_command, report
from support import read, save, source_hashes, Manifest
from whole_sample import supervise_sample, existing_result
from execution_policy import Deadline, BudgetExpired


class QuantileTests(unittest.TestCase):
    def test_fastest_twenty_percent_has_exact_count_even_with_ties_and_failed_solutions(self):
        records = [{'task_key': f'{i:04d}', 'status': 'completed', 'seconds': 42,
                    'joint_success': int(i > 20)} for i in range(1, 101)]
        selection = fastest_selection(list(reversed(records)), p20(records))
        selected = sorted(r['task_key'] for r in selection if r['status'] == 'selected')
        self.assertEqual(selected, [f'{i:04d}' for i in range(1, 21)])

    def test_fastest_selection_retains_censoring_and_invalid_coverage(self):
        records = [{'task_key': f'{i:04d}', 'status': 'completed', 'seconds': i} for i in range(1, 31)]
        records += [{'task_key': f'{i:04d}', 'status': 'timeout', 'cap_seconds': 100} for i in range(31, 101)]
        records += [{'task_key': 'invalid', 'status': 'gold_blocked'}]
        result = fastest_selection(records, p20(records))
        self.assertEqual(sum(r['status'] == 'selected' for r in result), 20)
        self.assertEqual(sum(r['status'] == 'reference_invalid' for r in result), 1)

    def test_absolute_deadline_is_shared_across_phases(self):
        with patch("execution_policy.time.monotonic", return_value=100):
            interaction = Deadline(300, "sample", expires=150)
        with patch("execution_policy.time.monotonic", return_value=140):
            review = interaction
            self.assertEqual(review.remaining(), 10)
        with patch("execution_policy.time.monotonic", return_value=151):
            with self.assertRaises(BudgetExpired):
                review.remaining()
    def test_p20_comes_from_complete_sample_not_fixed_fifteen_minutes_or_success_filter(self):
        refs = [{"status": "completed", "seconds": i * 37, "joint_success": int(i % 2 == 0)}
                for i in range(1, 101)]
        self.assertEqual(p20(refs)["seconds"], 740)
        self.assertEqual(p20(list(reversed(refs))), p20(refs) | {"records_digest": p20(list(reversed(refs)))["records_digest"]})

    def test_right_censored_observations_remain_in_denominator(self):
        refs = [{"status": "completed", "seconds": i} for i in range(1, 31)]
        refs += [{"status": "timeout", "cap_seconds": 100} for _ in range(70)]
        estimate = p20(refs)
        self.assertEqual((estimate["seconds"], estimate["population"], estimate["right_censored"]), (20, 100, 70))

    def test_unidentified_quantile_fails_instead_of_using_timeout_ceiling(self):
        refs = [{"status": "completed", "seconds": 10}] + [{"status": "timeout", "cap_seconds": 20}] * 99
        with self.assertRaisesRegex(ValueError, "right-censored"):
            p20(refs)
        with self.assertRaisesRegex(ValueError, "not identified"):
            p20([{"status": "completed", "seconds": 100}] * 8 + [{"status": "timeout", "cap_seconds": 10}] * 2)

    def test_infra_errors_do_not_look_like_fast_completions(self):
        refs = [{"status": "completed", "seconds": n} for n in range(100, 110)]
        refs += [{"status": "evaluation_invalid", "seconds": .01}] * 90
        result = p20(refs)
        self.assertEqual(result["seconds"], 101)
        self.assertEqual(result["excluded_invalid"], 90)
        self.assertEqual(result["coverage"], .1)

    def test_screening_is_not_a_measured_timeout_and_ties_are_preserved(self):
        self.assertEqual(task_time_status({"status": "completed", "seconds": 20}, 20), "selected")
        self.assertEqual(task_time_status({"status": "completed", "seconds": 21}, 20), "excluded_time_screen")
        self.assertEqual(task_time_status({"status": "timeout"}, 20, False), "selected")
        self.assertEqual(task_time_status({"status": "gold_blocked"}, 20), "reference_invalid")

    def test_timeouts_count_as_failures_but_infra_and_na_are_not_relabelled(self):
        result = metric_summary([{"status": "completed", "joint_success": 1},
                                 {"status": "completed", "joint_success": 0},
                                 {"status": "timeout", "joint_success": 0},
                                 {"status": "evaluation_invalid"}, {"status": "not_applicable"}])
        self.assertEqual(result["joint_rate_including_timeouts"], 1 / 3)
        self.assertEqual(result["scored_including_timeouts"], 3)

    def test_time_screen_policy_zeroes_remain_in_primary_denominator(self):
        result = metric_summary([{"status": "completed", "joint_success": 1},
                                 {"status": "excluded_time_screen"}, {"status": "not_applicable"}])
        self.assertEqual(result["joint_rate_including_timeouts"], .5)
        self.assertEqual(result["joint_rate_among_executed"], 1)
        self.assertEqual(result["time_screen_policy_failures"], 1)
        self.assertEqual(result["executed_scored"], 1)


class FullsetTests(unittest.TestCase):
    def test_legal_unicode_line_separators_inside_jsonl_fields_do_not_split_records(self):
        with TemporaryDirectory() as tmp:
            row = self.rows()[0][1] | {"problem_statement": "public\u2028issue\u0085text\u2029end"}
            task = Path(tmp) / "task.jsonl"
            task.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
            self.assertEqual(Manifest(task).instruction, row["problem_statement"])

    def rows(self):
        return [("c", {"instance_id": "owner__repo-1", "problem_statement": "Repair the public cap behavior",
                       "base_commit": "abcdef", "docker_image": "fixture-image"})]

    def test_inventory_retains_all_ten_and_does_not_disguise_missing_binders_as_na(self):
        items = preliminary_inventory({"key": "0001-c", "instance_id": "owner__repo-1"})
        self.assertEqual(len(items), 10)
        self.assertEqual(sum(x["status"] == "binding_required" for x in items), 8)
        self.assertFalse(any(x["status"] == "not_applicable" for x in items))

    def test_freeze_resume_and_threshold_provenance(self):
        with TemporaryDirectory() as tmp:
            study = Path(tmp)
            config = {"expected_tasks": 1, "prefilter": True}
            manifest = prepare(study, config, self.rows())
            self.assertEqual(prepare(study, config, []), manifest)
            ready = readiness(study, manifest)
            self.assertFalse(ready["final_queue_ready"])
            self.assertEqual(ready["validated_personas"], [])
            self.assertEqual(len(ready["qualification_errors"]), 10)
            refs = [{"task_key": "0001-c", "status": "completed", "seconds": 71.12345}]
            threshold, selection = freeze_threshold(study, manifest, refs)
            self.assertEqual(threshold["seconds"], 71.12345)
            self.assertEqual(selection[0]["status"], "selected")
            with self.assertRaisesRegex(ValueError, "changed after threshold"):
                freeze_threshold(study, manifest, [dict(refs[0], seconds=72)])
            summary = report(study, manifest)
            self.assertFalse(summary["publication_ready"])
            self.assertEqual(summary["planned_pair_arm_cells"], 30)
            changed = read(study / "inventory.json")
            changed[0]["status"] = "applicable"
            save(study / "inventory.json", changed)
            with self.assertRaisesRegex(ValueError, "inventory modified"):
                prepare(study, config, [])

    def test_full_dataset_count_is_asserted_not_balanced_twenty_task_sampling(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "1077 tasks, found 1"):
                prepare(Path(tmp), {"expected_tasks": 1077}, self.rows())

    def test_threshold_cannot_freeze_from_a_partial_or_duplicated_population(self):
        with TemporaryDirectory() as tmp:
            study = Path(tmp)
            manifest = prepare(study, {'expected_tasks': 1, 'prefilter': True}, self.rows())
            record = {'task_key': '0001-c', 'status': 'completed', 'seconds': 71}
            for records in ([], [record, record], [dict(record, task_key='other')]):
                with self.assertRaisesRegex(ValueError, 'every frozen task'):
                    freeze_threshold(study, manifest, records)

    def test_development_overlap_is_reported_without_dropping_dataset_rows(self):
        with TemporaryDirectory() as tmp:
            study = Path(tmp)
            manifest = prepare(study, {"expected_tasks": 1}, self.rows())
            manifest['adapter_qualification_task_ids'] = ['owner__repo-1']
            summary = report(study, manifest)
            self.assertEqual(summary['planned_pair_arm_cells'], 30)
            subsets = summary['by_development_overlap']
            for arm in ('persona', 'no_evolution', 'fixed_schedule'):
                self.assertEqual(subsets['qualification_seed_tasks'][arm]['candidate_count'], 10)
                self.assertEqual(subsets['remaining_tasks'][arm]['candidate_count'], 0)
                self.assertEqual(summary['by_arm'][arm]['candidate_count'], 10)
            self.assertTrue(all(row['adapter_qualification_overlap'] for row in read(study / 'results.json')))

    def test_marking_every_pair_na_cannot_satisfy_ten_persona_qualification(self):
        with TemporaryDirectory() as tmp:
            study = Path(tmp)
            manifest = prepare(study, {"expected_tasks": 1}, self.rows())
            inventory = read(study / "inventory.json")
            for pair in inventory:
                pair["status"] = "not_applicable"
            save(study / "inventory.json", inventory)
            result = readiness(study, manifest)
            self.assertFalse(result["final_queue_ready"])
            self.assertEqual(result["missing_binding_personas"], [])
            self.assertEqual(len(result["qualification_errors"]), 10)

    def test_matched_controls_have_one_shared_deadline_and_opening(self):
        task = {"key": "0001-c", "path": "task.jsonl"}
        args = episode_command(Path("study"), task, "model", "fixed_schedule", "manager_no_code", Path("opening.json"))
        self.assertIn("--fixed-schedule", args)
        self.assertEqual(args[args.index("--interaction-seconds") + 1], "0")
        self.assertEqual(args[args.index("--verification-seconds") + 1], "0")
        self.assertEqual(args[args.index("--opening-file") + 1], "opening.json")


class SupervisorTests(unittest.TestCase):
    def test_real_subprocess_deadline_and_resume_do_not_rerun_expensive_sample(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp) / "run"
            called = []
            result = supervise_sample([sys.executable, "-c", "import time; time.sleep(10)"], directory, .2,
                                      cleanup=lambda owner: called.append(owner) or [])
            self.assertEqual(result["status"], "timeout")
            self.assertEqual(result["joint_success"], 0)
            self.assertIsNone(result["base_task_success"])
            self.assertFalse(result["completion_within_budget"])
            self.assertEqual(len(called), 1)
            self.assertLess(result["seconds"], 5)
            self.assertEqual(existing_result(directory), result)

    def test_fast_crash_is_invalid_not_fast_reference(self):
        with TemporaryDirectory() as tmp:
            result = supervise_sample([sys.executable, "-c", "raise SystemExit(2)"], Path(tmp) / "run", 10,
                                      cleanup=lambda owner: [])
            self.assertEqual(result["status"], "evaluation_invalid")
            self.assertIsNone(result["joint_success"])

    def test_cleanup_failure_stops_future_jobs(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp) / "run"
            def fail(owner):
                raise RuntimeError("Docker unavailable")
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                supervise_sample([sys.executable, "-c", "pass"], directory, 10, cleanup=fail)
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                existing_result(directory)


if __name__ == "__main__":
    unittest.main()
