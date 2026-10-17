"""Fault injection for transport recovery and usable-result handling across profiles."""
import importlib.util
import json
import os
import ssl
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error

from swe_persona.api import RoleAPI
from swe_persona.api_recovery import (
    APITransportExhausted, assess_interrupted, is_api_interruption, retry_transport,
)
from swe_persona.cli import normalize_result

ROOT = Path(__file__).parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecoveryTests(unittest.TestCase):
    def test_ssl_disconnect_recovers_directly_and_through_wrappers(self):
        for kind in (ssl.SSLEOFError, ssl.SSLZeroReturnError):
            for wrapper in ('direct', 'urllib', 'cause'):
                with self.subTest(kind=kind.__name__, wrapper=wrapper):
                    error = kind(8, 'TLS connection closed')
                    if wrapper == 'urllib':
                        error = urllib.error.URLError(error)
                    elif wrapper == 'cause':
                        outer = RuntimeError('connection wrapper')
                        outer.__cause__ = error
                        error = outer
                    operation = Mock(side_effect=[error, 'response'])
                    records = []
                    with patch('swe_persona.api_recovery.time.sleep'):
                        self.assertEqual(retry_transport(operation, request_timeout=10,
                                                        audit=records.append), 'response')
                    self.assertEqual(operation.call_count, 2)
                    self.assertEqual([r['status'] for r in records], ['retrying', 'recovered'])

    def test_certificate_and_unknown_ssl_errors_fail_without_retry(self):
        for kind in (ssl.SSLCertVerificationError, ssl.SSLError):
            for wrapped in (False, True):
                with self.subTest(kind=kind.__name__, wrapped=wrapped):
                    error = kind(1, 'TLS configuration or certificate failure')
                    if wrapped:
                        error = urllib.error.URLError(error)
                    operation = Mock(side_effect=error)
                    with patch('swe_persona.api_recovery.time.sleep') as sleep:
                        with self.assertRaises(type(error)):
                            retry_transport(operation, request_timeout=10)
                    self.assertEqual(operation.call_count, 1)
                    sleep.assert_not_called()

    def test_ssl_disconnect_exhaustion_is_bounded(self):
        operation = Mock(side_effect=ssl.SSLEOFError(8, 'TLS connection closed'))
        with patch('swe_persona.api_recovery.time.sleep'), self.assertRaises(APITransportExhausted) as caught:
            retry_transport(operation, request_timeout=10)
        self.assertEqual(operation.call_count, 3)
        self.assertTrue(is_api_interruption(caught.exception))

    def test_generic_ssl_unexpected_eof_reason_is_retryable(self):
        error = ssl.SSLError(1, 'TLS error')
        error.reason = 'UNEXPECTED_EOF_WHILE_READING'
        operation = Mock(side_effect=[error, 'response'])
        with patch('swe_persona.api_recovery.time.sleep'):
            self.assertEqual(retry_transport(operation, request_timeout=10), 'response')

    def test_ssl_recovery_respects_episode_deadline(self):
        clock = SimpleNamespace(now=0.0)
        timeouts = []
        def operation(seconds):
            timeouts.append(seconds)
            clock.now += seconds
            raise ssl.SSLEOFError(8, 'TLS connection closed')
        def sleep(seconds):
            clock.now += seconds
        with patch('swe_persona.api_recovery.time.monotonic', side_effect=lambda: clock.now), \
             patch('swe_persona.api_recovery.time.sleep', side_effect=sleep), \
             self.assertRaises(APITransportExhausted):
            retry_transport(operation, request_timeout=3, deadline=5)
        self.assertEqual(timeouts, [3, 1])
        self.assertEqual(clock.now, 5)

    def test_transient_timeout_then_success(self):
        operation = Mock(side_effect=[TimeoutError(), 'response'])
        records = []
        with patch('swe_persona.api_recovery.time.sleep') as sleep:
            self.assertEqual(retry_transport(operation, request_timeout=10, audit=records.append), 'response')
        self.assertEqual(operation.call_count, 2)
        sleep.assert_called_once()
        self.assertEqual([r['status'] for r in records], ['retrying', 'recovered'])

    def test_timeout_exhaustion_is_typed_and_does_not_retry_tools(self):
        for error in (TimeoutError(), urllib.error.URLError(TimeoutError())):
            with self.subTest(error=type(error).__name__):
                operation = Mock(side_effect=error)
                with patch('swe_persona.api_recovery.time.sleep'), self.assertRaises(APITransportExhausted) as caught:
                    retry_transport(operation, request_timeout=10)
                self.assertEqual(operation.call_count, 3)
                self.assertTrue(is_api_interruption(caught.exception))
        self.assertFalse(is_api_interruption(TimeoutError('container timeout')))

    def test_authentication_schema_and_cancellation_are_not_retried(self):
        for error in (urllib.error.HTTPError('https://fixture', 401, 'unauthorized', {}, None),
                      ValueError('malformed schema'), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                operation = Mock(side_effect=error)
                with self.assertRaises(type(error)):
                    retry_transport(operation, request_timeout=10)
                self.assertEqual(operation.call_count, 1)

    def test_explicit_episode_deadline_bounds_requests_and_backoff(self):
        clock = SimpleNamespace(now=0.0)
        timeouts = []
        def operation(seconds):
            timeouts.append(seconds)
            clock.now += seconds
            raise TimeoutError()
        def sleep(seconds):
            clock.now += seconds
        with patch('swe_persona.api_recovery.time.monotonic', side_effect=lambda: clock.now), \
             patch('swe_persona.api_recovery.time.sleep', side_effect=sleep), \
             self.assertRaises(APITransportExhausted):
            retry_transport(operation, request_timeout=3, deadline=5)
        self.assertEqual(timeouts, [3, 1])
        self.assertEqual(clock.now, 5)

    def test_unrelated_exception_context_is_not_an_api_interruption(self):
        try:
            raise APITransportExhausted(3, 'TimeoutError')
        except APITransportExhausted:
            try:
                raise ValueError('Independent integrity violation')
            except ValueError as error:
                self.assertFalse(is_api_interruption(error))

    def test_role_api_json_uses_shared_recovery_and_audits_failed_attempts(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'FIXTURE_KEY': 'dummy'}):
            response = SimpleNamespace(model='fixture', usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2,
                                       completion_tokens_details=SimpleNamespace(reasoning_tokens=1)),
                                       choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='{"value":1}', reasoning_content='fixture'))])
            create = Mock(side_effect=[TimeoutError(), TimeoutError(), response])
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            client.with_options = lambda **options: client
            config = {'agent': {'api_key_env': 'FIXTURE_KEY', 'base_url': 'https://fixture.invalid/v1',
                               'model': 'fixture', 'api': 'chat_completions', 'timeout_sec': 2,
                               'structured_output_mode': 'prompt', 'request_options': {'max_tokens': 65536},
                               'max_tokens_field': 'max_tokens'}}
            with patch('swe_persona.api.OpenAI', return_value=client), patch('swe_persona.api_recovery.time.sleep'):
                api = RoleAPI(config, 'agent', Path(temp))
                value = api.json([{'role': 'user', 'content': 'fixture'}],
                                 {'type': 'object', 'properties': {'value': {'type': 'integer'}}, 'required': ['value']})
            self.assertEqual(value, {'value': 1})
            self.assertEqual(create.call_count, 3)
            self.assertEqual(create.call_args.kwargs['max_tokens'], 65536)
            call_record=json.loads((Path(temp) / 'api-calls.jsonl').read_text())
            self.assertEqual(call_record['requested_max_tokens'], 65536)
            self.assertEqual(call_record['finish_reason'], 'stop')
            self.assertEqual(call_record['reasoning_tokens'], 1)
            self.assertEqual(call_record['reasoning_content_chars'], 7)
            records = [json.loads(line) for line in (Path(temp) / 'transport-recovery.jsonl').read_text().splitlines()]
            self.assertEqual([r['status'] for r in records], ['retrying', 'retrying', 'recovered'])
            self.assertNotIn('dummy', (Path(temp) / 'transport-recovery.jsonl').read_text())


class InterruptedResultsTests(unittest.TestCase):
    def test_task_deadline_precedes_recovered_pass_and_no_progress(self):
        for progress in (False, True):
            result = assess_interrupted(True, True, artifacts_available=True, progress=progress, budget_exhausted=True)
            self.assertEqual(result['status'], 'fail')

    def test_supervisor_timeout_cannot_be_overridden_by_late_recovery_grades(self):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)
            payload={'benchmark_outcome':'fail','supervisor_status':'timeout','original_reward':None,
                     'api_recovery_assessment':assess_interrupted(True,True,artifacts_available=True,progress=True)}
            (out/'persona-result.json').write_text(json.dumps(payload))
            row={'task_id':'fixture','source_benchmark':'fixture','source_subset':'fixture','content_sha256':'fixture',
                 'interaction':{'profile_id':'swe-bench-live'}}
            result=normalize_result(row,out,124)
            self.assertEqual(result['status'],'fail')
            self.assertIsNone(result['native_pass'])
            self.assertNotIn('api_recovery_assessment',result)

    def test_grades_and_artifact_integrity(self):
        cases = [(True, True, True, True, True, 'pass'),
                 (False, True, True, True, True, 'fail'),
                 (True, None, True, True, True, 'fail'),
                 (None, None, True, True, True, 'fail'),
                 (False, False, True, False, True, 'infrastructure_error'),
                 (True, True, False, True, True, 'infrastructure_error'),
                 (True, True, True, True, False, 'invalid')]
        for native, persona, artifacts, progress, integrity, status in cases:
            with self.subTest(native=native, persona=persona, artifacts=artifacts, progress=progress):
                result = assess_interrupted(native, persona, artifacts_available=artifacts,
                                            progress=progress, integrity=integrity)
                self.assertEqual(result['status'], status)
                self.assertIs(result['native_pass'], native)
                self.assertIs(result['persona_pass'], persona)

    def test_normalization_retains_recovery_and_unknown_grades_for_all_six_profiles(self):
        for profile in ['terminal-bench-3', 'swe-bench-pro-mini', 'swe-bench-pro-shell',
                        'swe-bench-live', 'swe-together', 'deep-swe']:
            for native, persona, progress, status in [(True, True, True, 'pass'),
                                                     (True, None, True, 'fail'),
                                                     (None, None, False, 'infrastructure_error')]:
                with self.subTest(profile=profile, status=status), tempfile.TemporaryDirectory() as temp:
                    out = Path(temp)
                    assessment = assess_interrupted(native, persona, artifacts_available=True, progress=progress)
                    payload = {'benchmark_outcome': 'invalid', 'episode_valid': False,
                               'original_reward': None, 'api_recovery_assessment': assessment}
                    if profile == 'swe-bench-pro-shell':
                        path = out / 'trial/episode.json'; path.parent.mkdir()
                    else:
                        path = out / 'persona-result.json'
                    path.write_text(json.dumps(payload))
                    row = {'task_id': 'fixture', 'source_benchmark': 'fixture', 'source_subset': 'fixture',
                           'content_sha256': 'fixture', 'interaction': {'profile_id': profile}}
                    result = normalize_result(row, out, 2)
                    self.assertEqual(result['status'], status)
                    self.assertIs(result['native_pass'], native)
                    self.assertIs(result['persona_pass'], persona)
                    self.assertEqual(result['joint_success'], status == 'pass')
                    self.assertEqual(result['verification_complete'], isinstance(native, bool) and isinstance(persona, bool))

    def test_tb3_scores_preserved_work_and_keeps_deadline_failure(self):
        module = load('fixture_screening', ROOT / 'src/swe_persona/vendor/tb3/runtime/screening.py')
        run = {'terminal': 'api_interrupted', 'artifact_integrity': True, 'original_reward': 1,
               'required_requirement_ids': ['R1'], 'persona_check': {'checks': {'R1': {'status': 'pass'}}},
               'episode': {'disclosed_requirement_ids': ['R1'], 'clock': 'agent_user_wall',
                           'time_limit_seconds': 10, 'interaction_seconds': 5,
                           'events': [{'kind': 'shell_result', 'data': {'return_code': 0}}]}}
        self.assertEqual(module.benchmark_outcome(run)['status'], 'pass')
        run['original_reward'] = 0
        self.assertEqual(module.benchmark_outcome(run)['status'], 'fail')
        run['episode']['interaction_seconds'] = 10
        self.assertEqual(module.benchmark_outcome(run)['failure_reasons'], ['wall_timeout'])
        run['terminal'] = 'provider_or_protocol_error'
        run['episode']['interaction_seconds'] = 5
        self.assertEqual(module.benchmark_outcome(run)['status'], 'invalid')

    def test_mini_writes_final_verified_result_after_interruption(self):
        runtime = ROOT / 'src/swe_persona/vendor/swe_pro/mini/runtime'
        sys.path.insert(0, str(runtime))
        self.addCleanup(lambda: sys.path.remove(str(runtime)))
        from pcb_v6.episode import Episode
        with tempfile.TemporaryDirectory() as temp:
            episode = Episode.__new__(Episode)
            episode.out = Path(temp)
            episode.task = SimpleNamespace(task_id='fixture')
            episode.condition = 'persona'
            episode.frozen = {'ok': True}
            episode.models = {}; episode.errors = []; episode.decisions = []; episode.judgments = []
            episode.timing = SimpleNamespace(seconds={})
            result = episode._write_results(
                {'end_state': 'api_interrupted', 'controller': None,
                 'transcript': [{'role': 'agent', 'text': 'A substantive review.'}]},
                {'status': 'pass', 'reward': 1}, {'requirements': {'R1': {'status': 'pass'}}}, {}, {})
            self.assertEqual(result['benchmark_outcome'], 'pass')
            self.assertEqual(json.loads((episode.out / 'persona-result.json').read_text())['benchmark_outcome'], 'pass')

    def test_deepswe_export_preserves_verified_recovery_result(self):
        module = load('fixture_ds_export', ROOT / 'src/swe_persona/vendor/deepswe/project/dspilot/export_tb3_results.py')
        result = {'error': 'API interruption', 'api_recovery_assessment': assess_interrupted(
            True, False, artifacts_available=True, progress=True)}
        self.assertEqual(module.benchmark_assessment(result)['status'], 'fail')


if __name__ == '__main__':
    unittest.main()
