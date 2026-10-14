"""Offline regressions for command recovery and independent grading after stalls."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import importlib
import sys
import types

ROOT = Path(__file__).parents[1]
package = types.ModuleType('fixture_together_limits')
package.__path__ = [str(ROOT / 'src/swe_persona/vendor/swe_together/swe_together_pilot/persona_runtime')]
sys.modules[package.__name__] = package
run = importlib.import_module(package.__name__ + '.public_run')


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_is_tool_result_and_next_command_can_run(self):
        for error in (TimeoutError(), RuntimeError('Command timed out after 180 seconds')):
            env = SimpleNamespace(exec=AsyncMock(side_effect=[error, SimpleNamespace(
                return_code=0, stdout='recovered', stderr='')]))
            rc, output, timed = await run.agent_command(env, 'build', '/repo', 180)
            self.assertEqual((rc, timed), (124, True))
            self.assertIn('may still be running', output)
            self.assertEqual(await run.agent_command(env, 'inspect', '/repo', 180),
                             (0, 'recovered\n', False))

    async def test_unrelated_runtime_failure_is_not_swallowed(self):
        env = SimpleNamespace(exec=AsyncMock(side_effect=RuntimeError('Docker unavailable')))
        with self.assertRaisesRegex(RuntimeError, 'Docker unavailable'):
            await run.agent_command(env, 'build', '/repo', 180)

    async def test_nonzero_exit_preserves_output(self):
        env = SimpleNamespace(exec=AsyncMock(return_value=SimpleNamespace(
            return_code=2, stdout='partial', stderr='compile failed')))
        self.assertEqual(await run.agent_command(env, 'build', '/repo', 180),
                         (2, 'partial\ncompile failed', False))


class StallTests(unittest.TestCase):
    wait = {'action': 'no-op', 'kind': 'wait'}

    def test_four_silent_turns_without_progress_stop(self):
        stall = run.InteractionStall()
        self.assertEqual([stall.observe(self.wait, {'snapshot_sha256': 'a'}, 0, t)
                          for t in range(1, 5)], [False, False, False, True])

    def test_command_workspace_or_user_reply_resets_streak(self):
        for change in ('command', 'workspace', 'reply', 'accept'):
            stall = run.InteractionStall()
            for t in range(1, 4):
                self.assertFalse(stall.observe(self.wait, {'snapshot_sha256': 'a'}, 0, t))
            decision = {'action': change} if change in ('reply', 'accept') else self.wait
            snapshot = 'b' if change == 'workspace' else 'a'
            count = int(change == 'command')
            self.assertFalse(stall.observe(decision, {'snapshot_sha256': snapshot}, count, 4))
            self.assertFalse(stall.observe(self.wait, {'snapshot_sha256': snapshot}, count, 5))

    def test_configurable_threshold_and_disabled_detector(self):
        self.assertFalse(run.InteractionStall(0).observe(self.wait, {'snapshot_sha256': 'a'}, 0, 1))
        self.assertTrue(run.InteractionStall(1).observe(self.wait, {'snapshot_sha256': 'a'}, 0, 1))
        for value in (-1, True, 1.5):
            with self.assertRaises(ValueError):run.InteractionStall(value)

    def test_pending_disclosure_resets_streak(self):
        stall=run.InteractionStall()
        for t in range(1,4):stall.observe(self.wait, {'snapshot_sha256':'a'},0,t)
        self.assertFalse(stall.observe(self.wait, {'snapshot_sha256':'a'},0,4,pending_disclosures=True))
        self.assertEqual(stall.turns,[])


class EpisodeTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self,summary,native_reward=1,native_error=False):
        env = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
        async def execute(command, **kwargs):
            if 'rev-parse HEAD' in command:
                return SimpleNamespace(return_code=0, stdout='a'*40, stderr='')
            if command == 'build':
                raise RuntimeError('Command timed out after 180 seconds')
            return SimpleNamespace(return_code=0, stdout=json.dumps([
                {'ref': 'host:final_diff', 'text': '(no changes)'}]), stderr='')
        env.exec = AsyncMock(side_effect=execute)
        sim = SimpleNamespace(records=[], history=[], user_accepted=False,
                              disclosed={'fixture-request':1},native_disclosed={},finish=AsyncMock(return_value=summary))
        async def step(observation):
            decision = {'action': 'no-op', 'kind': 'wait', 'content': ''}
            sim.records.append({'turn': observation['turn'], 'decision': decision})
            sim.history.append({'role': 'agent', 'text': observation['agent_message']})
            return decision
        sim.step = AsyncMock(side_effect=step)
        api = Mock()
        api.json.side_effect = [{'action': 'command', 'content': 'build', 'completion_claim': False}] + [
            {'action': 'message', 'content': f'Waiting for config ({i})', 'completion_claim': False}
            for i in range(4)]
        client = Mock()
        client.images.get.return_value = SimpleNamespace(attrs={'Config': {'WorkingDir': '/repo'}}, id='image')
        agent_container=Mock()
        agent_container.commit.return_value=SimpleNamespace(id='frozen-work')
        client.containers.list.return_value=[agent_container]
        verifier=Mock()
        verifier.wait.return_value={'StatusCode':0}
        verifier.logs.return_value=b'Native tests completed.\n'
        def verify(*args,**kwargs):
            if native_error:raise RuntimeError('Native verifier could not start')
            logs=next(Path(p) for p,mount in kwargs['volumes'].items() if mount['bind']=='/logs/verifier')
            (logs/'reward.txt').write_text(str(native_reward))
            return verifier
        client.containers.run.side_effect=verify
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory)/'task'; task.mkdir()
            (task/'instruction.md').write_text('Fix code')
            (task/'user_simulation_prompt.md').write_text('User context')
            (task/'task.toml').write_text('[environment]\ncpus=1\nmemory_mb=512\n')
            out = Path(directory)/'episode'
            binding = {'original_instruction': 'Fix code', 'original_user_context': 'User context',
                       'task_id': 'fixture', 'persona': {'id': 'fixture','bound_evolved_requirements':[{'id':'fixture-request'}]},
                       'binding_sha256': 'fixture'}
            with patch.dict(os.environ,{'SWE_PERSONA_TOGETHER_STALL_TURNS':'4'}), \
                 patch.object(run, 'validate'), patch.object(run, 'fingerprint', return_value='fixture'), \
                 patch.object(run, 'initial_persona_message', return_value=''), \
                 patch.object(run, 'Calls'), patch.object(run, 'UserSimulator', return_value=sim), \
                 patch.object(run, 'RoleAPI', return_value=api), \
                 patch('docker.from_env', return_value=client), \
                 patch('harbor.environments.docker.docker.DockerEnvironment', return_value=env):
                result = await run.episode(binding, task, {}, out, 'fixture')
            self.assertEqual(result['stop_reason'], 'interaction_stalled')
            self.assertEqual(api.json.call_count, 5)
            sim.finish.assert_awaited_once()
            env.stop.assert_awaited_once_with(delete=True)
            client.containers.run.assert_called_once()
            self.assertEqual(client.containers.run.call_args.kwargs['volumes'][str(task/'tests')]['mode'],'ro')
            for name in ('transcript.json', 'persona-evidence.json', 'persona-turns.json',
                         'stall-evidence.json', 'persona-result.json', 'executions.json'):
                self.assertTrue((out/name).is_file(), name)
            self.assertTrue(json.loads((out/'executions.json').read_text())[0]['timed_out'])
            self.assertEqual(json.loads((out/'stall-evidence.json').read_text())['turns'], [1,2,3,4])
            self.assertEqual(result['interaction_stall_policy']['threshold'],4)
            if not native_error:
                verifier.remove.assert_called_once_with(force=True,v=True)
                self.assertTrue((out/'native-execution.json').is_file())
            from swe_persona.cli import normalize_result
            row={'interaction':{'profile_id':'swe-together'},'task_id':'fixture',
                 'source_benchmark':'SWE-Together','source_subset':'fixture','content_sha256':'fixture'}
            normalized=normalize_result(row,out,0 if result['benchmark_outcome'] in ('pass','fail') else 2)
            return result,normalized

    def summary(self,status='unknown',valid=True,invalid_reasons=None):
        return {'persona_status':status,'evaluation_valid':valid,'user_accepted':False,
                'requirements':[{'requirement_id':'fixture-request','status':status}],
                'undisclosed':[],'invalid_reasons':invalid_reasons or []}

    async def test_timeout_then_stall_grades_native_keeps_unknown_and_is_valid_fail(self):
        result,normalized=await self.exercise(self.summary())
        self.assertEqual(result['benchmark_outcome'],'fail')
        self.assertEqual(result['original_reward'],1)
        self.assertFalse(result['user_accepted'])
        self.assertFalse(result['interaction_stall_assessment']['api_interrupted'])
        self.assertNotIn('api_recovery_assessment',result)
        self.assertFalse(result['interaction_stall_assessment']['verification_complete'])
        self.assertIs(normalized['native_pass'],True)
        self.assertIsNone(normalized['persona_pass'])
        self.assertEqual(normalized['status'],'fail')
        self.assertEqual(normalized['outcome_reason'],'interaction_stalled')
        self.assertIn('interaction_stall_assessment',normalized)
        self.assertNotIn('api_recovery_assessment',normalized)

    async def test_stall_preserves_native_failure(self):
        result,normalized=await self.exercise(self.summary(),native_reward=0)
        self.assertEqual(result['benchmark_outcome'],'fail')
        self.assertIs(normalized['native_pass'],False)

    async def test_stall_preserves_verified_persona_failure(self):
        result,normalized=await self.exercise(self.summary(status='not_met'))
        self.assertEqual(result['benchmark_outcome'],'fail')
        self.assertIs(normalized['persona_pass'],False)
        self.assertTrue(normalized['verification_complete'])

    async def test_stall_does_not_clear_integrity_invalid(self):
        result,normalized=await self.exercise(self.summary(valid=False,invalid_reasons=['modified_evidence']))
        self.assertEqual(result['benchmark_outcome'],'invalid')
        self.assertEqual(normalized['status'],'invalid')

    async def test_final_judge_unavailable_is_unknown_not_invalid(self):
        result,normalized=await self.exercise(self.summary(valid=False))
        self.assertEqual(result['benchmark_outcome'],'fail')
        self.assertIsNone(normalized['persona_pass'])

    async def test_native_environment_failure_remains_infrastructure_error(self):
        result,normalized=await self.exercise(self.summary(),native_error=True)
        self.assertEqual(result['benchmark_outcome'],'infrastructure_error')
        self.assertIsNone(normalized['native_pass'])
        self.assertIsNone(result['original_reward'])


class RealSimulatorTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        pilot=ROOT/'src/swe_persona/vendor/swe_together/swe_together_pilot'
        case=json.loads((pilot/'persona_runtime/calibration-cases.json').read_text())[0]
        return json.loads((pilot/case['binding_file']).read_text()),case

    async def test_real_silence_stalls_then_final_review_keeps_acceptance_unknown(self):
        binding,case=self.fixture()
        async def call(role,system,payload):
            self.assertEqual(role,'user_simulator')
            return {'action':'no-op','kind':'wait','content':'','requirement_id':None,
                    'base_task_complete':None,'completion_reason':None}
        sim=run.UserSimulator(binding,call)
        sim.disclosed={r['id']:case['activation_turn'] for r in binding['persona']['bound_evolved_requirements']}
        async def review(req,evidence,phase):
            return {'requirement_id':req['id'],'status':'met','evidence_refs':[],
                    'reason':'Fixture verifier','revision':evidence['revision']}
        sim._review_requirement=AsyncMock(side_effect=review)
        stall=run.InteractionStall()
        for index in range(1,5):
            turn=case['activation_turn']+index
            decision=await sim.step({'turn':turn,'events':[],'agent_message':'Waiting for configuration.',
                'completion_attempt':False,'interaction_checkpoint':True,
                'remaining_agent_turns':100,'evidence':deepcopy(case['evidence'])})
            self.assertEqual(decision['action'],'no-op')
            self.assertEqual(stall.observe(decision,case['evidence'],0,turn),index==4)
        with patch.object(sys.modules[run.UserSimulator.__module__],'audit_interaction',
                          AsyncMock(return_value={'status':'pass'})):
            summary=await sim.finish(deepcopy(case['evidence']))
        self.assertTrue(summary['evaluation_valid'])
        self.assertEqual(summary['requirements'][0]['status'],'met')
        self.assertEqual(summary['persona_status'],'unknown')
        self.assertFalse(summary['user_accepted'])
        self.assertIn('final',[c.args[2] for c in sim._review_requirement.await_args_list])

    async def test_real_pending_requirement_feedback_does_not_count_as_silence(self):
        binding,case=self.fixture()
        async def call(*args):
            return {'action':'no-op','kind':'wait','content':'','requirement_id':None,
                    'base_task_complete':None,'completion_reason':None}
        sim=run.UserSimulator(binding,call)
        sim.disclosed={r['id']:case['activation_turn'] for r in binding['persona']['bound_evolved_requirements']}
        sim._review_requirement=AsyncMock(return_value={'status':'not_met','requirement_id':
            binding['persona']['bound_evolved_requirements'][0]['id'],'reason':'Missing deliverable'})
        stall=run.InteractionStall()
        for index in range(1,6):
            turn=case['activation_turn']+index
            decision=await sim.step({'turn':turn,'events':[],'agent_message':'Please review.',
                'completion_attempt':False,'interaction_checkpoint':True,
                'remaining_agent_turns':100,'evidence':deepcopy(case['evidence'])})
            self.assertEqual(decision['action'],'message')
            self.assertTrue(sim.records[-1]['completion_gate_override'])
            self.assertFalse(stall.observe(decision,case['evidence'],0,turn))

    async def test_silence_cannot_stop_before_a_frozen_disclosure(self):
        binding,case=self.fixture()
        req=binding['persona']['bound_evolved_requirements'][0]
        req.update(earliest_turn=8,deadline_turn=8)
        digest=sys.modules[run.UserSimulator.__module__].digest
        binding['binding_sha256']=digest({k:v for k,v in binding.items() if k!='binding_sha256'})
        async def call(role,system,payload):
            if payload['due']:
                return {'action':'message','kind':'persona','content':payload['due']['statement'],
                        'requirement_id':payload['due']['id'],'base_task_complete':None,'completion_reason':None}
            return {'action':'no-op','kind':'wait','content':'','requirement_id':None,
                    'base_task_complete':None,'completion_reason':None}
        sim=run.UserSimulator(binding,call)
        stall=run.InteractionStall()
        for turn in range(1,9):
            decision=await sim.step({'turn':turn,'events':[],'agent_message':'Waiting for feedback.',
                'completion_attempt':False,'interaction_checkpoint':True,
                'remaining_agent_turns':100,'evidence':deepcopy(case['evidence'])})
            pending=req['id'] not in sim.disclosed
            self.assertFalse(stall.observe(decision,case['evidence'],0,turn,pending_disclosures=pending))
            self.assertEqual(decision['action'],'no-op' if turn<8 else 'message')
        self.assertEqual(sim.disclosed[req['id']],8)


if __name__ == '__main__':
    unittest.main()
