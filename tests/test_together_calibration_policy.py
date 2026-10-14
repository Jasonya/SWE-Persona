"""Bounded calibration must never hide errors or produce formal scores."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import asyncio
import contextlib
import io
import unittest
from unittest.mock import patch

from swe_persona.cli import gate_allows_execution, normalize_result
from swe_persona.vendor.swe_together.swe_together_pilot.persona_runtime import calibration_policy as policy
from swe_persona.vendor.swe_together.swe_together_pilot.persona_runtime.calibration_fixtures import load_cases

ROOT=Path(__file__).parents[1]
spec=importlib.util.spec_from_file_location('score_calibration',ROOT/'scripts/score.py')
score=importlib.util.module_from_spec(spec);spec.loader.exec_module(score)


class TogetherCalibrationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'SWE_PERSONA_TOGETHER_CALIBRATION_MODE':'bounded'})
        self.env.start();self.addCleanup(self.env.stop)
        cases,validation=load_cases();suite,_=policy.suite()
        self.controls=suite['cases']
        self.report={'profile':'swe-together','controls':37,'passed_controls':37,
            'fixture_validation':validation,
            'binding_sha256s':sorted({c['binding_sha256'] for c in self.controls}),
            'results':[{'id':c['id'],'expected':c['expected'],'actual':c['expected'],'passed':True,
                'judgment':{'status':c['expected'],'judge_error':None}} for c in cases],
            'simulator_transport_controls':[{'binding_file':b,'passed':True}
                for b in sorted({c['binding_file'] for c in cases})]}

    def finish(self,report=None,mode='bounded'):
        report=deepcopy(report or self.report)
        report['passed_controls']=sum(r['passed'] is True for r in report['results'])
        report.update(policy.decision(report['results'],report['simulator_transport_controls'],report['fixture_validation'],mode))
        return report

    def mismatch(self,report,control,actual):
        row=next(r for r in report['results'] if r['id']==control['id'])
        row.update(actual=actual,passed=False,judgment={'status':actual,'judge_error':None})

    def gate(self,report):
        return {'profile':'swe-together','worker_exit_code':0,'passed':report['passed'],
                'execution_allowed':report['execution_allowed'],'calibration':report}

    def test_complete_pass_and_two_reviewed_disagreements(self):
        report=self.finish();self.assertTrue(policy.calibration_allowed(report))
        self.assertTrue(gate_allows_execution(self.gate(report)))
        soft=[c for c in self.controls if c.get('degradation_allowed')]
        self.mismatch(report,soft[0],'not_met');self.mismatch(report,soft[1],'unknown')
        report=self.finish(report)
        self.assertEqual(report['status'],'degraded');self.assertFalse(report['passed'])
        self.assertTrue(report['score_provisional'])
        self.assertTrue(gate_allows_execution(self.gate(report)))

    def test_three_disagreements_or_strict_mode_block(self):
        soft=[c for c in self.controls if c.get('degradation_allowed')]
        report=deepcopy(self.report);self.mismatch(report,soft[0],'not_met')
        self.assertFalse(policy.calibration_allowed(self.finish(report,'strict')))
        for c in soft[:3]:self.mismatch(report,c,'unknown')
        self.assertFalse(policy.calibration_allowed(self.finish(report)))

    def test_unreviewed_positive_disagreement_blocks(self):
        c=next(c for c in self.controls if c['expected']=='met' and not c.get('degradation_allowed'))
        self.mismatch(self.report,c,'not_met')
        self.assertFalse(policy.calibration_allowed(self.finish()))

    def test_every_negative_and_missing_evidence_control_remains_required(self):
        for c in self.controls:
            if c['expected']=='met':continue
            for actual in {'met','not_met','unknown'}-{c['expected']}:
                with self.subTest(case=c['id'],actual=actual):
                    report=deepcopy(self.report);self.mismatch(report,c,actual)
                    self.assertFalse(policy.calibration_allowed(self.finish(report)))

    def test_unknown_from_judge_error_never_counts_as_pass(self):
        row=next(r for r in self.report['results'] if r['expected']=='unknown')
        row['judgment']['judge_error']={'error_type':'ValueError'}
        # Even a marker that incorrectly claims it passed must be rejected.
        self.assertFalse(policy.calibration_allowed(self.finish()))
        row['passed']=False
        self.assertFalse(policy.calibration_allowed(self.finish()))

    def test_reviewed_positive_api_or_protocol_error_still_blocks(self):
        c=next(c for c in self.controls if c.get('degradation_allowed'))
        for field,value in [('judge_error',{'error_type':'InvalidJudgeResponse'}),('api_interrupted',True)]:
            report=deepcopy(self.report);self.mismatch(report,c,'unknown')
            next(r for r in report['results'] if r['id']==c['id'])['judgment'][field]=value
            self.assertFalse(policy.calibration_allowed(self.finish(report)))

    def test_incomplete_reports_duplicate_controls_and_wire_failures_block(self):
        for change in [lambda r:r['results'].pop(),
                lambda r:r['results'].__setitem__(0,r['results'][1]),
                lambda r:r['simulator_transport_controls'].pop(),
                lambda r:r['simulator_transport_controls'][0].update(passed=False),
                lambda r:r.update(binding_sha256s=[]),
                lambda r:r['fixture_validation'].update(suite_sha256='wrong')]:
            report=deepcopy(self.report);change(report)
            self.assertFalse(policy.calibration_allowed(self.finish(report)))

    def test_legacy_or_forged_gate_cannot_bypass_validation(self):
        self.assertFalse(gate_allows_execution({'profile':'swe-together','passed':True}))
        report=self.finish();report['results'].pop()
        self.assertFalse(gate_allows_execution(self.gate(report)))
        report=self.finish();gate=self.gate(report);gate['worker_exit_code']=1
        self.assertFalse(gate_allows_execution(gate))

    def test_strict_mode_cannot_reuse_bounded_gate(self):
        report=self.finish()
        with patch.dict(os.environ,{'SWE_PERSONA_TOGETHER_CALIBRATION_MODE':'strict'}):
            self.assertFalse(gate_allows_execution(self.gate(report)))

    def test_mode_changes_cache_identity_and_success_is_reused(self):
        from swe_persona import cli
        def worker(root,mode,out,config):
            report=self.finish(mode=policy.mode())
            (out/'calibration.json').write_text(json.dumps(report))
            return 0
        row={'interaction':{'profile_id':'swe-together'}}
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(cli,'load_config',return_value={}), \
                patch.object(cli,'rows',return_value=[row]), \
                patch.object(cli,'materialize',return_value=Path(temp)), \
                patch.object(cli,'worker',side_effect=worker) as calls:
            cache=Path(temp)/'cache';cache.mkdir()
            bounded=cli.calibrate('dataset','swe-together','config',cache)
            self.assertEqual(bounded,cli.calibrate('dataset','swe-together','config',cache))
            self.assertEqual(calls.call_count,1)
            with patch.dict(os.environ,{'SWE_PERSONA_TOGETHER_CALIBRATION_MODE':'strict'}):
                strict=cli.calibrate('dataset','swe-together','config',cache)
            self.assertNotEqual(bounded,strict);self.assertEqual(calls.call_count,2)

    def test_calibrator_error_unknown_is_not_a_successful_control(self):
        from swe_persona.vendor.swe_together.swe_together_pilot.persona_runtime import calibrate
        cases,_=load_cases();expected={c['evidence']['revision']:c['expected'] for c in cases}
        class Simulator:
            def __init__(self,binding,calls):
                self.binding=binding;self.simulator_errors=[];self.invalid=False
            async def step(self,payload):
                req=self.binding['persona']['bound_evolved_requirements'][0]
                return {'requirement_id':req['id'],'content':req['statement']}
        async def judge(call,req,evidence,*args):
            status=expected[evidence['revision']]
            return {'status':status,'judge_error':{'error_type':'ValueError'} if status=='unknown' else None}
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(calibrate,'Calls',return_value=None), \
                patch.object(calibrate,'UserSimulator',Simulator), \
                patch.object(calibrate,'judge_requirement',side_effect=judge), \
                contextlib.redirect_stdout(io.StringIO()):
            report=asyncio.run(calibrate.calibrate({},Path(temp)))
        row=next(r for r in report['results'] if r['expected']=='unknown')
        self.assertFalse(row['passed'])
        self.assertEqual(report['passed_controls'],36)
        self.assertEqual(report['status'],'blocked')
        self.assertFalse(policy.calibration_allowed(report))

    def test_normalization_preserves_provisional_flag_without_episode(self):
        row={'task_id':'fixture','source_benchmark':'SWE-Together','source_subset':'test',
             'content_sha256':'fixture','interaction':{'profile_id':'swe-together'}}
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)
            (out/'run-config.json').write_text(json.dumps({'calibration':{'passed':False,
                'status':'degraded','execution_allowed':True,'score_provisional':True}}))
            result=normalize_result(row,out,124)
            self.assertTrue(result['score_provisional'])
            self.assertFalse(result['calibration']['passed'])

    def test_degraded_attempts_cannot_publish_formal_joint_rates(self):
        results=[{'task_id':'a','status':'pass','native_pass':True,'persona_pass':True,
                  'score_provisional':True},
                 {'task_id':'b','status':'fail','native_pass':True,'persona_pass':False}]
        metrics=score.metrics(results,2)
        self.assertIsNone(metrics['joint_success_rate'])
        self.assertIsNone(metrics['joint_success_rate_valid_only'])
        self.assertEqual(metrics['provisional_joint_success_rate'],.5)
        self.assertEqual(metrics['native_pass_rate_observed'],1.)
        self.assertEqual(metrics['persona_pass_rate_observed'],.5)
        results[0]['score_provisional']=False
        self.assertEqual(score.metrics(results,2)['joint_success_rate'],.5)


if __name__=='__main__':unittest.main()
