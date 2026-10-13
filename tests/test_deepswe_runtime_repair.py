"""Regression coverage for observed DeepSWE network and reference failures."""
import asyncio
from copy import deepcopy
import importlib
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).parents[1]
PACKAGE = 'fixture_deepswe_runtime_repair'
package = ModuleType(PACKAGE)
package.__path__ = [str(ROOT/'src/swe_persona/vendor/deepswe/project/dspilot')]
sys.modules[PACKAGE] = package
judge = importlib.import_module(PACKAGE+'.requirement_judge')
runtime = importlib.import_module(PACKAGE+'.harbor_runtime')
assessment = importlib.import_module(PACKAGE+'.export_tb3_results')
delivery = importlib.import_module(PACKAGE+'.delivery_runtime')
from swe_persona.harbor_bridge import cleanup_empty_session_networks


class DeepSWEWorkerImportTests(unittest.TestCase):
    def test_child_imports_exact_parent_package_without_ambient_paths(self):
        import swe_persona
        project = ROOT/'src/swe_persona/vendor/deepswe/project'
        pair = {'task_id':'fixture', 'persona_id':'fixture'}
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, PYTHONPATH=temp):
            env = delivery.worker_env(project,pair,'harbor')
            self.assertNotIn(temp,env['PYTHONPATH'].split(os.pathsep))
            child = subprocess.run([sys.executable,'-B','-c',
                'import json,swe_persona.output_mode,dspilot.api; '
                'print(json.dumps([swe_persona.output_mode.__file__,dspilot.api.__file__]))'],
                cwd=project,env=env,capture_output=True,text=True,timeout=15)
        self.assertEqual(child.returncode,0,child.stderr)
        paths = json.loads(child.stdout)
        self.assertEqual(Path(paths[0]).resolve(),Path(swe_persona.__file__).resolve().parent/'output_mode.py')
        self.assertEqual(Path(paths[1]).resolve(),project/'dspilot/api.py')


class DeepSWEReferenceTests(unittest.TestCase):
    def fixture(self, content):
        identity = {'run_id':'run', 'task_id':'task', 'persona_id':'persona'}
        events = [dict(identity,event_id='disclosure',speaker='user',content='Fix the issue.'),
                  dict(identity,event_id='delivery',speaker='agent',user_visible=True,content=content)]
        rule = {'requirement_id':'scope', 'criteria':[{'criterion_id':'scope.contract'}]}
        return identity,events,rule

    def vote(self, refs):
        return {'requirement_id':'scope', 'verdict':'MET', 'reason':'Visible support.',
                'feedback':'The requirement is satisfied.',
                'criteria':[{'criterion_id':'scope.contract', 'status':'MET',
                             'reason':'Visible support.',
                             'evidence':[{'reference_id':key} for key in refs]}]}

    def test_registered_json_lines_all_pass_the_real_validator(self):
        content = 'Task paths and manifest:\n```json\n{\n  "new_files": [\n    "ast/node.go"\n  ],\n  "protected_files": ["persona-user-notes.txt"]\n}\n```'
        identity,events,rule = self.fixture(content)
        eligible = judge.eligible_messages(events,'disclosure',identity)
        refs = judge.visible_references(eligible)
        original = deepcopy(events)
        self.assertTrue(refs)
        self.assertFalse(any(r['quote'].strip() in ('{','}','],','```') for r in refs.values()))
        for key in refs:
            resolved = judge.resolve_visible_references(self.vote([key]),refs)
            judge.validate(resolved,rule,events,'disclosure',identity)
        client = Mock()
        client.create.side_effect = lambda messages,**kwargs: {'text':json.dumps(self.vote(
            json.loads(messages[-1]['content'])['registered_visible_references']))}
        client.text.side_effect = lambda response: response['text']
        with patch.dict('os.environ',{'DS_RUNTIME':'harbor'}):
            result = judge.judge(client,rule,events,'disclosure',identity,'Fix the issue.','persona')
        self.assertEqual(result['status'],'met')
        self.assertEqual(client.create.call_count,1)
        self.assertEqual(events,original)

    def test_punctuation_only_messages_offer_no_evidence(self):
        identity,events,rule = self.fixture('  ],\n}\n```\n— …\n\t')
        eligible = judge.eligible_messages(events,'disclosure',identity)
        self.assertEqual(judge.visible_references(eligible),{})
        vote = self.vote({})
        vote['verdict'] = vote['criteria'][0]['status'] = 'UNOBSERVABLE'
        client = Mock();client.create.return_value = {'text':json.dumps(vote)}
        client.text.side_effect = lambda response: response['text']
        with patch.dict('os.environ',{'DS_RUNTIME':'harbor'}):
            result = judge.judge(client,rule,events,'disclosure',identity,'Fix the issue.','persona')
        self.assertEqual(result['status'],'unknown')
        evidence = client.create.call_args.kwargs['schema']['properties']['criteria']['items']['properties']['evidence']
        self.assertEqual(evidence['maxItems'],0)

    def test_foreign_and_pre_disclosure_messages_are_not_registered(self):
        identity,events,rule = self.fixture('Allowed evidence.')
        events.insert(0,dict(identity,event_id='before',speaker='agent',user_visible=True,content='Before disclosure.'))
        events.append(dict(identity,event_id='hidden',speaker='agent',user_visible=False,content='Hidden.'))
        events.append(dict(identity,event_id='foreign',speaker='agent',user_visible=True,task_id='other',content='Other task.'))
        refs = judge.visible_references(judge.eligible_messages(events,'disclosure',identity))
        self.assertEqual([r['quote'] for r in refs.values()],['Allowed evidence.'])
        with self.assertRaises(ValueError):
            judge.resolve_visible_references(self.vote(['invented']),refs)

    def test_final_evaluator_error_preserves_native_result_and_prevents_joint_pass(self):
        # The observed httpx result is a rejected evaluator verdict, not an
        # independently established persona failure or a joint capability pass.
        result = {'arm':'pivot', 'base_pass':True, 'persona_pass':None,
                  'joint_pass':None, 'original_evaluation_valid':True,
                  'original_report_eligible':True, 'persona_evaluation_valid':False,
                  'evaluation_valid':True,
                  'final_error':'Final review: JudgeOutputError: Missing required evidence sources: explanation'}
        self.assertEqual(assessment.benchmark_assessment(result)['status'],'infrastructure_error')
        self.assertIs(result['base_pass'],True)
        self.assertIsNone(result['persona_pass'])


class DeepSWENetworkTests(unittest.TestCase):
    def start(self, temp, env):
        box = SimpleNamespace(name='test-box')
        with patch.dict('os.environ',{'DS_HARBOR_WORK_ROOT':temp}), \
                patch.object(runtime,'metadata'), \
                patch('harbor.environments.docker.docker.DockerEnvironment',return_value=env):
            runtime.start(box,None,'cpu-test-image',{'memory':'128m','cpus':1})
        return box

    def test_session_is_recorded_under_host_directory_and_normal_exit_cleans_network(self):
        env = SimpleNamespace(start=AsyncMock(),stop=AsyncMock())
        with tempfile.TemporaryDirectory() as temp, \
                patch('swe_persona.harbor_bridge.remove_empty_session_network') as cleanup:
            box = self.start(temp,env)
            self.assertEqual(box._harbor_root.parent,Path(temp))
            self.assertEqual(box._harbor_project,'ds-'+box._harbor_root.name.removeprefix('deepswe-harbor-'))
            self.assertTrue((box._harbor_root/'environment/docker-compose.yaml').exists())
            runtime.close(box);runtime.close(box)
            env.stop.assert_awaited_once_with(delete=False)
            cleanup.assert_called_once_with(box._harbor_root,box._harbor_project)
            self.assertTrue(box._harbor_loop.is_closed())

    def test_failed_start_also_cleans_network_without_masking_original_error(self):
        env = SimpleNamespace(start=AsyncMock(side_effect=RuntimeError('address pool exhausted')),
                              stop=AsyncMock(side_effect=ValueError('teardown failed')))
        with tempfile.TemporaryDirectory() as temp, \
                patch('swe_persona.harbor_bridge.remove_empty_session_network') as cleanup, \
                self.assertWarns(RuntimeWarning):
            with self.assertRaisesRegex(RuntimeError,'address pool exhausted'):
                self.start(temp,env)
            cleanup.assert_called_once()
            self.assertTrue((cleanup.call_args.args[0]/'environment/docker-compose.yaml').exists())

    def test_outer_worker_reclaims_recorded_ds_session_after_interruption(self):
        project = 'ds-123456abcdef'
        inspection = SimpleNamespace(returncode=0,stderr='',stdout=json.dumps([{
            'Id':'owned-ds-id','Labels':{'com.docker.compose.project':project},'Containers':{}}]))
        with tempfile.TemporaryDirectory() as temp, \
                patch('swe_persona.harbor_bridge.subprocess.run',side_effect=[inspection,
                    SimpleNamespace(returncode=0,stderr='')]) as docker:
            root = Path(temp)/'environments/deepswe-harbor-123456abcdef'
            (root/'environment').mkdir(parents=True)
            (root/'environment/docker-compose.yaml').write_text('services: {}')
            receipt = cleanup_empty_session_networks(temp)
            self.assertEqual(receipt['sessions'][0]['project'],project)
            self.assertTrue(receipt['sessions'][0]['removed'])
            self.assertEqual(docker.call_args.args[0],['docker','network','rm','owned-ds-id'])

    def test_symlinked_host_cache_cannot_authorize_cleanup(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as outside, \
                patch('swe_persona.harbor_bridge.subprocess.run') as docker:
            root = Path(outside)/'deepswe-harbor-123456abcdef/environment'
            root.mkdir(parents=True);(root/'docker-compose.yaml').write_text('services: {}')
            (Path(temp)/'environments').symlink_to(outside)
            self.assertEqual(cleanup_empty_session_networks(temp)['sessions'],[])
            docker.assert_not_called()


if __name__ == '__main__':
    unittest.main()
