"""SWE-Live control failures must stay separate from semantic gate failures."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).parents[1]
ADAPTER = ROOT/'src/swe_persona/vendor/swe_live/runtime/project/scripts/swe_live_multilang/tb3_adapter'

def load(name, path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

protocol=load('live_protocol_fixture',ADAPTER/'protocol.py')

def module(name, **fields):
    value=types.ModuleType(name);value.__dict__.update(fields);return value


class LiveRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.persona={'id':'manager_no_code','name':'Manager','attributes':{
            k:'Concise handoff.' for k in ('technical_context','primary_goal','interaction_preferences','constraints')}}
        self.req=protocol.requirement(self.persona)
        self.messages=[{'role':'agent','message_id':'a-2','turn':2,
                        'text':'- Recommendation: Ship.\n- Progress: Verified.\n- Decision: Approve.'}]

    def review(self, ref='a-2', verdict='MET'):
        return {'requirement_id':'R1','verdict':verdict,
                'criteria':[{'criterion_id':'R1.contract','status':verdict,'evidence_refs':[ref],'reason':'Evidence.'}],
                'feedback':'Visible handoff.','reason':'Evidence.'}

    def assess(self, client):
        return protocol.assess_judgment(client,self.req,self.messages,{'eligible_visible_messages':self.messages},
                                       protocol.JUDGE_PROMPT,'fixture',self.root/'assessment.json')

    def test_repair_uses_same_evidence_and_preserves_raw_text(self):
        self.messages[0]['text']='  leading whitespace\n`literal tokens`'
        client=Mock();del client.deadline
        client.json_obj.side_effect=[self.review('invented'),self.review()]
        self.assertEqual(self.assess(client)['verdict'],'MET')
        self.assertEqual(client.json_obj.call_count,2)
        calls=client.json_obj.call_args_list
        self.assertEqual(calls[0].args[0][1],calls[1].args[0][1])
        self.assertIn('CONTROLLER VALIDATION FEEDBACK',calls[1].args[0][0]['content'])
        schema=calls[1].args[1]
        self.assertEqual(schema['properties']['feedback'],{'type':'string'})
        self.assertEqual(schema['properties']['criteria']['items']['properties']['evidence_refs']['items']['enum'],['a-2'])
        audit=json.loads((self.root/'assessment.json').read_text())
        self.assertEqual(audit['evidence']['a-2'],self.messages[0])
        self.assertEqual(len(audit['errors']),1)

    def test_unavailable_transport_not_retried_by_assessment_layer(self):
        from swe_persona.api_recovery import APITransportExhausted
        for error, expected in ((ValueError('malformed'),2),(APITransportExhausted(3,'TimeoutError'),1)):
            with self.subTest(error=type(error).__name__):
                client=Mock();del client.deadline;client.json_obj.side_effect=error
                with self.assertRaises(protocol.AssessmentUnavailable):self.assess(client)
                self.assertEqual(client.json_obj.call_count,expected)
                audit=json.loads((self.root/'assessment.json').read_text())
                self.assertEqual(audit['status'],'assessment_unavailable')
                self.assertNotIn('output',audit)

    def test_assessment_deadline_is_shared_and_caller_clock_unchanged(self):
        class Deadline:
            expires=1000
            def remaining(self):return self.expires-1
        calls=[]
        class Client:
            deadline=Deadline();request_seconds=2
            def json_obj(client,*args,**kwargs):
                calls.append(client.deadline.expires)
                if len(calls)==1:raise ValueError('Bad assessment')
                return self.review()
        client=Client()
        with patch.object(protocol.time,'monotonic',return_value=10):self.assess(client)
        self.assertEqual(calls,[19,19])
        self.assertEqual(client.deadline.expires,1000)

    def test_scope_integrity_and_cancellation_are_not_unknown(self):
        client=Mock();del client.deadline;client.json_obj.side_effect=KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):self.assess(client)
        changed=deepcopy(self.req);changed['disclosure']+=' Verify security.'
        with self.assertRaisesRegex(ValueError,'cover'):
            protocol.independent_judgment(changed,self.persona,self.messages,[])

    def test_fallback_grades_literal_violation_instead_of_passing_unknown(self):
        for text,expected in ((self.messages[0]['text'],'MET'),
                              (self.messages[0]['text'].replace('Ship.','Run `rm -rf /`.'),'UNMET')):
            self.messages[0]['text']=text
            result,recovery=protocol.independent_judgment(self.req,self.persona,self.messages,[{'error_type':'ValueError'}])
            self.assertEqual(result['verdict'],expected)
            self.assertEqual(recovery['model_assessment'],'unknown')
            self.assertEqual(recovery['check']['verdict'],'pass' if expected=='MET' else 'fail')
        self.messages[0]['turn']=1
        result,_=protocol.independent_judgment(self.req,self.persona,self.messages,[])
        self.assertEqual(result['verdict'],'UNOBSERVABLE')
        self.assertEqual(result['criteria'][0]['evidence_refs'],[])

    def calibrator(self, mode):
        def save(path,value):Path(path).write_text(json.dumps(value))
        def call(messages,schema,**kwargs):
            payload=json.loads(messages[1]['content'])
            visible=payload['eligible_visible_messages']
            code=kwargs['role']=='calibration.code_marker'
            verdict={'pass':'MET','fail':'UNMET','unknown':'UNOBSERVABLE'}[protocol.checker(self.req,visible)['verdict']]
            ref='a-control'
            if code and mode=='unknown':ref='invented'
            if code and mode=='semantic_fail':verdict='MET'
            result=self.review(ref,verdict)
            if not visible:result['criteria'][0]['evidence_refs']=[]
            return result
        client=types.SimpleNamespace(json_obj=Mock(side_effect=call))
        stubs={'bootstrap_adapter':module('bootstrap_adapter',HERE=ADAPTER),
            'registry':module('registry',load_persona=lambda _:self.persona),
            'llm':module('llm',Llm=lambda **_:client,CallLog=lambda **_:None),
            'support':module('support',file_hash=lambda _: 'fingerprint',read=lambda p:json.loads(Path(p).read_text())),
            'execution_policy':module('execution_policy',BudgetClient=lambda c,_:c),
            'build_task':module('build_task',save=save),'protocol':protocol}
        with patch.dict(sys.modules,stubs):calibrator=load('live_calibrator_fixture',ADAPTER/'calibrate_judge.py')
        return calibrator,client

    def test_all_controls_run_after_unknown_and_gate_is_honestly_degraded(self):
        from swe_persona.cli import gate_allows_execution
        calibrator,client=self.calibrator('unknown')
        value=calibrator.calibrate(self.root,'fixture-model')
        self.assertEqual(len(value['controls']),12)
        self.assertEqual(client.json_obj.call_count,13)
        self.assertFalse(value['passed'])
        self.assertEqual(value['status'],'degraded')
        self.assertEqual(value['unavailable_controls'],['code_marker'])
        self.assertTrue(protocol.calibration_allowed(value))
        gate={'profile':'swe-bench-live','passed':False,'execution_allowed':True,
              'worker_exit_code':0,'calibration':value}
        self.assertTrue(gate_allows_execution(gate))
        gate['profile']='swe-together'
        self.assertFalse(gate_allows_execution(gate))
        value['independent_checker_required']=False
        self.assertFalse(protocol.calibration_allowed(value))

    def test_semantically_wrong_valid_judge_blocks_profile_after_all_controls(self):
        calibrator,client=self.calibrator('semantic_fail')
        with self.assertRaisesRegex(ValueError,'semantic controls'):calibrator.calibrate(self.root,'fixture-model')
        value=json.loads((self.root/'calibration.json').read_text())
        self.assertEqual(client.json_obj.call_count,12)
        self.assertEqual(value['semantic_failures'],['code_marker'])
        self.assertFalse(protocol.calibration_allowed(value))
        incomplete=protocol.calibration_report({},value['controls'][:3])
        self.assertFalse(protocol.calibration_allowed(incomplete))

    def test_online_controller_resolves_failure_and_next_valid_judge_has_no_sticky_error(self):
        def save(path,value):Path(path).write_text(json.dumps(value))
        stubs={'bootstrap_adapter':module('bootstrap_adapter',HERE=ADAPTER),
            'agent':module('agent',TerminalAgent=Mock()),
            'llm':module('llm',Llm=Mock(),CallLog=Mock()),
            'support':module('support',**{k:Mock() for k in ('Manifest','Runtime','Snapshots','Checkers','read','file_hash')}),
            'execution_policy':module('execution_policy',BudgetClient=Mock(),Deadline=Mock()),
            'run_reference':module('run_reference',gold_proof=Mock()),
            'roleplay':module('roleplay',FIT={},fit_messages=Mock(),validate_fit=Mock()),
            'build_task':module('build_task',build=Mock(),validate=Mock(),save=save),'protocol':protocol}
        with patch.dict(sys.modules,stubs):controller=load('live_controller_fixture',ADAPTER/'run.py')
        client=Mock();del client.deadline;client.json_obj.return_value=self.review('invented')
        rubric={'criteria':[{'criterion_id':'R1.contract','text':self.req['disclosure']}]}
        args=(client,self.req,self.persona,{},self.messages,2,self.root,protocol.JUDGE_PROMPT,rubric)
        recovered=controller.judge(*args)
        self.assertEqual(recovered['verdict'],'MET')
        self.assertEqual(recovered['assessment_recovery']['model_assessment'],'unknown')
        self.assertEqual(client.json_obj.call_count,2)
        audit=json.loads((self.root/'02-checkpoint.json').read_text())
        self.assertEqual(audit['status'],'assessment_unavailable')
        self.assertTrue((self.root/'02-checkpoint-resolution.json').exists())
        # Only complete independent native+persona verification may earn credit.
        check=recovered['assessment_recovery']['check']
        self.assertEqual(protocol.benchmark_outcome(0,[check],True),'fail')
        self.assertEqual(protocol.benchmark_outcome(None,[check],True),'invalid')
        self.assertEqual(protocol.benchmark_outcome(1,[check],True),'pass')
        client.json_obj.return_value=self.review()
        fresh=controller.judge(*args,final=True)
        self.assertNotIn('assessment_recovery',fresh)
        self.assertEqual(json.loads((self.root/'02-final.json').read_text())['status'],'accepted')


if __name__=='__main__':unittest.main()
