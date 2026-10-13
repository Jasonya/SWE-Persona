"""Calibration controls must have complete, reproducible evidence."""
from copy import deepcopy
import importlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import ModuleType
import unittest

ROOT = Path(__file__).parents[1]
RUNTIME = ROOT/'src/swe_persona/vendor/swe_together/swe_together_pilot/persona_runtime'
package = ModuleType('fixture_together_calibration')
package.__path__ = [str(RUNTIME)]
sys.modules[package.__name__] = package
fixtures = importlib.import_module(package.__name__+'.calibration_fixtures')
seal = importlib.import_module(package.__name__+'.evaluation').seal_evidence


class TogetherCalibrationFixtureTests(unittest.TestCase):
    def setUp(self):
        self.cases = json.loads((RUNTIME/'calibration-cases.json').read_text())

    def modified(self,kind,change):
        cases = deepcopy(self.cases)
        case = next(c for c in cases if c.get('fixture',{}).get('kind')==kind and c['expected']=='met')
        change(case)
        ev = case['evidence']
        case['evidence'] = seal(ev['artifacts'],ev['messages'],ev.get('execution_history',ev['executions']))
        return cases

    def test_packaged_sources_and_control_labels(self):
        cases,record = fixtures.load_cases()
        self.assertEqual(record['source_files_verified'],16)
        self.assertFalse(record['executions_synthesized'])
        self.assertEqual(sum(c['expected']=='met' for c in cases),18)
        self.assertEqual(sum(c['expected']=='not_met' for c in cases),18)
        self.assertEqual(sum(c['expected']=='unknown' for c in cases),1)

    def test_changed_packaged_source_fails_before_model_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copytree(RUNTIME/'calibration-sources',root/'calibration-sources')
            target = root/'calibration-sources/repositories/agent-swarm/package.json'
            target.write_text('{}')
            with self.assertRaisesRegex(ValueError,'checksum mismatch'):
                fixtures.validate_cases(self.cases,root)

    def test_positive_handoff_cannot_omit_command_inventory(self):
        def omit(case):
            artifact = next(a for a in case['evidence']['artifacts'] if a['ref']=='host:validation_inventory')
            command = json.loads(artifact['text'])['items'][0]['command']
            case['evidence']['messages'][-1]['text'] = case['evidence']['messages'][-1]['text'].replace(command,'omitted')
        with self.assertRaisesRegex(ValueError,'omits a declared validation item'):
            fixtures.validate_cases(self.modified('pr_handoff',omit))

    def test_positive_handoff_cannot_restore_generic_placeholder(self):
        def weaken(case):
            case['evidence']['messages'][-1]['text'] += '\nimplements or documents the task behavior'
        with self.assertRaisesRegex(ValueError,'Generic placeholder'):
            fixtures.validate_cases(self.modified('pr_handoff',weaken))

    def test_review_artifact_must_match_the_pinned_branch(self):
        def replace(case):
            next(a for a in case['evidence']['artifacts'] if a['ref']=='host:final_diff')['text']='unrelated patch'
        with self.assertRaisesRegex(ValueError,'diff differs from pinned branch sources'):
            fixtures.validate_cases(self.modified('branch_review',replace))

    def test_missing_diff_cannot_be_relabelled_as_positive(self):
        cases = deepcopy(self.cases)
        next(c for c in cases if c['expected']=='unknown')['expected']='met'
        with self.assertRaisesRegex(ValueError,'must remain unknown'):
            fixtures.validate_cases(cases)

    def test_missing_evidence_control_cannot_supply_a_sufficient_source(self):
        cases = deepcopy(self.cases)
        case = next(c for c in cases if c['expected']=='unknown')
        case['evidence']['artifacts'].append({'ref':'dataclaw/anonymizer.py','text':'return text.replace(username, username_hash)'})
        ev = case['evidence']
        case['evidence'] = seal(ev['artifacts'],ev['messages'],[])
        with self.assertRaisesRegex(ValueError,'omit the corresponding source'):
            fixtures.validate_cases(cases)

    def test_reconstructed_diff_applies_to_exact_base_bytes(self):
        sources = RUNTIME/'calibration-sources'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(['git','init','-q',str(root)],check=True,capture_output=True)
            pairs = [('dataclaw/anonymizer.py','windows_anonymizer.py'),
                     ('tests/test_anonymizer.py','windows_test_anonymizer.py')]
            for path,_ in pairs:
                target = root/path
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes((sources/'repositories/dataclaw'/path).read_bytes())
            applied = subprocess.run(['git','apply','--whitespace=nowarn','-'],
                                     cwd=root,input=fixtures.review_diff(sources),text=True,capture_output=True)
            self.assertEqual(applied.returncode,0,applied.stderr)
            for path,filename in pairs:
                self.assertEqual((root/path).read_bytes(),
                    (sources/'tasks/dataclaw-anonymizer-tests/environment'/filename).read_bytes())


if __name__ == '__main__':
    unittest.main()
