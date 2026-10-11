"""Offline regressions for the shared client used by the persona runner."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import run_baseline  # Establish the team's client import path.
from llm import CallLog, Llm, SchemaRepairFailed, parse_json


class ClientContractTests(unittest.TestCase):
    def client(self, replies):
        client = Llm.__new__(Llm)
        client.model = "offline-test"
        client.spec = SimpleNamespace(enforces_schema=True)
        client.log = CallLog()
        client._call = Mock(side_effect=[(reply, None) for reply in replies])
        return client

    def test_json_citations_preserve_embedded_fences(self):
        payload = {"quote": "```sh\nmake check\n```", "other": "```json\n{}\n```"}
        encoded = json.dumps(payload)
        for text in (encoded, "```json\n" + encoded + "\n```", "Result: " + encoded):
            with self.subTest(text=text):
                self.assertEqual(parse_json(text), payload)

    def test_zero_repairs_means_one_provider_call(self):
        client = self.client(["invalid", '{}'])
        with self.assertRaises(SchemaRepairFailed):
            client.json_obj([{"role":"user", "content":"test"}], {"type":"object"}, repairs=0)
        self.assertEqual(client._call.call_count, 1)

    def test_one_repair_allows_exactly_two_calls(self):
        client = self.client(["invalid", '{}'])
        self.assertEqual(client.json_obj([{"role":"user", "content":"test"}],
                                         {"type":"object"}, repairs=1), {})
        self.assertEqual(client._call.call_count, 2)

    def test_validation_events_do_not_count_as_requests(self):
        log = CallLog()
        log.add({"model":"test", "input_tokens":10, "output_tokens":2})
        log.add({"model":"test", "schema_repair_round":1})
        log.add({"model":"test", "error":"connection failed"})
        self.assertEqual(log.totals()["calls"], 2)
        self.assertEqual(log.totals()["input_tokens"], 10)

    def test_empty_response_is_not_double_counted(self):
        client = Llm.__new__(Llm)
        client.model = "offline-test"
        client.spec = SimpleNamespace(provider="azure_responses")
        client.log = CallLog()
        client._azure_responses = Mock(return_value=("", (10, 2), {}))
        with self.assertRaises(RuntimeError):
            client.text([], attempts=1)
        self.assertEqual(client.log.totals()["calls"], 1)
        self.assertEqual(client.log.totals()["output_tokens"], 2)


if __name__ == "__main__":
    unittest.main()
