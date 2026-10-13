"""Regression tests for recoverable online assessment failures and later recovery."""
import asyncio
from copy import deepcopy
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, Mock, AsyncMock

ROOT = Path(__file__).parents[1]
TB3 = ROOT / 'src/swe_persona/vendor/tb3/runtime'
TOGETHER = ROOT / 'src/swe_persona/vendor/swe_together/swe_together_pilot'
spec = importlib.util.spec_from_file_location('fixture_requirement_loop', TB3 / 'requirement_loop.py')
tb3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tb3)
package = types.ModuleType('fixture_together_recovery')
package.__path__ = [str(TOGETHER / 'persona_runtime')]
sys.modules[package.__name__] = package
swt = importlib.import_module(package.__name__ + '.simulator')


class TB3OnlineRecoveryTests(unittest.TestCase):
    def fixture(self):
        requirements = [{'id': rid, 'disclosure': 'Implement the current request.',
                         'depends_on': ['R1'] if rid == 'R2' else [], 'earliest_turn': 2,
                         'deadline_turn': 3, 'trigger_event': 'first_plan'} for rid in ('R1', 'R2')]
        binding = {'binding_sha256': 'fixture', 'public_contract': {'instruction': 'Fix the code.'},
                   'persona': {}, 'proposal': {'requirements': requirements}}
        loop = tb3.RequirementLoop(binding, tb3.compile_rubric(binding))
        loop.disclose('R1', 1)
        events = [{'kind': 'initial_instruction', 'data': {'content': 'Fix the code.'}},
                  {'kind': 'simulator', 'data': {'message': 'Implement the current request.'}},
                  {'kind': 'agent_action', 'data': {'message': 'the CLI keeps the same defaults.'}},
                  {'kind': 'agent_action', 'data': {'message': 'Final compile and requested unittest run.'}},
                  {'kind': 'agent_action', 'data': {'message': 'I will capture the requested CLI checks.'}}]
        return binding, loop, events

    def review(self, event_id='e000002'):
        return {'requirement_id': 'R1', 'verdict': 'MET',
                'criteria': [{'criterion_id': 'R1.contract', 'status': 'MET',
                              'evidence_refs': [event_id], 'reason': 'Visible evidence.'}],
                'feedback': 'The request is satisfied.', 'reason': 'Visible evidence.'}

    def test_internal_repair_uses_registered_refs_and_unchanged_host_text(self):
        for bad_id in ('invented', 'e000001'):
            with self.subTest(bad_id=bad_id), tempfile.TemporaryDirectory() as directory:
                binding, loop, events = self.fixture()
                events[2]['data']['message'] = '    cargo bins picked from the public config.\nThe exact unittest discovery runs.'
                call = Mock(side_effect=[self.review(bad_id), self.review()])
                result = tb3.assess(binding, loop, 'R1', events, directory, 10, call_fn=call)
                self.assertEqual(result['verdict'], 'MET')
                self.assertEqual(call.call_count, 2)
                self.assertIn('CONTROLLER VALIDATION FEEDBACK', call.call_args.args[1])
                schema = call.call_args.args[2]
                refs = schema['properties']['criteria']['items']['properties']['evidence_refs']['items']['enum']
                self.assertEqual(refs, ['e000002', 'e000003', 'e000004'])
                self.assertEqual(schema['properties']['requirement_id']['enum'], ['R1'])
                self.assertEqual(schema['properties']['criteria']['items']['properties']['criterion_id']['enum'], ['R1.contract'])
                self.assertEqual(schema['properties']['feedback'], {'type': 'string'})
                self.assertEqual(schema['properties']['reason'], {'type': 'string'})
                self.assertEqual(schema['properties']['criteria']['items']['properties']['reason'], {'type': 'string'})
                audit = json.loads((Path(directory) / 'review-evidence.jsonl').read_text())
                self.assertEqual(audit['evidence']['e000002']['content'], events[2]['data']['message'])
                self.assertNotIn('quote', schema['properties']['criteria']['items']['properties'])
                loop.record('R1', result, tb3.visible_ledger(events))
                self.assertEqual(loop.due(10, 'first_plan')['id'], 'R2')

    def test_unavailable_preserves_last_met_and_blocks_acceptance_until_valid(self):
        binding, loop, events = self.fixture()
        loop.record('R1', self.review(), tb3.visible_ledger(events))
        previous = deepcopy(loop.states['R1']['review'])
        with tempfile.TemporaryDirectory() as directory:
            call = Mock(return_value=self.review('invented'))
            with self.assertRaises(tb3.AssessmentUnavailable) as caught:
                tb3.assess(binding, loop, 'R1', events, directory, 10, call_fn=call)
            loop.record_unavailable('R1', caught.exception, tb3.visible_ledger(events))
            self.assertEqual(call.call_count, 2)
            self.assertEqual(loop.states['R1']['review'], previous)
            self.assertEqual(loop.states['R1']['state'], 'met')
            self.assertFalse(loop.complete())
            self.assertIsNone(loop.due(10, 'first_plan'))
            self.assertEqual(loop.review_target(), 'R1')
            loop.record('R1', self.review(), tb3.visible_ledger(events))
            self.assertFalse(loop.states['R1']['assessment_unavailable'])
            self.assertEqual(loop.due(10, 'first_plan')['id'], 'R2')
            unmet = self.review()
            unmet['verdict'] = unmet['criteria'][0]['status'] = 'UNMET'
            loop.record('R1', unmet, tb3.visible_ledger(events))
            self.assertEqual(loop.states['R1']['state'], 'pending')

    def test_call_and_schema_errors_are_bounded_without_fake_verdict(self):
        from swe_persona.api_recovery import APITransportExhausted
        for error, attempts in ((APITransportExhausted(3, 'TimeoutError'), 1),
                                (ValueError('bad response'), 2), (None, 2)):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                binding, loop, events = self.fixture()
                call = Mock(side_effect=error) if error else Mock(return_value={'verdict': 'MET'})
                with self.assertRaises(tb3.AssessmentUnavailable):
                    tb3.assess(binding, loop, 'R1', events, directory, 10, call_fn=call)
                self.assertEqual(call.call_count, attempts)
                self.assertIsNone(loop.states['R1']['review'])

    def test_internal_repair_does_not_extend_deadline(self):
        binding, loop, events = self.fixture()
        with tempfile.TemporaryDirectory() as directory, patch.object(tb3.time, 'monotonic', side_effect=[0, 0, 4, 4]):
            call = Mock(side_effect=[ValueError('bad response'), self.review()])
            tb3.assess(binding, loop, 'R1', events, directory, 10, call_fn=call)
            self.assertEqual([c.kwargs['timeout'] for c in call.call_args_list], [10, 6])

    def test_cancellation_and_invalid_host_state_are_not_hidden(self):
        binding, loop, events = self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            def cancelled(*args, **kwargs):
                raise asyncio.CancelledError()
            with self.assertRaises(asyncio.CancelledError):
                tb3.assess(binding, loop, 'R1', events, directory, 1, call_fn=cancelled)
            with self.assertRaises(KeyError):
                tb3.assess(binding, loop, 'unknown-requirement', events, directory, 1,
                           call_fn=lambda *a, **k: self.review())
            self.assertFalse((Path(directory) / 'review-recovery.jsonl').exists())


class TogetherOnlineRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        case = json.loads((TOGETHER / 'persona_runtime/calibration-cases.json').read_text())[0]
        binding = json.loads((TOGETHER / case['binding_file']).read_text())
        return binding, case

    async def test_unknown_feedback_and_later_met_do_not_poison_validity(self):
        for mode in ('bad_reference', 'call_error', 'outer_error'):
            with self.subTest(mode=mode):
                binding, case = self.fixture()
                failed = True
                simulator_calls = []
                async def call(role, system, payload):
                    if role == 'requirement_rubric_judge':
                        if failed and mode == 'call_error':
                            raise ValueError('Malformed judge output')
                        return {'packet_id': payload['packet_id'], 'status': 'met',
                                'evidence_refs': ['invented'] if failed else
                                [payload['evidence']['artifacts'][0]['ref']], 'reason': 'Visible evidence.'}
                    simulator_calls.append(role)
                    return {'action': 'no-op', 'kind': 'wait', 'content': '', 'requirement_id': None}
                sim = swt.UserSimulator(binding, call)
                requirements = binding['persona']['bound_evolved_requirements']
                sim.disclosed = {r['id']: case['activation_turn'] for r in requirements}
                sim.native_disclosed = {r['id']: case['activation_turn']
                                       for r in binding.get('native_phase_contract', {}).get('required_followups', [])}
                observation = {'turn': case['activation_turn'] + 1, 'events': [],
                               'evidence': case['evidence'], 'agent_message': 'Please review the work.',
                               'completion_attempt': True, 'interaction_checkpoint': True,
                               'remaining_agent_turns': 100}
                if mode == 'outer_error':
                    with patch.object(swt, 'judge_requirement', side_effect=RuntimeError('Assessor failed')):
                        decision = await sim.step(observation)
                else:
                    decision = await sim.step(observation)
                self.assertEqual(decision['action'], 'assessment_unavailable')
                self.assertEqual(decision['content'], '')
                self.assertEqual(simulator_calls, [])
                self.assertFalse(sim.completion_ready())
                self.assertFalse(sim.invalid)
                self.assertTrue(sim.judge_errors)
                self.assertNotIn('Malformed judge output', decision['content'])
                failed = False
                for req in requirements:
                    sim.verdicts[req['id']] = await sim._review_requirement(req, case['evidence'], 'interaction')
                self.assertTrue(sim.completion_ready())
                sim.user_accepted = True
                async def audit(*args):
                    return {'status': 'pass', 'affects_joint_success': False}
                with patch.object(swt, 'audit_interaction', audit):
                    summary = await sim.finish(case['evidence'])
                self.assertTrue(summary['evaluation_valid'])
                self.assertEqual(summary['persona_status'], 'met')
                self.assertEqual(summary['invalid_reasons'], [])
                self.assertTrue(summary['judge_errors'])

    async def test_internal_retry_can_recover_and_later_errors_preserve_accepted_verdict(self):
        binding, case = self.fixture()
        req = binding['persona']['bound_evolved_requirements'][0]
        calls = []
        fail = False
        async def call(role, system, payload):
            calls.append((role, system))
            return {'packet_id': payload['packet_id'], 'status': 'met',
                    'evidence_refs': ['invented'] if fail or len(calls) == 1 else
                    [payload['evidence']['artifacts'][0]['ref']], 'reason': 'Visible evidence.'}
        sim = swt.UserSimulator(binding, call)
        sim.disclosed[req['id']] = case['activation_turn']
        accepted = await sim._review_requirement(req, case['evidence'], 'interaction')
        self.assertEqual(accepted['status'], 'met')
        self.assertEqual(len(calls), 2)
        self.assertIn('previous assessment was rejected', calls[1][1])
        fail = True
        unavailable = await sim._review_requirement(req, case['evidence'], 'interaction')
        self.assertEqual(len(calls), 4)
        self.assertEqual(unavailable['status'], 'unknown')
        self.assertEqual(unavailable['last_valid_verdict'], accepted)
        self.assertEqual(sim.last_valid_verdicts[req['id']], accepted)
        self.assertFalse(sim.invalid)

    async def test_modified_host_evidence_still_raises(self):
        binding, case = self.fixture()
        async def call(*args):
            raise AssertionError('No model call should occur')
        sim = swt.UserSimulator(binding, call)
        evidence = deepcopy(case['evidence'])
        evidence['revision'] = 'tampered'
        with self.assertRaisesRegex(ValueError, 'Modified evidence'):
            await sim.step({'turn': 1, 'evidence': evidence})


class TB3ControllerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(TB3))
        cls.controller = importlib.import_module('terminal_agent')

    async def fixture(self, directory):
        binding, loop, events = TB3OnlineRecoveryTests().fixture()
        agent = self.controller.PersonaTerminalAgent.__new__(self.controller.PersonaTerminalAgent)
        agent.binding = binding
        agent.requirement_loop = loop
        agent.events = events
        agent.private_dir = Path(directory)
        agent.user_turns = agent.simulator_steps = 3
        agent.user = Mock()
        agent.pause = AsyncMock()
        agent.resume_containers = AsyncMock()
        agent.expired = False
        agent.remaining = lambda: 30
        async def within(operation):
            return await operation
        agent.within_budget = within
        agent.user_wait_seconds = 0
        agent.cutoff_at = None
        agent.actions_since_user = 8
        agent.visible_activity = ['shell']
        agent.event = lambda kind, data: agent.events.append({'kind': kind, 'data': data})
        agent.terminal = 'running'
        return agent

    async def test_repeated_internal_errors_never_add_agent_feedback_or_user_turns(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = await self.fixture(directory)
            messages = [{'role': 'assistant', 'content': 'Current validation report.'}]
            # Use the same typed exception imported by the actual controller.
            with patch.object(self.controller, 'assess', side_effect=self.controller.AssessmentUnavailable('assessor unavailable')) as call:
                for i in range(3):
                    agent.events.append({'kind': 'agent_action', 'data': {'message': f'Work checkpoint {i}'}})
                    self.assertTrue(await agent.user_response(messages, 'work_checkpoint'))
                self.assertFalse(await agent.user_response(messages, 'completion_attempt'))
                self.assertEqual(call.call_count, 4)
            agent.user.step.assert_not_called()
            self.assertEqual(agent.user_turns, 3)
            self.assertEqual(agent.simulator_steps, 3)
            self.assertEqual(len(messages), 1)
            self.assertEqual(agent.resume_containers.await_count, 3)
            self.assertEqual(agent.terminal, 'online_judge_unavailable')

    async def test_no_fresh_evidence_does_not_recall_judge_or_simulator(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = await self.fixture(directory)
            with patch.object(self.controller, 'assess', side_effect=self.controller.AssessmentUnavailable('unavailable')) as call:
                self.assertTrue(await agent.user_response([], 'work_checkpoint'))
                self.assertTrue(await agent.user_response([], 'work_checkpoint'))
                self.assertEqual(call.call_count, 1)
            agent.user.step.assert_not_called()

    async def test_valid_unobservable_is_a_genuine_review(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = await self.fixture(directory)
            review = TB3OnlineRecoveryTests().review()
            review['verdict'] = review['criteria'][0]['status'] = 'UNOBSERVABLE'
            review['criteria'][0]['evidence_refs'] = []
            with patch.object(self.controller, 'assess', return_value=review):
                self.assertTrue(await agent.review_requirements('completion_attempt'))
            state = agent.requirement_loop.states['R1']
            self.assertFalse(state['assessment_unavailable'])
            self.assertEqual(state['review']['verdict'], 'UNOBSERVABLE')


class IndependentFinalGradingTests(unittest.TestCase):
    def test_online_error_requires_every_final_grade_and_never_uses_cached_met(self):
        spec = importlib.util.spec_from_file_location('fixture_screening_new', TB3 / 'screening.py')
        screening = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(screening)
        run = {'terminal': 'online_judge_unavailable', 'artifact_integrity': True,
               'original_reward': 1, 'required_requirement_ids': ['R1', 'R2'],
               'persona_check': {'checks': {'R1': {'status': 'pass'}, 'R2': {'status': 'pass'}}},
               'episode': {'disclosed_requirement_ids': ['R1', 'R2'], 'clock': 'agent_user_wall',
                           'time_limit_seconds': 10, 'interaction_seconds': 5,
                           'requirement_loop': {'perceived_complete': True},
                           'events': [{'kind': 'shell_result', 'data': {'return_code': 0}}]}}
        good = screening.benchmark_outcome(run)
        self.assertEqual(good['status'], 'pass')
        self.assertTrue(good['online_judge_unavailable'])
        self.assertFalse(good['api_interrupted'])
        del run['persona_check']['checks']['R2']
        unknown = screening.benchmark_outcome(run)
        self.assertEqual(unknown['status'], 'fail')
        self.assertIsNone(unknown['persona_pass'])
        self.assertFalse(unknown['verification_complete'])
        run['artifact_integrity'] = False
        self.assertEqual(screening.benchmark_outcome(run)['status'], 'invalid')
        run['episode']['interaction_seconds'] = 10
        self.assertEqual(screening.benchmark_outcome(run)['failure_reasons'], ['wall_timeout'])

    def test_normalization_keeps_online_error_separate_from_transport(self):
        from swe_persona.cli import normalize_result
        from swe_persona.api_recovery import assess_interrupted
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            assessment = assess_interrupted(True, None, artifacts_available=True, progress=True)
            assessment.update(api_interrupted=False, online_judge_unavailable=True,
                              outcome_reason='online_judge_unavailable')
            (out / 'persona-result.json').write_text(json.dumps({'benchmark_outcome': 'fail',
                'api_recovery_assessment': assessment, 'original_reward': 1}))
            row = {'task_id': 'fixture', 'source_benchmark': 'fixture', 'source_subset': 'fixture',
                   'content_sha256': 'fixture', 'interaction': {'profile_id': 'terminal-bench-3'}}
            result = normalize_result(row, out, 0)
            self.assertEqual(result['outcome_reason'], 'online_judge_unavailable')
            self.assertIn('online_judge_assessment', result)
            self.assertNotIn('api_recovery_assessment', result)

if __name__ == '__main__':
    unittest.main()
