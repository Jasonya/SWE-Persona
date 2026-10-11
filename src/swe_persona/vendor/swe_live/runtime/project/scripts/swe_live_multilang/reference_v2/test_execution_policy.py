import bootstrap
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import time
import unittest
from unittest.mock import Mock, patch

from execution_policy import Deadline, BudgetExpired, BudgetClient, IndependentSamples


class PolicyTests(unittest.TestCase):
    def test_expired_budget_makes_zero_provider_calls(self):
        provider = Mock(model="test")
        client = BudgetClient(provider)
        client.deadline = Deadline(1, "interaction")
        client.deadline.expires = time.monotonic() - 1
        with self.assertRaises(BudgetExpired):
            client.text([])
        provider.text.assert_not_called()

    def test_retry_count_and_remaining_time_are_not_multiplied(self):
        provider = Mock(model="test")
        provider.text.side_effect = RuntimeError("transport unavailable")
        client = BudgetClient(provider, request_seconds=90)
        client.deadline = Deadline(10, "interaction")
        with patch("execution_policy.time.sleep"), self.assertRaises(RuntimeError):
            client.text([], role="agent.turn1")
        self.assertEqual(provider.text.call_count, 2)
        self.assertEqual(provider.text.call_args.kwargs["attempts"], 1)
        self.assertLessEqual(provider._client.with_options.call_args.kwargs["timeout"], 10)
        self.assertEqual(provider._client.with_options.call_args.kwargs["max_retries"], 0)

    def test_schema_error_does_not_start_nested_repairs(self):
        from llm import SchemaRepairFailed
        provider = Mock(model="test")
        provider.json_obj.side_effect = SchemaRepairFailed("test", "schema", ["invalid"])
        client = BudgetClient(provider)
        with self.assertRaises(SchemaRepairFailed):
            client.json_obj([], {}, role="rubric.sample1")
        self.assertEqual(provider.json_obj.call_count, 1)
        self.assertEqual(provider.json_obj.call_args.kwargs["repairs"], 0)

    def test_three_independent_samples_overlap_but_keep_vote_order(self):
        barrier = Barrier(3)
        provider = Mock()
        def answer(*args, **kwargs):
            barrier.wait(timeout=2)  # Sequential execution would deadlock/fail.
            return {"sample": kwargs["role"]}
        provider.json_obj.side_effect = answer
        with IndependentSamples(provider) as client:
            results = [client.json_obj([], {}, role=f"rubric.req.sample{i}") for i in (1, 2, 3)]
        self.assertEqual([r["sample"] for r in results], [f"rubric.req.sample{i}" for i in (1, 2, 3)])
        self.assertEqual(provider.json_obj.call_count, 3)


if __name__ == "__main__":
    unittest.main()
