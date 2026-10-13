"""Require loaded, disclosed and independently graded shell persona obligations."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from swe_persona.api_recovery import assess_interrupted
from swe_persona.cli import normalize_result

ROOT=Path(__file__).parents[1]
RUNTIME=ROOT/'src/swe_persona/vendor/swe_pro/shell/portable/runtime-v3'

def module(name, **fields):
    result=types.ModuleType(name);result.__dict__.update(fields);return result

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    value=importlib.util.module_from_spec(spec);sys.modules[name]=value
    spec.loader.exec_module(value);return value


class ShellRequirementTests(unittest.TestCase):
    def row(self):
        return {'task_id':'fixture','source_benchmark':'SWE-bench Pro','source_subset':'swe-bench-pro-shell',
            'content_sha256':'fixture','interaction':{'profile_id':'swe-bench-pro-shell'},
            'requirements':[{'requirement_id':rid,'required':True} for rid in ('R1','R2')]}

    def normalized(self,episode):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp);(out/'trial').mkdir()
            (out/'trial/episode.json').write_text(json.dumps(episode))
            return normalize_result(self.row(),out,0)

    def episode(self,checks):
        return {'episode_valid':True,'base_pass':1,'persona_pass':1,'joint_success':1,
                'rubric_config':[{'requirement_id':rid} for rid in ('R1','R2')],
                'outcome':{'base_task':{'status':'pass'},'evolved_requirements':checks}}

    def test_old_empty_persona_episode_cannot_count_as_joint_pass(self):
        old=self.episode([]);old['rubric_config']=[];old['persona_pass']=0
        result=self.normalized(old)
        self.assertEqual(result['status'],'invalid')
        self.assertIs(result['native_pass'],True)
        self.assertIsNone(result['persona_pass'])
        self.assertFalse(result['joint_success'])
        self.assertEqual(result['persona_requirement_coverage']['missing_loaded_ids'],['R1','R2'])

    def test_final_grading_must_cover_every_required_requirement(self):
        result=self.normalized(self.episode([{'requirement':'R1@1','status':'met'}]))
        self.assertEqual(result['status'],'invalid')
        self.assertIsNone(result['persona_pass'])
        self.assertEqual(result['persona_requirement_coverage']['missing_check_ids'],['R2'])

    def test_optional_requirements_require_checks_once_disclosed(self):
        row=self.row()
        for req in row['requirements']:req['required']=False
        episode=self.episode([{'requirement':'R1@1','status':'met'}])
        episode['evolved_requirement_disclosure']={'disclosed_ids':['R1@1','R2@1']}
        from swe_persona.cli import shell_requirement_coverage
        coverage=shell_requirement_coverage(row,episode)
        self.assertEqual(coverage['missing_check_ids'],['R2'])
        self.assertIsNone(coverage['persona_pass'])
        episode['evolved_requirement_disclosure']['disclosed_ids']=['R1@1']
        coverage=shell_requirement_coverage(row,episode)
        self.assertEqual(coverage['missing_check_ids'],[])
        self.assertIs(coverage['persona_pass'],True)

    def test_complete_grades_control_joint_credit_and_unknown_stays_unknown(self):
        for state,status,persona in (('met','pass',True),('not_met','fail',False),('unknown','invalid',None)):
            with self.subTest(state=state):
                episode=self.episode([{'requirement':'R1@1','status':'met'},{'requirement':'R2@2','status':state}])
                result=self.normalized(episode)
                self.assertEqual(result['status'],status)
                self.assertIs(result['persona_pass'],persona)
                self.assertEqual(result['joint_success'],status=='pass')

    def test_interrupted_grading_preserves_unknown_and_never_uses_approval_to_pass(self):
        episode=self.episode([{'requirement':'R1@1','status':'met'}])
        episode['api_recovery_assessment']=assess_interrupted(True,True,artifacts_available=True,progress=True)
        result=self.normalized(episode)
        self.assertEqual(result['status'],'fail')
        self.assertIsNone(result['persona_pass'])
        self.assertFalse(result['verification_complete'])
        self.assertFalse(result['joint_success'])

    def base_verifier(self):
        stubs={'checkers':module('checkers',CheckerRegistry=object,CheckResult=object),
               'ledger':module('ledger',Ledger=object)}
        with patch.dict(sys.modules,stubs):return load('shell_verifier_fixture',RUNTIME/'tb2_persona_pilot/verifier.py')

    def metrics(self,arm,checked,selected=('R1','R2')):
        controller=types.SimpleNamespace(selected=[types.SimpleNamespace(requirement_id=rid,schedule={'required':True})
            for rid in selected],invalid_reasons=[],infra_failures=[],not_applicable=[],disclosure_turns={},
            revisions_done={},events=[],end_state='completed_pending_final',missed_deadlines=lambda _:[])
        evolved=[{'requirement':rid+'@1','status':'met','introduced_turn':2,'claimed_by_agent':True,
                  'reason_code':'verified'} for rid in checked]
        return self.base_verifier().compute_metrics(arm,{'base_task':{'status':'pass'},'evolved_requirements':evolved},
            None,controller,[],None,6,4)

    def test_progressive_empty_or_partial_checks_cannot_pass_but_native_baseline_can(self):
        for checked,selected in (((),()),((),('R1','R2')),(('R1',),('R1','R2'))):
            with self.subTest(checked=checked,selected=selected):
                metrics=self.metrics('progressive_persona',checked,selected)
                self.assertEqual(metrics['joint_success'],0)
                self.assertFalse(metrics['valid_evaluation_coverage']['episode_valid'])
                self.assertIsNone(metrics['evolved_requirement_success']['pass'])
        self.assertEqual(self.metrics('progressive_persona',('R1','R2'))['joint_success'],1)
        self.assertEqual(self.metrics('original',(),())['joint_success'],1)

    def test_registry_fails_on_missing_or_contradictory_applicability(self):
        stubs={'tb2_persona_pilot.registry':module('tb2_persona_pilot.registry',Candidate=object,load_persona=Mock()),
               'task_adapter':module('task_adapter',TaskManifest=object),
               'evidence_policy':module('evidence_policy',corrected_parameters=Mock())}
        with patch.dict(sys.modules,stubs):registry=load('shell_registry_fixture',RUNTIME/'runner/registry.py')
        for applicability in ({},{'verdict':'not_applicable'}):
            document={'requirements':[{'requirement_id':'R1'}],'status':'ready','applicability':applicability}
            with patch.object(registry,'load_binding',return_value=document),self.assertRaisesRegex(ValueError,'applicability'):
                registry.build_candidates(types.SimpleNamespace(),data_version='fixture')
        with patch.object(registry,'load_binding',return_value={'requirements':[],'status':'ready',
                'applicability':{'verdict':'applicable'}}),self.assertRaisesRegex(ValueError,'no requirements'):
            registry.build_candidates(types.SimpleNamespace(),data_version='fixture')

    def test_controller_consumes_shared_gate_and_rejects_wrong_grading_identity(self):
        base=module('tb2_persona_pilot.controller',ROLE_OVERLAYS={},Controller=object,TurnRecord=object,distinctive=Mock())
        identity={'protocol':'fixture','source_sha256':{'verifier':'fixture'}}
        grader=module('rubric_checker',grading_identity=lambda:identity)
        with patch.dict(sys.modules,{'tb2_persona_pilot.controller':base}):
            controller=load('shell_controller_fixture',RUNTIME/'runner/controller.py')
        with tempfile.TemporaryDirectory() as temp:
            gate=Path(temp)
            (gate/'gate.json').write_text(json.dumps({'profile':'swe-bench-pro-shell','passed':True,'worker_exit_code':0}))
            report={'gate_passed':True,'grading_identity':identity}
            path=gate/'rubric_report.fixture.json';path.write_text(json.dumps(report))
            with patch.dict(sys.modules,{'rubric_checker':grader}),patch.dict('os.environ',{'PCB_RUBRIC_GATE_DIR':temp}):
                instance=controller.Controller()
                self.assertEqual(instance._achievability_proof(None)['rubric_judge'],'pass')
                report['grading_identity']={'protocol':'old'};path.write_text(json.dumps(report))
                self.assertNotIn('rubric_judge',instance._achievability_proof(None))
                (gate/'gate.json').write_text(json.dumps({'profile':'swe-bench-pro-shell','passed':False,'worker_exit_code':2}))
                with self.assertRaisesRegex(ValueError,'passed'):instance._achievability_proof(None)


if __name__=='__main__':unittest.main()
