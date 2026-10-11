import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from run_batch import aggregate, select_task, main, latest_attempt
from run_persona import run_control


class BatchTests(unittest.TestCase):
    def test_batch_runs_gold_then_three_arms_and_resume_skips_valid_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "task.jsonl").write_text(json.dumps({"instance_id":"x","problem_statement":"public",
                "base_commit":"base","docker_image":"image"}) + "\n")
            manifest = {"tasks":[{"language":"go","instance_id":"x","path":"task.jsonl","sha256":"frozen"}]}
            client = Mock()
            client.info.return_value = {"OSType":"linux"}
            client.images.get.return_value.id = "image-id"
            commands = []
            def execute(command, log):
                commands.append(command)
                flag = "--output_dir" if "--patch_dir" in command else "--output-dir"
                output = Path(command[command.index(flag) + 1])
                output.mkdir(parents=True)
                if flag == "--output_dir":
                    (output / "results.json").write_text(json.dumps({"submitted":1,"success":1,"error":0,"incomplete":0}))
                else:
                    (output / "summary.json").write_text(json.dumps({"evaluation_valid":True,"base_task_success":False}))
                return 0
            with patch("sys.argv", ["batch", "--run", "--batch-dir", str(root)]), \
                 patch("run_batch.prepare", return_value=manifest), patch("run_batch.execute", side_effect=execute), \
                 patch("llm.load_local_env"), patch.dict("os.environ", {"AZURE_OPENAI_API_KEY":"offline-test"}), \
                 patch("docker.from_env", return_value=client):
                main()
                self.assertEqual(len(commands), 4)
                self.assertIn("gold", commands[0])
                self.assertEqual([c[c.index("--arm")+1] for c in commands[1:]],
                                 ["single_turn","interactive_no_evolution","persona"])
                main()
                self.assertEqual(len(commands), 4)
            self.assertFalse((root / "batch.lock").exists())

    def test_selection_is_independent_of_order_and_hidden_fields(self):
        rows = [{"instance_id":"a", "patch":"one"}, {"instance_id":"b", "patch":"two"}]
        selected = select_task(rows, "go")["instance_id"]
        self.assertEqual(select_task(list(reversed(rows)), "go")["instance_id"], selected)
        self.assertEqual(select_task([dict(r, patch="changed") for r in rows], "go")["instance_id"], selected)

    def test_aggregate_retains_failure_and_missing_arms(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp)
            run = batch / "episodes/go/persona/attempt-001"
            run.mkdir(parents=True)
            (run / "summary.json").write_text(json.dumps({"evaluation_valid":True,"joint_success":False}))
            (run.parent / "attempt-001.log").write_text("log must not be treated as an attempt")
            (run.parent / "attempt-001.watchdog.json").write_text("{}")
            self.assertEqual(latest_attempt(run.parent),run)
            rows = aggregate(batch, {"tasks":[{"language":"go","instance_id":"x"}]})
            self.assertEqual(len(rows), 3)
            self.assertFalse(rows[2]["joint_success"])
            self.assertTrue(rows[2]["evaluation_valid"])
            self.assertIsNone(rows[0]["evaluation_valid"])

    def control(self, arm):
        agent = Mock(history=[])
        replies = []
        for status in ("working", "done"):
            reply = Mock(status=status)
            reply.to_json.return_value = {"status":status}
            replies.append(reply)
        agent.run_turn.side_effect = replies
        with tempfile.TemporaryDirectory() as tmp, \
             patch("run_persona.TerminalAgent", return_value=agent), \
             patch("run_persona.untracked_fingerprints", return_value={}), \
             patch("run_persona.export_prediction", return_value="diff"):
            episode = run_control(Mock(), Mock(), {"base_commit":"base"}, {"environment":{}},
                                  Path(tmp), "identical original issue", arm, 4, 6)
        return agent, episode

    def test_single_turn_gets_total_shell_budget(self):
        agent, episode = self.control("single_turn")
        self.assertEqual(agent.run_turn.call_count, 1)
        self.assertEqual(agent.run_turn.call_args.kwargs["step_budget"], 24)
        self.assertFalse(episode["disclosed"])

    def test_interactive_control_continues_without_persona(self):
        agent, episode = self.control("interactive_no_evolution")
        self.assertEqual(agent.run_turn.call_count, 2)
        self.assertEqual(agent.run_turn.call_args.kwargs["step_budget"], 6)
        self.assertNotIn("three-point", agent.run_turn.call_args.args[0])
        self.assertTrue(episode["model_done"])
        self.assertEqual(episode["checks"], [])
