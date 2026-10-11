import queue
import re
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from runtime_output import make_output_reader


class Metadata:
    scanned_chars = 0
    pattern = re.compile(r"\n###PS1JSON###\n(.*?)\n###PS1END###", re.S)

    @classmethod
    def matches_ps1_metadata(cls, text):
        cls.scanned_chars += len(text)
        return list(cls.pattern.finditer(text))

    @classmethod
    def from_ps1_match(cls, match):
        return match.group(1)


class RuntimeOutputTests(unittest.TestCase):
    prompt = '\n###PS1JSON###\n{"exit_code": "0"}\n###PS1END###'

    def runtime(self, data, chunk_size):
        q = queue.Queue()
        for start in range(0, len(data), chunk_size):
            q.put(data[start:start+chunk_size])
        def combine(output, matches):
            return output[:matches[0].start()] if matches else output
        return SimpleNamespace(output_queue=q, _combine_outputs_between_matches=combine)

    def test_split_utf8_and_prompt_preserve_full_output(self):
        text = "中文 \x1b[31mred\x1b[0m\r\n"
        reader = make_output_reader(Metadata, re.compile(r"\x1b\[[0-9;]*m"))
        result, metadata = reader(self.runtime((text+self.prompt).encode(), 1), timeout=2)
        self.assertEqual(result, "中文 red\n")
        self.assertEqual(metadata, '{"exit_code": "0"}')

    def test_large_output_scans_once_instead_of_every_chunk(self):
        text = "test passed\n" * 300000
        Metadata.scanned_chars = 0
        reader = make_output_reader(Metadata, re.compile(r"\x1b\[[0-9;]*m"))
        result, metadata = reader(self.runtime((text+self.prompt).encode(), 4096), timeout=10)
        self.assertEqual(result, text)
        self.assertIsNotNone(metadata)
        self.assertLess(Metadata.scanned_chars, 2 * len(text))

    def test_no_prompt_does_not_report_success(self):
        reader = make_output_reader(Metadata, re.compile(r"\x1b\[[0-9;]*m"))
        result, metadata = reader(self.runtime(b"partial", 4096), timeout=0.01)
        self.assertEqual(result, "partial")
        self.assertIsNone(metadata)

    def test_dead_stream_stops_waiting_without_inventing_a_prompt(self):
        reader = make_output_reader(Metadata, re.compile(r"\x1b\[[0-9;]*m"))
        runtime = self.runtime(b"", 4096)
        runtime.output_queue = Mock()
        runtime.output_queue.get.side_effect = queue.Empty()
        runtime.output_thread = Mock()
        runtime.output_thread.is_alive.return_value = False
        result, metadata = reader(runtime, timeout=9000)
        runtime.output_queue.get.assert_called_once()
        self.assertEqual(result, "")
        self.assertIsNone(metadata)
