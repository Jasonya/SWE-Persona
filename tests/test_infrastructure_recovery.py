"""Regression coverage for observed DeepSWE render failures and native preflight."""
from copy import deepcopy
import importlib
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT=Path(__file__).parents[1]
PACKAGE='fixture_deepswe_infrastructure'
package=ModuleType(PACKAGE)
package.__path__=[str(ROOT/'src/swe_persona/vendor/deepswe/project/dspilot')]
sys.modules[PACKAGE]=package
sim=importlib.import_module(PACKAGE+'.simulator_staged')
evidence=importlib.import_module(PACKAGE+'.simulator_evidence')
from swe_persona.native_preflight import native_preflight_report
from swe_persona.cli import normalize_result


class InfrastructureRecoveryTests(unittest.TestCase):
    def test_probes_are_ready_for_disclosure_without_final_acceptance(self):
        controller=SimpleNamespace(disclosed={'explanation'},implementation_authorized=True)
        requirements={'explanation':{'public_probe':'case-v1','checker':{'kind':'grounded_explanation'}},
                      'future':{'public_probe':'secret','checker':{'kind':'grounded_explanation'}}}
        probes={'case-v1':{'id':'case-v1'},'secret':{'id':'secret'}}
        trusted={};bases={};cache={}
        execute=Mock(side_effect=lambda patch,*args:{'patch':patch,'passed':False})
        with tempfile.TemporaryDirectory() as directory:
            snapshot={'patch':'diff-one','patch_sha256':'one'}
            args=(controller,requirements,probes,snapshot,trusted,bases,cache,Path(directory),execute)
            evidence.collect_explanation_evidence(*args)
            self.assertEqual(execute.call_count,2)
            self.assertEqual(trusted['case-v1']['patch'],'diff-one')
            self.assertEqual(set(bases),{'case-v1'})
            evidence.collect_explanation_evidence(*args)
            self.assertEqual(execute.call_count,2)
            snapshot.update(patch='diff-two',patch_sha256='two')
            evidence.collect_explanation_evidence(*args)
            self.assertEqual(execute.call_count,3)
            self.assertEqual(trusted['case-v1']['patch'],'diff-two')
            controller.implementation_authorized=False
            evidence.collect_explanation_evidence(*args)
            self.assertEqual(execute.call_count,3)

    def test_read_only_disclosure_exposes_baseline_without_candidate_measurement(self):
        controller=SimpleNamespace(disclosed={'explanation'},implementation_authorized=False,
            plan={'requirements':{'explanation':{'before_implementation':True}}})
        requirements={'explanation':{'public_probe':'case-v1','checker':{'kind':'grounded_explanation'}}}
        trusted={};bases={};cache={};execute=Mock(return_value={'passed':False})
        with tempfile.TemporaryDirectory() as directory:
            evidence.collect_explanation_evidence(controller,requirements,{'case-v1':{}},
                {'patch':'uncommitted','patch_sha256':'one'},trusted,bases,cache,Path(directory),execute)
            controller.implementation_authorized=True
            evidence.collect_explanation_evidence(controller,requirements,{'case-v1':{}},
                {'patch':'changed','patch_sha256':'two'},trusted,bases,cache,Path(directory),execute)
        self.assertEqual(execute.call_count,1)
        self.assertEqual(execute.call_args.args[0],'')
        self.assertEqual(trusted,{})
        self.assertEqual(cache,{})
        self.assertEqual(set(bases),{'case-v1'})

    def context(self):
        return {'operation':'render','mode':'persona','stage':'S2','requirements':{},
            'checks':{'R1':{'status':'pending'}},'controller_receipt':{'outcome':'advance','accepted':True,'stage':'S1'},
            'required_content':['Public permissions: authorized task edits.','Original requirement with\nexact lines.'],
            'implementation_authorized':True,'remaining_budget':{'user_turns':4,'actions':10}}

    def client(self, outcomes):
        client=Mock();client.create.side_effect=outcomes
        client.text.side_effect=lambda response:json.dumps(response)
        return client

    def draft(self, prose='Please address the disclosed request.'):
        return {'message':prose,'closing':'','stage':'S2','controller_outcome':'advance',
                'acknowledged_checks':{'R1':'pending'},'public_documents':{'block_0':'block_0','block_1':'block_1'}}

    def test_rejected_prose_recovers_without_new_facts_or_agent_turns(self):
        audit=[];ctx=self.context();original=deepcopy(ctx)
        client=self.client([self.draft('Unsupported assertion.'),{'verdict':'reject','issues':['Contradiction.']},
            self.draft('Another unsupported assertion.'),{'verdict':'unknown','issues':[]},self.draft('')])
        message=sim.render(client,ctx,audit)
        self.assertNotIn('unsupported assertion',message)
        self.assertEqual(message.strip(),'\n\n'.join(ctx['required_content']))
        self.assertEqual(ctx,original)
        self.assertEqual(client.create.call_count,5)
        schema=client.create.call_args.kwargs['schema']
        self.assertEqual(schema['properties']['message']['enum'],[''])
        recovery=audit[-1]
        self.assertEqual(recovery['operation'],'message_recovery')
        self.assertEqual(recovery['extra_agent_turns'],0)
        self.assertFalse(recovery['rejected_drafts_delivered'])

    def test_recovery_cannot_change_documents_receipt_or_add_prose(self):
        for change in ('future_reference','wrong_receipt','prose'):
            with self.subTest(change=change):
                recovery=self.draft('')
                if change=='future_reference':recovery['public_documents']['block_0']='block_future'
                elif change=='wrong_receipt':recovery['controller_outcome']='finish'
                else:recovery['closing']='I authorize unrelated edits.'
                client=self.client([self.draft(),{'verdict':'reject','issues':['Contradiction.']},
                    self.draft(),{'verdict':'reject','issues':['Contradiction.']},recovery])
                with self.assertRaises(sim.SimulatorMessageError):sim.render(client,self.context(),[])

    def test_schema_failures_do_not_authorize_document_recovery(self):
        client=self.client([{},{}]);audit=[]
        with self.assertRaises(sim.SimulatorMessageError):sim.render(client,self.context(),audit)
        self.assertEqual(client.create.call_count,2)
        self.assertFalse(any(r['operation']=='message_recovery' for r in audit))

    def test_cancellation_is_not_recovered(self):
        client=Mock();client.create.side_effect=KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):sim.render(client,self.context(),[])

    def native_attempt(self, root, number, success):
        folder=root/f'attempt-{number:03d}';(folder/'evaluation/instance').mkdir(parents=True)
        (folder/'attempt.json').write_text(json.dumps({'exit_code':0}))
        (folder/'evaluation/results.json').write_text(json.dumps(
            {'submitted':1,'success':int(success),'error':0,'incomplete':0}))
        (folder/'evaluation/instance/report.json').write_text(json.dumps(
            {'instance_id':'instance','PASS_TO_PASS':{'failure':[] if success else ['Test_handler_download_logs']}}))

    def test_any_failed_reference_check_blocks_stability_even_after_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);self.native_attempt(root,1,False);self.native_attempt(root,2,True)
            report=native_preflight_report(root)
            self.assertFalse(report['passed'])
            self.assertEqual(len(report['attempts']),2)
            self.assertEqual(report['attempts'][0]['failed_tests']['instance']['PASS_TO_PASS'],['Test_handler_download_logs'])
            self.assertFalse(native_preflight_report(root/'missing')['passed'])

    def test_native_error_has_explicit_normalized_reason_without_scores(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'preflight-error.json').write_text(json.dumps({'error_category':'native_reference_failure',
                'episode_started':False,'error':'Reference failed'}))
            row={'task_id':'live/fixture','source_benchmark':'Live','source_subset':'swe-bench-live',
                 'content_sha256':'fixture','interaction':{'profile_id':'swe-bench-live'}}
            result=normalize_result(row,root,1)
            self.assertEqual(result['status'],'infrastructure_error')
            self.assertEqual(result['error_category'],'native_reference_failure')
            self.assertFalse(result['episode_started'])
            self.assertIsNone(result['native_pass'])
            self.assertIsNone(result['persona_pass'])


if __name__=='__main__':unittest.main()
