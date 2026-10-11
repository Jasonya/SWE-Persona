import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from task_adapter import public_view, read_task
from run_baseline import AgentContainer, BoundedLlm, BudgetExhausted


class PilotBoundaryTests(unittest.TestCase):
    def test_unknown_and_hidden_columns_never_enter_public_view(self):
        task = {"instance_id": "example__repo-1", "problem_statement": "Fix the public behavior.",
                "base_commit": "abc", "docker_image": "image",
                "patch": "SECRET_GOLD", "test_patch": "SECRET_TEST",
                "hints_text": "SECRET_HINT", "FAIL_TO_PASS": ["SECRET_CASE"],
                "new_future_answer_column": {"nested": "SECRET_FUTURE"}}
        public = public_view(task)
        self.assertEqual(set(public), {"instance_id", "problem_statement", "environment"})
        self.assertNotIn("SECRET", json.dumps(public))
        public["environment"]["memory"] = "1g"
        self.assertEqual(public_view(task)["environment"]["memory"], "16g")

    def test_multiple_task_rows_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.jsonl"
            path.write_text('{}\n{}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exactly one"):
                read_task(path)

    def test_call_budget_counts_attempts_and_disables_hidden_retries(self):
        client = Mock()
        client.text.return_value = "reply"
        bounded = BoundedLlm(client, calls=1, seconds=60)
        self.assertEqual(bounded.text([], role="test"), "reply")
        client.text.assert_called_once_with([], attempts=1, role="test")
        with self.assertRaises(BudgetExhausted):
            bounded.text([])
        self.assertEqual(client.text.call_count, 1)

    def test_expired_budget_never_calls_provider(self):
        client = Mock()
        bounded = BoundedLlm(client, calls=1, seconds=-1)
        with self.assertRaises(BudgetExhausted):
            bounded.text([])
        client.text.assert_not_called()

    def test_container_has_no_host_mounts_network_or_secrets(self):
        client = Mock()
        runtime = AgentContainer(client, "sha256:pinned")
        kwargs = client.containers.run.call_args.kwargs
        self.assertEqual(kwargs["network_mode"], "none")
        self.assertNotIn("volumes", kwargs)
        self.assertNotIn("mounts", kwargs)
        self.assertNotIn("environment", kwargs)
        runtime.remove()
        client.containers.run.return_value.remove.assert_called_once_with(force=True)


if __name__ == "__main__":
    unittest.main()
