"""Offline regressions for Harbor timeouts and unscored silent-loop exits."""
import json
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


class EpisodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_then_stall_saves_evidence_cleans_up_and_never_grades(self):
        env = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
        async def execute(command, **kwargs):
            if 'rev-parse HEAD' in command:
                return SimpleNamespace(return_code=0, stdout='a'*40, stderr='')
            if command == 'build':
                raise RuntimeError('Command timed out after 180 seconds')
            return SimpleNamespace(return_code=0, stdout=json.dumps([
                {'ref': 'host:final_diff', 'text': '(no changes)'}]), stderr='')
        env.exec = AsyncMock(side_effect=execute)
        sim = SimpleNamespace(records=[], history=[], user_accepted=False, finish=AsyncMock())
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
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory)/'task'; task.mkdir()
            (task/'instruction.md').write_text('Fix code')
            (task/'user_simulation_prompt.md').write_text('User context')
            (task/'task.toml').write_text('[environment]\ncpus=1\nmemory_mb=512\n')
            out = Path(directory)/'episode'
            binding = {'original_instruction': 'Fix code', 'original_user_context': 'User context',
                       'task_id': 'fixture', 'persona': {'id': 'fixture'}, 'binding_sha256': 'fixture'}
            with patch.object(run, 'validate'), patch.object(run, 'fingerprint', return_value='fixture'), \
                 patch.object(run, 'initial_persona_message', return_value=''), \
                 patch.object(run, 'Calls'), patch.object(run, 'UserSimulator', return_value=sim), \
                 patch.object(run, 'RoleAPI', return_value=api), \
                 patch('docker.from_env', return_value=client), \
                 patch('harbor.environments.docker.docker.DockerEnvironment', return_value=env):
                result = await run.episode(binding, task, {}, out, 'fixture')
            self.assertEqual(result['stop_reason'], 'interaction_stalled')
            self.assertEqual(result['benchmark_outcome'], 'invalid')
            self.assertIsNone(result['original_reward'])
            self.assertFalse(result['user_accepted'])
            self.assertEqual(api.json.call_count, 5)
            sim.finish.assert_not_awaited()
            env.stop.assert_awaited_once_with(delete=True)
            client.containers.run.assert_not_called()
            for name in ('transcript.json', 'persona-evidence.json', 'persona-turns.json',
                         'stall-evidence.json', 'persona-result.json', 'executions.json'):
                self.assertTrue((out/name).is_file(), name)
            self.assertTrue(json.loads((out/'executions.json').read_text())[0]['timed_out'])
            self.assertEqual(json.loads((out/'stall-evidence.json').read_text())['turns'], [1,2,3,4])


if __name__ == '__main__':
    unittest.main()
