import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from evaluation_runtime import (Timeline, install_runtime_adapters, cleanup_owned, supervise, OWNER_LABEL)


class EvaluationRuntimeTests(unittest.TestCase):
    def test_seconds_are_converted_once_and_cleanup_does_not_prune(self):
        class Runtime:
            def __init__(self, container, command_timeout):
                self.container, self.command_timeout = container, command_timeout
            def send_command(self, command, timeout=None):
                self.effective_seconds = self.command_timeout * 60 if timeout is None else timeout * 60
                return SimpleNamespace(metadata=SimpleNamespace(exit_code=0), output="test output")
        with tempfile.TemporaryDirectory() as tmp:
            install_runtime_adapters(Runtime, 9000, Timeline(Path(tmp) / "events.jsonl"))
            container = Mock(id="owned")
            runtime = Runtime(container, command_timeout=9000)
            runtime.send_command("true")
            self.assertEqual(runtime.effective_seconds, 9000)
            self.assertEqual(runtime.command_timeout, 150)
            runtime.send_command("true", timeout=2)
            self.assertEqual(runtime.effective_seconds, 120)
            runtime.cleanup()
            runtime.cleanup()
            container.remove.assert_called_once_with(force=True)
            container.stop.assert_not_called()

    def test_command_timeout_is_an_error_not_test_failure(self):
        class Runtime:
            def __init__(self, container, command_timeout):
                self.container = container
            def send_command(self, *args):
                return SimpleNamespace(metadata=SimpleNamespace(exit_code=124), output="partial tests")
        with tempfile.TemporaryDirectory() as tmp:
            install_runtime_adapters(Runtime, 1, Timeline(Path(tmp) / "events.jsonl"))
            runtime = Runtime(Mock(id="owned"), 9000)
            with self.assertRaises(TimeoutError):
                runtime.send_command("slow test")

    def test_cleanup_checks_exact_owner_label(self):
        ours = Mock(id="owned", labels={OWNER_LABEL: "job"})
        unrelated = Mock(id="unrelated", labels={OWNER_LABEL: "someone-else"})
        client = Mock()
        client.containers.list.return_value = [ours, unrelated]
        with patch("docker.from_env", return_value=client):
            self.assertEqual(cleanup_owned("job"), ["owned"])
        ours.remove.assert_called_once_with(force=True)
        unrelated.remove.assert_not_called()
        client.images.prune.assert_not_called()

    def test_wall_timeout_stops_child_and_preserves_unknown(self):
        process = Mock()
        process.wait.side_effect = subprocess.TimeoutExpired("child", 1)
        with tempfile.TemporaryDirectory() as tmp, patch("evaluation_runtime.subprocess.Popen", return_value=process), \
             patch("evaluation_runtime.stop_process") as stop, patch("evaluation_runtime.cleanup_owned", return_value=["ours"]):
            code = supervise(["child"], tmp, seconds=1)
            summary = json.loads((Path(tmp) / "runtime-summary.json").read_text())
            self.assertEqual(code, 124)
            self.assertEqual(summary["status"], "wall_time_exceeded")
            self.assertFalse(summary["evaluation_valid"])
            self.assertFalse((Path(tmp) / "results.json").exists())
            stop.assert_called_once_with(process)


if __name__ == "__main__":
    unittest.main()
