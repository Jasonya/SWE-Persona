"""Offline checks for concurrency, shared calibration, and failure accounting."""
import argparse
from collections import Counter
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('batch_evaluate', Path(__file__).parents[1] / 'scripts/evaluate.py')
batch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(batch)


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'output'
        self.output.mkdir()
        self.args = argparse.Namespace(cache=self.root / 'cache', dataset=self.root / 'dataset.parquet',
                                       config=self.root / 'api.json', output=self.output, workers=4,
                                       dry_run=False, timeout_sec=None)
        self.rows = [{'task_id': f'task-{i}', 'source_benchmark': 'fixture',
                      'interaction': {'profile_id': 'fixture-profile'}} for i in range(9)]

    def test_four_active_tasks_one_calibration_and_stable_result_order(self):
        lock = threading.Lock()
        barrier = threading.Barrier(4)
        calls = Counter()
        active = peak = 0

        def command(argv, log, interrupted):
            nonlocal active, peak
            if 'calibrate' in argv:
                with lock:
                    calls['calibrate'] += 1
                time.sleep(0.03)
                return 0
            task = argv[argv.index('--task') + 1]
            index = int(task.split('-')[1])
            with lock:
                calls[task] += 1
                active += 1
                peak = max(peak, active)
            if index < 4:
                barrier.wait(timeout=5)
            time.sleep(0.005 * (4 - index % 4))
            out = Path(argv[argv.index('--output') + 1])
            out.mkdir()
            (out / 'result.json').write_text(json.dumps({'task_id': task, 'status': 'fail' if index == 3 else 'pass'}))
            with lock:
                active -= 1
            return 0

        with patch.object(batch, 'run_command', side_effect=command):
            rc = batch.evaluate(self.args, self.rows)
        self.assertEqual(rc, 0)
        self.assertEqual(peak, 4)
        self.assertEqual(calls['calibrate'], 1)
        self.assertTrue(all(calls[row['task_id']] == 1 for row in self.rows))
        summary = json.loads((self.output / 'summary.json').read_text())
        self.assertEqual([r['task_id'] for r in summary['results']], [r['task_id'] for r in self.rows])
        self.assertEqual(summary['status_counts'], {'pass': 8, 'fail': 1})
        self.assertEqual(summary['running'], 0)
        self.assertEqual(summary['workers'], 4)

    def test_failed_calibration_blocks_only_its_profile_without_episodes(self):
        calls = []
        def command(argv, *_):
            calls.append(argv)
            time.sleep(0.02)
            return 2
        with patch.object(batch, 'run_command', side_effect=command):
            rc = batch.evaluate(self.args, self.rows)
        summary = json.loads((self.output / 'summary.json').read_text())
        self.assertEqual(rc, 2)
        self.assertEqual(len(calls), 1)
        self.assertIn('calibrate', calls[0])
        self.assertEqual(summary['status_counts'], {'calibration_error': 9})
        self.assertFalse(summary['stopped'])
        self.assertEqual(summary['started'], 9)
        self.assertEqual(summary['blocked_profiles'], ['fixture-profile'])
        self.assertTrue(all(r['episode_started'] is False for r in summary['results']))
        self.assertEqual(summary['selected'], 9)

    def test_other_profiles_continue_after_calibration_failure(self):
        for i, row in enumerate(self.rows):
            row['interaction']['profile_id'] = 'bad' if i % 2 == 0 else 'good'
        calls = Counter()
        lock = threading.Lock()
        def command(argv, *_):
            if 'calibrate' in argv:
                profile=argv[argv.index('--profile')+1]
                with lock:
                    calls[profile]+=1
                return 2 if profile == 'bad' else 0
            task=argv[argv.index('--task')+1]
            self.assertEqual(int(task.split('-')[1]) % 2, 1)
            out=Path(argv[argv.index('--output')+1]);out.mkdir()
            (out/'result.json').write_text(json.dumps({'task_id':task,'status':'pass'}))
            return 0
        with patch.object(batch,'run_command',side_effect=command):
            self.assertEqual(batch.evaluate(self.args,self.rows),2)
        summary=json.loads((self.output/'summary.json').read_text())
        self.assertEqual(calls,{'bad':1,'good':1})
        self.assertEqual(summary['status_counts'],{'calibration_error':5,'pass':4})
        self.assertFalse(summary['stopped'])

    def test_dry_run_skips_calibration_and_accounts_for_errors(self):
        self.args.dry_run = True
        def command(argv, *_):
            self.assertNotIn('calibrate', argv)
            self.assertIn('--dry-run', argv)
            return 1 if argv[argv.index('--task') + 1] == 'task-2' else 0
        with patch.object(batch, 'run_command', side_effect=command):
            rc = batch.evaluate(self.args, self.rows)
        summary = json.loads((self.output / 'summary.json').read_text())
        self.assertEqual(rc, 2)
        self.assertEqual(summary['status_counts'], {'dry_run_pass': 8, 'infrastructure_error': 1})
        self.assertEqual(summary['attempted'], 9)


if __name__ == '__main__':
    unittest.main()
