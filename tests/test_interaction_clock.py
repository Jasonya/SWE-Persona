"""Regression tests for setup stealing a task's agent/user interaction budget."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from swe_persona.time_limit import (
    PHASE_ENV, POLICY, begin_interaction, begin_verification, resolve_limit, run_bounded, apply_timeout_result,
)

ROOT=Path(__file__).parents[1]


class InteractionClockTests(unittest.TestCase):
    def run_child(self, source, *, seconds=.2, preparation=2, verification=2, grace=.2):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            env=dict(os.environ, PYTHONPATH=str(ROOT/'src'))
            with (folder/'worker.log').open('w') as log:
                receipt=run_bounded([sys.executable,'-c',source],env=env,log=log,
                    seconds=seconds, phase_file=folder/'clock.json',
                    preparation_sec=preparation, verification_sec=verification,
                    cleanup_grace_sec=grace)
            self.last_log=(folder/'worker.log').read_text()
            return receipt

    def test_preparation_and_grading_longer_than_cap_do_not_steal_interaction(self):
        r=self.run_child('''import time
from swe_persona.time_limit import begin_interaction,begin_verification
time.sleep(.25)
begin_interaction(.2)
time.sleep(.025)
begin_verification()
time.sleep(.25)
''')
        self.assertEqual(r['process_exit_code'],0,self.last_log)
        self.assertFalse(r['timed_out'])
        self.assertGreater(r['phase_elapsed_sec']['preparation'],.2)
        self.assertGreater(r['phase_elapsed_sec']['verification'],.2)
        self.assertLess(r['phase_elapsed_sec']['interaction'],.2)
        self.assertEqual([e['phase'] for e in r['phase_events']],['interaction','verification'])
        self.assertEqual(r['cleanup_elapsed_sec'],0)

    def test_preparation_timeout_has_no_interaction(self):
        r=self.run_child('import time; time.sleep(10)',preparation=.15)
        self.assertTrue(r['timed_out'])
        self.assertEqual(r['timeout_phase'],'preparation')
        self.assertFalse(r['interaction_started'])
        self.assertIn('SIGINT',r['signals'])

    def test_grading_timeout_is_separate_from_interaction(self):
        r=self.run_child('''import time
from swe_persona.time_limit import begin_interaction,begin_verification
begin_interaction(.2)
begin_verification()
time.sleep(10)
''',verification=.15)
        self.assertTrue(r['timed_out'])
        self.assertEqual(r['timeout_phase'],'verification')
        self.assertLess(r['phase_elapsed_sec']['interaction'],.2)

    def test_expired_interaction_can_finish_grading_but_cannot_earn_credit(self):
        r=self.run_child('''import time
from swe_persona.time_limit import begin_interaction,begin_verification
begin_interaction(.05)
time.sleep(.12)
begin_verification()
time.sleep(.02)
''',seconds=.05,grace=.5)
        self.assertEqual(r['process_exit_code'],0,self.last_log)
        self.assertTrue(r['timed_out'])
        self.assertEqual(r['timeout_phase'],'interaction')
        self.assertEqual(r['signals'],[])
        self.assertGreater(r['phase_elapsed_sec']['verification'],0)

    def test_stuck_interaction_is_killed_without_touching_sibling(self):
        sibling=subprocess.Popen([sys.executable,'-c','import time; time.sleep(10)'])
        try:
            r=self.run_child('''import time
from swe_persona.time_limit import begin_interaction
begin_interaction(.05)
time.sleep(10)
''',seconds=.05,grace=.1)
            self.assertEqual(r['timeout_phase'],'interaction')
            self.assertIsNone(sibling.poll())
        finally:
            sibling.terminate();sibling.wait(timeout=5)

    def test_synchronous_cutoff_bypasses_model_retries_and_allows_final_grading(self):
        r=self.run_child('''import time
from swe_persona.time_limit import begin_interaction,arm_interaction,begin_verification,InteractionExpired
arm_interaction(begin_interaction(.05))
try:
 try:time.sleep(10)
 except Exception:raise AssertionError('provider retries swallowed the deadline')
except InteractionExpired:
 begin_verification();time.sleep(.25)
''',seconds=.05,grace=.5)
        self.assertEqual(r['process_exit_code'],0,self.last_log)
        self.assertTrue(r['timed_out'])
        self.assertEqual(r['timeout_phase'],'interaction')
        self.assertEqual(r['signals'],[])

    def test_infrastructure_timeout_is_not_scored_as_model_failure(self):
        for phase in ('preparation','verification'):
            with self.subTest(phase=phase):
                r=apply_timeout_result({'native_pass':None,'persona_pass':None},
                    {'timed_out':True,'timeout_phase':phase})
                self.assertEqual(r['status'],'infrastructure_error')
                self.assertEqual(r['outcome_reason'],phase+'_timeout')
                self.assertIsNone(r['score'])

    def test_interaction_timeout_keeps_final_grades_and_never_creates_missing_grades(self):
        for native,persona in ((True,True),(False,True),(None,None)):
            r=apply_timeout_result({'native_pass':native,'persona_pass':persona},
                {'timed_out':True,'timeout_phase':'interaction'})
            self.assertEqual(r['status'],'fail')
            self.assertEqual(r['score'],0)
            self.assertFalse(r['joint_success'])
            self.assertIs(r['native_pass'],native)
            self.assertIs(r['persona_pass'],persona)
            self.assertEqual(r['verification_complete'],native is not None and persona is not None)

    def test_override_and_native_inner_cap_share_deadline_without_restart(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{PHASE_ENV:str(Path(temp)/'clock.json')}):
            p=Path(os.environ[PHASE_ENV]);p.write_text(json.dumps({'interaction_sec':.5,'verification_sec':2,'events':[]}))
            with patch('swe_persona.time_limit.time.monotonic',return_value=100):
                self.assertEqual(begin_interaction(10),100.5)
            with patch('swe_persona.time_limit.time.monotonic',return_value=100.1):
                self.assertEqual(begin_interaction(10),100.5)
                self.assertEqual(begin_verification(),102.1)
            self.assertEqual(len(json.loads(p.read_text())['events']),2)

    def test_existing_profile_limit_is_not_extended_by_larger_override(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{PHASE_ENV:str(Path(temp)/'clock.json')}):
            p=Path(os.environ[PHASE_ENV]);p.write_text(json.dumps({'interaction_sec':10,'verification_sec':2,'events':[]}))
            with patch('swe_persona.time_limit.time.monotonic',return_value=100):
                self.assertEqual(begin_interaction(.5),100.5)

    def test_configured_infrastructure_budgets_and_interaction_default(self):
        row={'budget':{'episode_timeout_sec':155.5326,'verifier_timeout_sec':155.5326}}
        with patch.dict(os.environ,{'SWE_PERSONA_PREPARATION_TIMEOUT_SEC':'7200','SWE_PERSONA_VERIFICATION_TIMEOUT_SEC':'900'}):
            r=resolve_limit(row)
            self.assertEqual(r['effective_sec'],155.5326)
            self.assertEqual(r['preparation_sec'],7200)
            self.assertEqual(r['verification_sec'],900)
            self.assertEqual(r['policy'],POLICY)
            self.assertEqual(resolve_limit(row,60)['effective_sec'],60)

    def test_invalid_infrastructure_budget_is_rejected(self):
        with patch.dict(os.environ,{'SWE_PERSONA_PREPARATION_TIMEOUT_SEC':'nan'}):
            with self.assertRaises(ValueError):
                resolve_limit({'budget':{'episode_timeout_sec':1,'verifier_timeout_sec':1}})

    def test_swe_live_nested_supervisor_does_not_reapply_preparation_cap(self):
        # Real nested processes, with fake grade files and no API/Docker calls.
        # The old inner process.wait(seconds) killed this sample during startup.
        vendor=ROOT/'src/swe_persona/vendor/swe_live/runtime/project/scripts/swe_live_multilang/reference_v2/whole_sample.py'
        source='''import importlib.util,json,pathlib,sys,tempfile,types
support=types.ModuleType('support')
support.read=lambda p:json.loads(pathlib.Path(p).read_text())
def save(p,d):pathlib.Path(p).write_text(json.dumps(d))
support.save=save;sys.modules['support']=support
runtime=types.ModuleType('evaluation_runtime');runtime.stop_process=lambda p:p.terminate()
sys.modules['evaluation_runtime']=runtime
spec=importlib.util.spec_from_file_location('whole_sample',VENDOR)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
child="""import argparse,json,pathlib,time
from swe_persona.time_limit import begin_interaction,begin_verification
p=argparse.ArgumentParser();p.add_argument('--sample-seconds');p.add_argument('--deadline-monotonic');p.add_argument('--output-dir');a=p.parse_args()
time.sleep(.25);begin_interaction(.2);time.sleep(.02);begin_verification();time.sleep(.25)
r=pathlib.Path(a.output_dir);r.mkdir()
(r/'release_check.json').write_text(json.dumps({'ready_for_review':True}))
(r/'episode.json').write_text(json.dumps({'metrics':{'base_task_success':1,'joint_success':1}}))
"""
with tempfile.TemporaryDirectory() as temp:
 r=m.supervise_sample([sys.executable,'-c',child],pathlib.Path(temp)/'sample',.2,cleanup=lambda owner:[])
 assert r['status']=='completed',r
 assert r['joint_success']==1,r
 assert r['seconds']>.2,r
 assert len(r['phase_events'])==2,r
'''.replace('VENDOR',repr(str(vendor)))
        r=self.run_child(source)
        self.assertEqual(r['process_exit_code'],0,self.last_log)
        self.assertFalse(r['timed_out'])


if __name__=='__main__':unittest.main()
