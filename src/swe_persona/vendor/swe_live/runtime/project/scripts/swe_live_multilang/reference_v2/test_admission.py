import bootstrap
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from collection.admission import validate_pairs, freeze_admission, resolved_inventory
from support import REVISION, save


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.study, self.source = self.root / 'study', self.root / 'input'
        self.study.mkdir()
        self.source.mkdir()
        task = {'instance_id': 'example__repo-1', 'problem_statement': 'Repair the public title link.',
                'docker_image': 'example-image', 'base_commit': 'abcdef'}
        (self.study / 'task.jsonl').write_text(json.dumps(task) + '\n', encoding='utf-8')
        self.manifest = {'dataset_revision': REVISION, 'personas': ['manager_no_code'],
                         'tasks': [{'key': '0001-js', 'instance_id': task['instance_id'], 'path': 'task.jsonl'}]}
        save(self.study / 'manifest.json', self.manifest)
        self.row = {'task_key': '0001-js', 'instance_id': task['instance_id'], 'persona': 'manager_no_code',
                    'status': 'applicable', 'opening_path': 'openings/manager.json',
                    'public_fit': {'outcome_blind': True, 'decision': 'compatible',
                                   'reason': 'The title-link behavior admits a conclusion-only handoff.',
                                   'public_issue_quotes': ['Repair the public title link.']}}
        (self.source / 'openings').mkdir()
        self.opening = self.source / self.row['opening_path']
        save(self.opening, {'task_id': task['instance_id'], 'persona': 'manager_no_code',
                            'vague_request': 'Please fix the title link.'})

    def test_duplicate_or_missing_pairs_cannot_reduce_the_inventory(self):
        for rows in ([], [self.row, self.row]):
            with self.assertRaisesRegex(ValueError, 'complete task-persona matrix'):
                validate_pairs(self.source, self.manifest, rows, self.study)

    def test_missing_implementation_cannot_be_relabelled_as_incompatible(self):
        row = dict(self.row, status='not_applicable', public_fit=dict(self.row['public_fit'],
                   decision='incompatible', reason_code='fixture_not_implemented'))
        with self.assertRaisesRegex(ValueError, 'Missing implementations'):
            validate_pairs(self.source, self.manifest, [row], self.study)

    def test_fit_requires_exact_public_evidence_and_no_outcome_selection(self):
        for change in ({'outcome_blind': False}, {'public_issue_quotes': ['Invented task fact']}):
            row = dict(self.row, public_fit=dict(self.row['public_fit'], **change))
            with self.assertRaisesRegex(ValueError, 'outcome-blind'):
                validate_pairs(self.source, self.manifest, [row], self.study)

    def test_wrong_opening_identity_or_escaping_path_is_rejected(self):
        save(self.opening, {'task_id': 'other-task', 'persona': 'manager_no_code', 'vague_request': 'Please fix it.'})
        with self.assertRaisesRegex(ValueError, 'opening identity'):
            validate_pairs(self.source, self.manifest, [self.row], self.study)
        with self.assertRaisesRegex(ValueError, 'relative artifact path'):
            validate_pairs(self.source, self.manifest, [dict(self.row, opening_path='../outside.json')], self.study)

    def test_freeze_preserves_shared_opening_and_detects_changes(self):
        save(self.source / 'admission-input.json', {'dataset_revision': REVISION, 'pairs': [self.row]})
        rows = freeze_admission(self.study, self.manifest, self.source)
        self.assertEqual(rows[0]['opening_path'], 'admission/openings/manager.json')
        self.assertEqual(rows, resolved_inventory(self.study, self.manifest))
        with self.assertRaisesRegex(ValueError, 'already exists'):
            freeze_admission(self.study, self.manifest, self.source)
        copied = self.study / rows[0]['opening_path']
        changed = json.loads(copied.read_text())
        changed['vague_request'] = 'A different opening'
        save(copied, changed)
        with self.assertRaisesRegex(ValueError, 'evidence changed'):
            resolved_inventory(self.study, self.manifest)

    def test_fit_decision_change_is_detected_after_admission(self):
        save(self.source / 'admission-input.json', {'dataset_revision': REVISION, 'pairs': [self.row]})
        freeze_admission(self.study, self.manifest, self.source)
        path = self.study / 'admission' / 'frozen.json'
        modified = json.loads(path.read_text())
        modified['pairs'][0]['public_fit']['reason'] = 'A changed eligibility rationale'
        save(path, modified)
        with self.assertRaisesRegex(ValueError, 'decisions changed'):
            resolved_inventory(self.study, self.manifest)


if __name__ == '__main__':
    unittest.main()
