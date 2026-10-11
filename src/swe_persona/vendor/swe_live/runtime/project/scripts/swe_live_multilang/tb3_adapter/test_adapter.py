import bootstrap_adapter
from bootstrap_adapter import ROOT
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from registry import load_persona
from protocol import requirement,checker,validate_judgment,benchmark_outcome,FIELDS,visible_persona
from build_task import build,validate,save
from run_batch import draw,next_slot,report,run,passed,trial_projection


def native_fixture():
    local=ROOT/'.local/multilang-roleplay/fullset-once-v3/tasks/0894-ts/task.private.jsonl'
    if local.exists():return local
    # A copied delivery package carries its own native row; no original PC path is needed.
    return next((ROOT.parents[1]/'tasks').glob('*/tests/native-task.jsonl'),local)


class ProtocolTests(unittest.TestCase):
    def test_judge_persona_view_excludes_undisclosed_templates(self):
        for name in FIELDS:
            value=visible_persona(load_persona(name))
            self.assertNotIn('evolved_requirements',value['attributes'])
            self.assertEqual(set(value['attributes']),{'technical_context','primary_goal','interaction_preferences','constraints'})

    def test_all_ten_shapes_have_discriminating_controls(self):
        for name,labels in FIELDS.items():
            req=requirement(load_persona(name))
            reply='\n'.join('- '+label+': Confirmed.' for label in labels)
            good=[{'message_id':'a-2','text':reply}]
            self.assertEqual(checker(req,good)['verdict'],'pass',name)
            self.assertEqual(checker(req,[{'message_id':'a-2','text':reply+'\n- Extra: Unrequested.'}])['verdict'],'fail')
            self.assertEqual(checker(req,[])['verdict'],'unknown')

    def test_manager_code_fails_even_with_correct_labels(self):
        req=requirement(load_persona('manager_no_code'))
        self.assertEqual(checker(req,[{'message_id':'a','text':'- Recommendation: `cmd`\n- Progress: X\n- Decision: Y'}])['verdict'],'fail')

    def test_judge_cannot_cite_before_disclosure_or_invent_quote(self):
        req=requirement(load_persona('manager_no_code'))
        m=[{'role':'agent','message_id':'a-1','turn':1,'text':'confirmed'}]
        r={'requirement_id':'R1','verdict':'MET','criteria':[{'criterion_id':'R1.contract','status':'MET',
            'evidence':[{'event_id':'a-1','quote':'confirmed'}],'reason':'x'}],'reason':'x','feedback':''}
        with self.assertRaises(ValueError):validate_judgment(r,req,m,2)
        m[0]['turn']=2;validate_judgment(r,req,m,2)
        r['criteria'][0]['evidence'][0]['quote']='invented'
        with self.assertRaises(ValueError):validate_judgment(r,req,m,2)

    def test_met_requires_evidence_and_matching_aggregation(self):
        req=requirement(load_persona('manager_no_code'))
        r={'requirement_id':'R1','verdict':'MET','criteria':[{'criterion_id':'R1.contract','status':'MET','evidence':[]}]}
        with self.assertRaises(ValueError):validate_judgment(r,req,[],2)
        r['criteria'][0]['status']='UNMET'
        with self.assertRaises(ValueError):validate_judgment(r,req,[],2)

    def test_online_met_never_overrides_native_or_objective_failure(self):
        self.assertEqual(benchmark_outcome(0,[{'verdict':'pass'}],True),'fail')
        self.assertEqual(benchmark_outcome(1,[{'verdict':'fail'}],True),'fail')
        self.assertEqual(benchmark_outcome(None,[{'verdict':'pass'}],True),'invalid')
        self.assertEqual(benchmark_outcome(1,[{'verdict':'pass'}],False),'fail')
        self.assertEqual(benchmark_outcome(1,[{'verdict':'pass'}],True),'pass')

    def test_frozen_task_preserves_native_inputs_and_detects_mutation(self):
        source=native_fixture()
        if not source.exists(): self.skipTest('Pinned local fixture is not installed')
        with TemporaryDirectory() as temp:
            target=Path(temp)/'task';build(source,'manager_no_code',target,212.68)
            self.assertEqual((target/'tests/native-task.jsonl').read_bytes(),source.read_bytes())
            self.assertTrue(validate(target)['structure_valid'])
            (target/'tests/persona/closed_loop/rubric.json').write_text('{}')
            with self.assertRaises(ValueError):validate(target)

    def test_worker_runs_final_recheck_and_independent_acceptance(self):
        from run import main as worker_main
        source=native_fixture()
        if not source.exists():self.skipTest('Pinned local task unavailable')
        row=json.loads(source.read_text(encoding='utf-8')); calls=[]
        reply='- Recommendation: Review delivery.\n- Progress: Local checks completed.\n- Decision: Confirm release approval.'
        class FakeLLM:
            def json_obj(self,messages,schema,**kwargs):
                calls.append(kwargs['role']);payload=json.loads(messages[-1]['content'])
                if kwargs['role']=='adapter.fit':
                    return {'decision':'applicable','reason':'Communication scope fits','public_quote':'','opening':'Please help fix this issue.'}
                if kwargs['role'].startswith('simulator'):
                    return {'message':'Thank you.','satisfied':True}
                self_payload=payload['eligible_visible_messages']
                assert self_payload and all(m['turn']>=2 and m['role']=='agent' for m in self_payload)
                assert 'evolved_requirements' not in payload['persona']['attributes']
                return {'requirement_id':'R1','verdict':'MET','criteria':[{'criterion_id':'R1.contract','status':'MET',
                    'evidence':[{'event_id':'a-2','quote':reply}],'reason':'All disclosed structural obligations visible'}],
                    'feedback':'','reason':'Evidence complete'}
        runtime=SimpleNamespace(start=lambda:None,remove=lambda:None,sh=lambda *a,**k:(0,row['base_commit'],None))
        agent=SimpleNamespace(run_turn=lambda message,turn:SimpleNamespace(
            reply='What is the issue?' if turn==1 else reply,status='working' if turn==1 else 'done',to_json=lambda:{}))
        snapshots=SimpleNamespace(capture=lambda turn:(None,None,{'patch':'candidate diff'}))
        verify=SimpleNamespace(official=lambda patch:('pass','official_tests_pass',{'reward':1}))
        with TemporaryDirectory() as temp:
            directory=Path(temp);binding=directory/'binding';build(source,'manager_no_code',binding,212.68)
            client=SimpleNamespace(images=SimpleNamespace(get=lambda name:SimpleNamespace(id='image')),close=lambda:None)
            def log_client(**kwargs):
                kwargs['call_log'].add({'model':'offline','event':'directory_exists'})
                return object()
            with patch('run.Llm',side_effect=log_client),patch('run.BudgetClient',return_value=FakeLLM()),\
                 patch('docker.from_env',return_value=client),patch('run.gold_proof',return_value={}),\
                 patch('run.Runtime',return_value=runtime),patch('run.Snapshots',return_value=snapshots),\
                 patch('run.TerminalAgent',return_value=agent),patch('run.Checkers',return_value=verify):
                rc=worker_main(['--task',str(source),'--persona','manager_no_code','--gold-dir',str(directory/'gold'),
                    '--binding-dir',str(binding),'--sample-seconds','212.68','--output-dir',str(directory/'worker')])
            result=json.loads((directory/'worker/persona-result.json').read_text())
            self.assertEqual(rc,0);self.assertEqual(result['benchmark_outcome'],'pass')
            self.assertEqual(calls,['adapter.fit','requirement.turn2','requirement.final','simulator.turn3'])
            self.assertTrue((directory/'worker/agent-private/requirement-judge-audit/02-final.json').exists())


class RotationTests(unittest.TestCase):
    def test_supervisor_timeout_overrides_late_pass_and_keeps_skips_invalid(self):
        for status,outcome in [('timeout','fail'),('not_applicable','invalid'),('interrupted','invalid')]:
            with self.subTest(status=status), TemporaryDirectory() as temp:
                directory=Path(temp)
                save(directory/'worker/persona-result.json',{'original_reward':1,'benchmark_outcome':'pass'})
                trial_projection(directory,{'status':status})
                value=json.loads((directory/'persona-result.json').read_text())
                self.assertEqual(value['benchmark_outcome'],outcome)
                self.assertIsNone(value['original_reward'])
                self.assertEqual(value['diagnostic_original_reward'],1)

    def test_draw_is_reproducible_distinct_and_from_original_pool(self):
        for i in range(1077):
            pair=draw(list(FIELDS),'seed',str(i))
            self.assertEqual(pair,draw(list(reversed(FIELDS)),'seed',str(i)))
            self.assertEqual(len(set(pair)),2);self.assertTrue(set(pair)<=set(FIELDS))

    def test_failure_gets_one_fallback_success_stops(self):
        fail={'status':'completed','joint_success':0}
        good={'status':'completed','joint_success':1}
        self.assertEqual(next_slot([]),0);self.assertEqual(next_slot([fail]),1)
        self.assertIsNone(next_slot([good]));self.assertIsNone(next_slot([fail,fail]))
        self.assertFalse(passed({'status':'timeout','joint_success':1}))

    def test_run_then_resume_never_resamples_or_repeats(self):
        with TemporaryDirectory() as temp:
            study=Path(temp);calls=[]
            task={'key':'test','instance_id':'org__repo-1','personas':['manager_no_code','student_needing_explanation'],
                  'historical_coverage':False,'path':'unused'}
            plan={'tasks':[task],'initial_seconds':212.68,'maximum_seconds':3600,'update_every':20,
                  'historical_tasks':0,'total_tasks':1,'model':'offline'}
            def sample(command,directory,seconds,cleanup):
                directory.mkdir(parents=True);(directory/'worker').mkdir()
                if '--setup-task' in command:
                    save(directory/'worker/setup.json',{'gold_dir':'gold','image_id':'fake','pulled':False})
                    receipt={'status':'completed','seconds':1,'cap_seconds':seconds}
                else:
                    calls.append(command[command.index('--persona')+1])
                    receipt={'status':'completed','seconds':10,'cap_seconds':seconds,'base_task_success':1,
                             'joint_success':int(len(calls)==2)}
                save(directory/'result.json',receipt);return receipt
            with patch('calibrate_judge.calibrate'),patch('run_batch.build',return_value=study/'binding'),patch('run_batch.supervise_sample',side_effect=sample):
                run(study,plan);run(study,plan)
            self.assertEqual(calls,task['personas'])
            summary=report(study,plan)[0]
            self.assertEqual(summary['processed_tasks'],1);self.assertEqual(summary['new_benchmark_passes'],1)
            self.assertEqual(summary['next_cap_seconds'],212.68);self.assertIsNone(summary['global_deadline'])

    def test_run_first_pass_stops_and_two_failures_exhaust(self):
        for outcomes in ([1],[0,0]):
            with self.subTest(outcomes=outcomes), TemporaryDirectory() as temp:
                study=Path(temp);calls=[]
                task={'key':'test','instance_id':'org__repo-1','personas':['manager_no_code','student_needing_explanation'],
                      'historical_coverage':False,'path':'unused'}
                plan={'tasks':[task],'initial_seconds':212.68,'maximum_seconds':3600,'update_every':20,
                      'historical_tasks':0,'total_tasks':1,'model':'offline'}
                def sample(command,directory,seconds,cleanup):
                    directory.mkdir(parents=True);(directory/'worker').mkdir()
                    if '--setup-task' in command:
                        save(directory/'worker/setup.json',{'gold_dir':'gold','image_id':'fake','pulled':False})
                        receipt={'status':'completed','seconds':1,'cap_seconds':seconds}
                    else:
                        calls.append(command[command.index('--persona')+1])
                        receipt={'status':'completed','seconds':10,'cap_seconds':seconds,'base_task_success':1,
                                 'joint_success':outcomes[len(calls)-1]}
                    save(directory/'result.json',receipt);return receipt
                with patch('calibrate_judge.calibrate'),patch('run_batch.build',return_value=study/'binding'),patch('run_batch.supervise_sample',side_effect=sample):
                    run(study,plan);run(study,plan)
                self.assertEqual(len(calls),len(outcomes))
                summary=report(study,plan)[0]
                self.assertEqual(summary['processed_tasks'],1)
                self.assertEqual(summary['new_benchmark_passes'],int(any(outcomes)))

    def test_online_p20_excludes_invalid_and_only_changes_after_twenty_observations(self):
        from collection.online import budget_after
        observations=[{'status':'completed','seconds':100+i,'cap_seconds':212.68} for i in range(4)]
        observations += [{'status':'timeout','seconds':213,'cap_seconds':212.68} for _ in range(16)]
        self.assertEqual(budget_after(observations[:19],212.68)['next_cap_seconds'],212.68)
        observations += [{'status':'not_applicable','seconds':.01,'cap_seconds':212.68} for _ in range(10)]
        result=budget_after(observations,212.68)
        self.assertEqual(result['estimate']['population'],20)
        self.assertEqual(result['estimate']['seconds'],103)
        self.assertAlmostEqual(result['next_cap_seconds'],212.68*.75)


if __name__=='__main__':unittest.main()
