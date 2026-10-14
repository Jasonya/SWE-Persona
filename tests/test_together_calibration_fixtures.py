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
import ast
import os
import re

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
        self.assertEqual(record['source_files_verified'],26)
        self.assertFalse(record['executions_synthesized'])
        self.assertEqual(record['fixture_execution_records'],6)
        self.assertEqual(sum(c['expected']=='met' for c in cases),18)
        self.assertEqual(sum(c['expected']=='not_met' for c in cases),18)
        self.assertEqual(sum(c['expected']=='unknown' for c in cases),1)

    def test_changed_packaged_source_fails_before_model_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copytree(RUNTIME/'calibration-sources',root/'calibration-sources')
            shutil.copyfile(RUNTIME/'calibration-suite.json',root/'calibration-suite.json')
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
        with self.assertRaisesRegex(ValueError,'changed calibration control suite'):
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

    def test_empty_or_missing_control_is_rejected(self):
        for cases in ([],self.cases[:-1],self.cases[:3]+self.cases[4:]):
            with self.assertRaisesRegex(ValueError,'control suite'):
                fixtures.validate_cases(cases)

    def test_inventory_and_matching_answer_cannot_both_drop_a_command(self):
        def omit(case):
            artifact=next(a for a in case['evidence']['artifacts'] if a['ref']=='host:validation_inventory')
            inventory=json.loads(artifact['text']);item=inventory['items'].pop(0)
            artifact['text']=json.dumps(inventory)
            message=case['evidence']['messages'][-1]
            message['text']='\n'.join(line for line in message['text'].splitlines() if item['command'] not in line)
        with self.assertRaisesRegex(ValueError,'Incomplete declared validation inventory'):
            fixtures.validate_cases(self.modified('pr_handoff',omit))

    def test_package_artifact_must_equal_pinned_source(self):
        def replace(case):
            next(a for a in case['evidence']['artifacts'] if a['ref']=='repository:package.json')['text']='{}'
        with self.assertRaisesRegex(ValueError,'artifact differs from reviewed source'):
            fixtures.validate_cases(self.modified('pr_handoff',replace))

    def test_old_path_cannot_drop_baseline(self):
        def remove(case):case['evidence']['artifacts']=[]
        with self.assertRaisesRegex(ValueError,'Missing or unexpected calibration source artifact'):
            fixtures.validate_cases(self.modified('old_path',remove))

    def test_every_control_has_provenance(self):
        cases=deepcopy(self.cases);cases[14].pop('fixture')
        with self.assertRaisesRegex(ValueError,'Missing calibration fixture provenance'):
            fixtures.validate_cases(cases)

    def test_fixture_metadata_cannot_hide_missing_file_explanations(self):
        cases=deepcopy(self.cases);cases[0]['fixture']['per_file_reasons']=[]
        with self.assertRaisesRegex(ValueError,'Changed calibration fixture provenance'):
            fixtures.validate_cases(cases)

    def test_windows_patch_and_exact_outputs_are_reproducible(self):
        sources=RUNTIME/'calibration-sources'
        before=(sources/'repositories/dataclaw/dataclaw/anonymizer.py').read_bytes()
        case=next(c for c in self.cases if c['fixture']['kind']=='windows_boundary')
        patch=next(a['text'] for a in case['evidence']['artifacts'] if a['ref']=='host:final_diff')
        patch='diff --git '+next(s for s in patch.split('diff --git ')[1:] if s.startswith('a/dataclaw/anonymizer.py '))
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'dataclaw/anonymizer.py';path.parent.mkdir();path.write_bytes(before)
            result=subprocess.run(['git','apply','--whitespace=nowarn','-'],cwd=temp,input=patch,text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(path.read_bytes(),(sources/'derived/dataclaw-windows/anonymizer.py').read_bytes())
            body=path.read_text()
        node=next(n for n in ast.parse(body).body if isinstance(n,ast.FunctionDef) and n.name=='anonymize_path')
        namespace={'os':os,'re':re}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'<fixture>','exec'),namespace)
        fn=namespace['anonymize_path']
        self.assertEqual(fn(r'C:\Users\alice\Documents\myproject\file.py','alice','user_HASH',r'C:\Users\alice'),r'user_HASH/myproject\file.py')
        self.assertEqual(fn(r'C:\Users\alice\work\file.py','alice','user_HASH',r'C:\Users\alice'),r'user_HASH/work\file.py')
        self.assertEqual(fn('/Users/alice/Documents/myproject/file.py','alice','user_HASH','/Users/alice'),'myproject/file.py')

    def test_incorrect_positive_boundary_answer_is_rejected(self):
        def replace(case):
            case['evidence']['messages'][-1]['text']=case['evidence']['messages'][-1]['text'].replace('user_HASH/myproject\\file.py','myproject/file.py')
        with self.assertRaisesRegex(ValueError,'actual boundary outputs'):
            fixtures.validate_cases(self.modified('windows_boundary',replace))

    def test_static_fixture_checks_replay_against_exact_patch(self):
        sources=RUNTIME/'calibration-sources'
        case=next(c for c in self.cases if c['fixture']['kind']=='static_learning')
        artifacts={a['ref']:a['text'] for a in case['evidence']['artifacts']}
        with tempfile.TemporaryDirectory() as temp:
            workspace=Path(temp)/'repo';workspace.mkdir();logs=Path(temp)/'logs';logs.mkdir()
            paths=['Dockerfile.worker','docker-entrypoint.sh','new-ui/src/api/hooks/use-agents.ts']
            for name in paths:
                target=workspace/name;target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(sources/'repositories/agent-swarm'/name,target)
            subprocess.run(['git','init','-q',str(workspace)],check=True,capture_output=True)
            subprocess.run(['git','add','.'],cwd=workspace,check=True,capture_output=True)
            result=subprocess.run(['git','apply','--whitespace=nowarn','-'],cwd=workspace,input=artifacts['host:final_diff'],text=True,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            for name in paths:self.assertEqual((workspace/name).read_text(),artifacts[name])
            (workspace/'bounded_acceptance_checks.py').write_text(artifacts['host:bounded_acceptance_checker'])
            for command in (['bash','-n','docker-entrypoint.sh'],['git','diff','--check'],
                            [sys.executable,'bounded_acceptance_checks.py',str(workspace),str(logs)]):
                result=subprocess.run(command,cwd=workspace,text=True,capture_output=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__ == '__main__':
    unittest.main()
