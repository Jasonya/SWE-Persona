import bootstrap
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from message_guard import MessageGuard, coverage_errors
from support import digest
from run_reference import dialogue


class MessageGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = Mock(assert_no_leak=Mock())
        self.manifest.public_view.return_value = {"original_instruction": "Remove legacy transport"}
        self.binding = {"validation_catalog": "local tests only", "delivery_boundary": "source and tests editable",
                        "initial_untracked_sha256": {"cache": "hash"},
                        "initial_tracked_patch": {"changed_paths": []}}
        self.llm = Mock(model="test")
        self.llm.json_obj.return_value = dict(task_conflict=False, unauthorized_requirement=False,
                                             unsupported_fact=False, requests_work=False, reason="Acknowledgement")
        self.guard = MessageGuard(self.llm, self.manifest, self.binding, self.root)

    def test_closing_work_request_is_rejected_but_allowed_as_feedback(self):
        self.llm.json_obj.return_value.update(requests_work=True, reason="Complete pending verification")
        text = "Complete pending verification before handing the patch to final review."
        self.assertTrue(self.guard.check(text, 3, "end"))
        self.assertFalse(self.guard.check(text, 3, "feedback"))
        self.assertEqual([r["accepted"] for r in self.guard.receipts], [False, True])

    def test_scope_conflict_blocks_delivery_and_receipt_is_bound_to_exact_message(self):
        self.llm.json_obj.return_value.update(task_conflict=True, reason="without changing the tests")
        text = "Remove legacy APIs without changing the tests."
        self.assertTrue(self.guard.check(text, 2, "feedback"))
        transcript = [{"role": "user", "turn": 2, "message_id": "u-2", "text": text}]
        self.assertTrue(coverage_errors(transcript, self.guard.receipts))
        self.llm.json_obj.return_value.update(task_conflict=False)
        self.guard.check("Preserve official acceptance; update local tests as needed.", 2, "feedback")
        self.assertTrue(coverage_errors(transcript, self.guard.receipts))

    def test_transport_failure_cannot_be_used_as_permission(self):
        self.llm.json_obj.side_effect = RuntimeError("unavailable")
        with self.assertRaises(RuntimeError):
            self.guard.check("Thanks", 2, "end")
        saved = json.loads((self.root / "message_guard.json").read_text())
        self.assertFalse(saved[0]["accepted"])
        self.assertEqual(saved[0]["error_type"], "RuntimeError")

    def test_public_context_only_and_accepted_closing_coverage(self):
        text = "Thanks; this is pending final review."
        self.assertFalse(self.guard.check(text, 3, "end"))
        payload = json.loads(self.llm.json_obj.call_args.args[0][1]["content"])
        self.assertEqual(payload["public_contract"], self.manifest.public_view())
        self.assertNotIn("initial_untracked_sha256", payload["workspace_binding"])
        transcript = [{"role": "user", "turn": 3, "message_id": "u-3", "text": text, "closing": True}]
        self.assertEqual(coverage_errors(transcript, self.guard.receipts), [])
        self.guard.receipts[0]["result"]["requests_work"] = True
        self.assertTrue(coverage_errors(transcript, self.guard.receipts))

    def test_registered_persona_notice_is_in_opening_context(self):
        notice = "This is my first PR. Please take one small step at a time."
        self.binding["task_persona_initial_notice"] = notice
        guard = MessageGuard(self.llm, self.manifest, self.binding, self.root)
        self.assertFalse(guard.check(notice + " Please fix the public issue.", 1, "opening"))
        payload = json.loads(self.llm.json_obj.call_args.args[0][1]["content"])
        self.assertEqual(payload["workspace_binding"]["task_persona_initial_notice"], notice)
        self.assertIsNone(payload["observation"])

    def test_rejected_end_is_regenerated_without_extra_agent_turn(self):
        controller = Mock(max_turns=8, fired={"first_patch", "approach_visible", "completion_claim"},
                          initial_digest="initial", invalid_reasons=[], infra_failures=[])
        controller.snapshot.return_value = ("image", "changed", True)
        controller.checkers.snapshots.items = {"image": {"listing": []}}
        controller.ledger.active.return_value = []
        controller.run_probes.return_value = []
        controller.verify_active.return_value = []
        controller.build_observation.return_value = {"requirement_to_disclose": None}
        controller.validate_decision.return_value = []
        controller.apply_decision.return_value = []
        reply = SimpleNamespace(message_id="a-1", status="done", reply="Patch ready", steps=[], to_json=lambda: {})
        agent = Mock(history=[], container=SimpleNamespace())
        agent.run_turn.return_value = reply
        simulator = Mock()
        simulator.decide.side_effect = [
            {"action": "end", "user_message": "Run more tests", "evidence_refs": []},
            {"action": "end", "user_message": "Thanks; pending final review", "evidence_refs": []}]
        guard = Mock()
        guard.check.side_effect = [["end requests work"], []]
        transcript, turns, error = dialogue(agent, simulator, controller, self.manifest,
                                            "Public request", self.root, "interactive", guard)
        self.assertIsNone(error)
        self.assertEqual(turns, 1)
        self.assertEqual(agent.run_turn.call_count, 1)
        self.assertEqual(simulator.decide.call_count, 2)
        self.assertNotIn("Run more tests", [m["text"] for m in transcript])
        self.assertEqual(transcript[-1]["text"], "Thanks; pending final review")


if __name__ == "__main__":
    unittest.main()
