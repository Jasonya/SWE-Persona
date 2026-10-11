import bootstrap
import json
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from run_fullset import online_stage, atomic_save, report
from support import file_hash, read, source_hashes


class OnlineRunnerTests(unittest.TestCase):
    def test_no_reference_prepass_matched_budgets_and_resume(self):
        with TemporaryDirectory() as tmp:
            study = Path(tmp)
            personas = ['manager_no_code', 'first_time_pr_contributor']
            tasks, pairs = [], []
            for i in range(1, 3):
                task = {'key': f'{i:04d}-js', 'instance_id': f'owner__repo-{i}', 'path': f'tasks/{i}.jsonl'}
                task_file = study / task['path']
                task_file.parent.mkdir(parents=True, exist_ok=True)
                task_file.write_text(json.dumps({'instance_id': task['instance_id'], 'docker_image': 'image',
                    'base_commit': 'abc', 'problem_statement': 'Repair public link.'}) + '\n', encoding='utf-8')
                task['sha256'] = file_hash(study / task['path'])
                tasks.append(task)
                for persona in personas:
                    opening = f'openings/{i}-{persona}.json'
                    atomic_save(study / opening, {'task_id': task['instance_id'], 'persona': persona,
                                                 'vague_request': 'Please fix the link.'})
                    pairs.append({'task_key': task['key'], 'instance_id': task['instance_id'], 'persona': persona,
                                  'status': 'applicable', 'opening_path': opening,
                                  'opening_sha256': file_hash(study / opening)})
            manifest = {'config': {'model': 'test', 'order_seed': 'stable', 'initial_threshold_seconds': 900,
                         'online_max_seconds': 3600, 'update_every': 2, 'gold_safety_seconds': 900, 'timing_mode': 'online'},
                        'tasks': tasks, 'personas': personas, 'sources': source_hashes()}
            atomic_save(study / 'manifest.json', manifest)
            atomic_save(study / 'admission/frozen.json', {})
            for persona in personas:
                atomic_save(study / 'qualification' / persona / 'qualification.json', {})
            calls = []
            def run(command, directory, cap):
                result = {'status': 'completed', 'seconds': 100, 'cap_seconds': cap,
                          'joint_success': 0, 'base_task_success': 0}
                atomic_save(directory / 'result.json', result)
                calls.append((directory, cap))
                return result
            with ExitStack() as stack:
                stack.enter_context(patch('run_fullset.readiness', return_value={'final_queue_ready': True}))
                stack.enter_context(patch('run_fullset.resolved_inventory', return_value=pairs))
                stack.enter_context(patch('run_fullset.get_image', return_value=(SimpleNamespace(id='sha256:image'), False)))
                stack.enter_context(patch('run_fullset.release_image'))
                stack.enter_context(patch('run_fullset.ensure_gold', return_value=3))
                stack.enter_context(patch('run_reference.gold_proof'))
                stack.enter_context(patch('run_qualification.ensure_gate'))
                stack.enter_context(patch('run_fullset.supervise_sample', side_effect=run))
                prepass = stack.enter_context(patch('run_fullset.reference_stage', side_effect=AssertionError('No prepass allowed')))
                online_stage(study, manifest, object(), False)
                self.assertEqual(len(calls), 12)
                self.assertFalse(prepass.called)
                self.assertEqual([cap for _, cap in calls], [900] * 6 + [675] * 6)
                self.assertFalse((study / 'reference').exists())
                summary = report(study, manifest)
                self.assertFalse(summary['exact_fastest_twenty_percent_guaranteed'])
                self.assertEqual(summary['by_arm']['persona']['joint_passes'], 0)
                online_stage(study, manifest, object(), False)
                self.assertEqual(len(calls), 12, 'Resume must retain valid failures rather than buying retries')
                self.assertEqual(read(study / 'online-threshold.json')['next_cap_seconds'], 506.25)

    def test_unqualified_ten_persona_scope_blocks_before_paid_work(self):
        with patch('run_fullset.readiness', return_value={'final_queue_ready': False,
                  'unresolved_pairs': 10770, 'validated_personas': ['manager_no_code']}), \
             patch('run_fullset.get_image', side_effect=AssertionError('No Docker work allowed')):
            with self.assertRaisesRegex(RuntimeError, 'No partial fallback'):
                online_stage(Path('unused'), {}, object(), False)


if __name__ == '__main__':
    unittest.main()
