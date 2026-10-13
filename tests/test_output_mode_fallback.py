"""Capability downgrade must be bounded and preserve validation and budgets."""
from copy import deepcopy
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
import urllib.error

import httpx
from openai import APIStatusError
from swe_persona.api import RoleAPI
from swe_persona.api_recovery import APITransportExhausted
from swe_persona import output_mode

ROOT = Path(__file__).parents[1]
PACKAGE = 'fixture_deepswe_format_fallback'
package = ModuleType(PACKAGE)
package.__path__ = [str(ROOT/'src/swe_persona/vendor/deepswe/project/dspilot')]
sys.modules[PACKAGE] = package
ds = importlib.import_module(PACKAGE+'.api')
SCHEMA = {'type':'object','properties':{'ok':{'type':'boolean'}},
          'required':['ok'],'additionalProperties':False}


class OutputFallbackTests(unittest.TestCase):
    def setUp(self):
        output_mode._cache.clear()

    def tearDown(self):
        output_mode._cache.clear()

    def sdk_error(self, message, status=400):
        response = httpx.Response(status,request=httpx.Request('POST','https://fixture.invalid'))
        return APIStatusError(message,response=response,body={'message':message})

    def sdk_response(self, transport, text='{"ok":true}'):
        if transport=='responses':
            return NS(output_text=text,model='fixture',status='completed',usage=None,incomplete_details=None)
        return NS(model='fixture',usage=None,choices=[NS(message=NS(content=text),finish_reason='stop')])

    def api(self, transport, outcomes, directory=None, mode='json_schema', fallback=True):
        create = Mock(side_effect=outcomes)
        client = NS(responses=NS(create=create),chat=NS(completions=NS(create=create)))
        client.with_options = Mock(return_value=client)
        config = {'agent':{'api_key_env':'FORMAT_FIXTURE_KEY','base_url':'https://fixture.invalid/v1',
                  'model':'fixture','api':transport,'timeout_sec':30,'structured_output_mode':mode,
                  'structured_output_fallback':fallback,'request_options':{'max_output_tokens':500}
                       if transport=='responses' else {'max_completion_tokens':500}}}
        with patch.dict(os.environ,{'FORMAT_FIXTURE_KEY':'dummy'}), \
                patch('swe_persona.api.OpenAI',return_value=client):
            api = RoleAPI(config,'agent',directory)
        return api,create,client,config

    def modes(self, calls, transport):
        return [(x.get('text',{}).get('format',{}).get('type','prompt') if transport=='responses'
                 else x.get('response_format',{}).get('type','prompt')) for x in calls]

    def test_both_sdk_transports_downgrade_in_order_with_audit_and_no_prompt_stacking(self):
        for transport in ('responses','chat_completions'):
            with self.subTest(transport=transport),tempfile.TemporaryDirectory() as temp:
                output_mode._cache.clear()
                outcomes=[self.sdk_error('json_schema is not supported by this model'),
                          self.sdk_error('response_format json_object is not supported'),
                          self.sdk_response(transport),self.sdk_response(transport)]
                api,create,_,config=self.api(transport,outcomes,Path(temp))
                messages=[{'role':'system','content':'Do the task.'},{'role':'user','content':'Ready?'}]
                original=deepcopy(messages);settings=deepcopy(config)
                self.assertEqual(api.json(messages,SCHEMA),{'ok':True})
                self.assertEqual(api.json(messages,SCHEMA),{'ok':True})
                calls=[x.kwargs for x in create.call_args_list]
                self.assertEqual(self.modes(calls,transport),['json_schema','json_object','prompt','prompt'])
                field='max_output_tokens' if transport=='responses' else 'max_completion_tokens'
                self.assertEqual([x[field] for x in calls],[500]*4)
                message_field='input' if transport=='responses' else 'messages'
                for x in calls[1:]:
                    self.assertEqual(sum(m.get('content','').count('Output transport:') for m in x[message_field]),1)
                self.assertEqual(messages,original);self.assertEqual(config,settings)
                records=[json.loads(x) for x in (Path(temp)/'transport-recovery.jsonl').read_text().splitlines()]
                self.assertEqual([(x['from'],x['to']) for x in records if x.get('policy')==output_mode.POLICY],
                                 [('json_schema','json_object'),('json_object','prompt')])
                self.assertTrue(all(x['action_executed'] is False for x in records))
                audit=json.loads((Path(temp)/'api-calls.jsonl').read_text().splitlines()[-1])
                self.assertEqual(audit['structured_output_mode'],'prompt')

    def test_provider_capability_cache_survives_routed_client_recreation_and_is_scoped(self):
        api,_,_,_=self.api('responses',[self.sdk_error('json_schema is unsupported'),self.sdk_response('responses')])
        api.json([],SCHEMA)
        other,create,_,_=self.api('responses',[self.sdk_response('responses')])
        other.json([],SCHEMA)
        self.assertEqual(self.modes([create.call_args.kwargs],'responses'),['json_object'])
        self.assertEqual(output_mode.OutputModeFallback('https://different.invalid','fixture','responses','json_schema').mode,'json_schema')
        self.assertEqual(output_mode.OutputModeFallback('https://fixture.invalid/v1','other','responses','json_schema').mode,'json_schema')

    def test_lower_initial_mode_and_explicit_opt_out_are_respected(self):
        api,create,_,_=self.api('responses',[self.sdk_error('json_object unsupported'),self.sdk_response('responses')],mode='json_object')
        api.json([],SCHEMA)
        self.assertEqual(self.modes([x.kwargs for x in create.call_args_list],'responses'),['json_object','prompt'])
        api,create,_,_=self.api('responses',[self.sdk_error('json_schema unsupported')],fallback=False)
        with self.assertRaises(APIStatusError):api.json([],SCHEMA)
        self.assertEqual(create.call_count,1)
        self.assertEqual(self.modes([create.call_args.kwargs],'responses'),['json_schema'])

    def test_auth_invalid_schema_and_generic_errors_do_not_downgrade(self):
        for status,message in [(401,'json_schema unsupported'),(403,'response_format not allowed'),
                (400,'Invalid schema for response_format json_schema; unsupported type'),
                (400,'Unsupported schema keyword in response_format: additionalProperties'),
                (400,'Unsupported schema keyword in response_format: enum'),
                (400,'response_format json_schema invalid'),(404,'Model not found')]:
            with self.subTest(status=status,message=message):
                api,create,_,_=self.api('responses',[self.sdk_error(message,status)])
                with self.assertRaises(APIStatusError):api.json([],SCHEMA)
                self.assertEqual(create.call_count,1)
                self.assertEqual(api._format.mode,'json_schema')
        for status in (408,429,500,503):
            self.assertFalse(output_mode.unsupported_format(status,'response_format unsupported','json_schema'))
        self.assertTrue(output_mode.unsupported_format(400,"Unknown parameter: 'text'",'json_schema'))

    def test_prompt_failure_stops_after_at_most_two_downgrades(self):
        api,create,_,_=self.api('responses',[self.sdk_error('json_schema unsupported'),
            self.sdk_error('json_object unsupported'),self.sdk_error('response_format unsupported')])
        with self.assertRaises(APIStatusError):api.json([],SCHEMA)
        self.assertEqual(create.call_count,3)

    def test_format_repair_keeps_the_effective_mode_and_original_schema(self):
        api,create,_,_=self.api('responses',[self.sdk_error('json_schema unsupported'),
            self.sdk_response('responses','{"wrong":true}'),self.sdk_response('responses')])
        self.assertEqual(api.json([],SCHEMA),{'ok':True})
        self.assertEqual(self.modes([x.kwargs for x in create.call_args_list],'responses'),
                         ['json_schema','json_object','json_object'])
        self.assertIn('schema mismatch',create.call_args.kwargs['input'][-1]['content'])
        self.assertIn(json.dumps(SCHEMA),create.call_args.kwargs['input'][0]['content'])

    def test_fallback_does_not_restart_the_callers_deadline(self):
        clock=NS(now=0)
        api,create,client,_=self.api('responses',[])
        def reject(**kwargs):
            clock.now+=4
            raise self.sdk_error('response_format is unsupported')
        create.side_effect=reject
        with patch('time.monotonic',side_effect=lambda:clock.now):
            with self.assertRaises(APITransportExhausted):api.json([],SCHEMA,timeout=6)
        self.assertEqual(create.call_count,2)
        self.assertEqual([x.kwargs['timeout'] for x in client.with_options.call_args_list],[6,2])

    def test_configuration_rejects_invalid_fallback_flag(self):
        with self.assertRaises(ValueError):output_mode.configured_mode({'structured_output_fallback':'true'})

    def deep_client(self,transport,directory,mode='json_schema',fallback=True,max_calls=10):
        config={'MODEL':'deep-fixture','API_BASE_URL':'https://deep-fixture.invalid/v1','API_KEY':'dummy',
            'API_PROVIDER':'openai','API_MODE':transport,'STRUCTURED_OUTPUT_MODE':mode,
            'STRUCTURED_OUTPUT_FALLBACK':str(fallback).lower(),'HTTP_TIMEOUT_SECONDS':'30',
            'REQUEST_OPTIONS_JSON':json.dumps({'max_output_tokens':500})}
        with patch.object(ds,'config',return_value=config):
            return ds.Client(Path(directory)/'api.jsonl',max_calls=max_calls)

    def deep_error(self,mode,status=400):
        return urllib.error.HTTPError('https://deep-fixture.invalid',status,'bad request',{},
            io.BytesIO(json.dumps({'error':{'message':mode+' is unsupported'}}).encode()))

    def deep_response(self,transport):
        data=({'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}],
               'usage':{'prompt_tokens':1,'completion_tokens':2}} if transport=='chat_completions' else
              {'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'{"ok":true}'}]}],
               'usage':{'input_tokens':1,'output_tokens':2}})
        return io.BytesIO(json.dumps(data).encode())

    def test_deepswe_both_transports_share_policy_and_keep_function_tools(self):
        for transport in ('responses','chat_completions'):
            with self.subTest(transport=transport),tempfile.TemporaryDirectory() as temp:
                output_mode._cache.clear();client=self.deep_client(transport,temp);calls=[]
                outcomes=[self.deep_error('json_schema'),self.deep_error('json_object'),
                          self.deep_response(transport),self.deep_response(transport)]
                def request(req,timeout):
                    calls.append(json.loads(req.data));result=outcomes.pop(0)
                    if isinstance(result,Exception):raise result
                    return result
                tools=[{'type':'function','name':'inspect','description':'Inspect.',
                        'parameters':{'type':'object','properties':{},'additionalProperties':False},'strict':True}]
                with patch.object(ds,'bounded_timeout',side_effect=lambda seconds:seconds), \
                        patch.object(ds.urllib.request,'urlopen',side_effect=request):
                    result=client.create([],schema=SCHEMA,tools=tools)
                    client.create([],schema=SCHEMA,tools=tools)
                self.assertEqual(result['status'],'completed')
                self.assertEqual(client.calls,4)
                self.assertEqual(self.modes(calls,transport),['json_schema','json_object','prompt','prompt'])
                self.assertTrue(all(x['tools']==calls[0]['tools'] for x in calls))
                records=[json.loads(x) for x in (Path(temp)/'api.jsonl').read_text().splitlines()]
                self.assertEqual(sum(x.get('policy')==output_mode.POLICY for x in records),2)

    def test_deepswe_downgrade_consumes_the_existing_call_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            client=self.deep_client('responses',temp,max_calls=1)
            with patch.object(ds,'bounded_timeout',side_effect=lambda seconds:seconds), \
                    patch.object(ds.urllib.request,'urlopen',side_effect=self.deep_error('json_schema')) as request:
                with self.assertRaisesRegex(ds.ApiFailure,'call budget exhausted'):
                    client.create([],schema=SCHEMA)
            self.assertEqual(request.call_count,1)

    def test_deepswe_fallback_shares_deadline_and_explicit_opt_out(self):
        with tempfile.TemporaryDirectory() as temp:
            client=self.deep_client('responses',temp);client.deadline=6
            clock=NS(now=0);timeouts=[]
            def request(req,timeout):
                timeouts.append(timeout);clock.now+=4
                raise self.deep_error('response_format')
            with patch.object(ds,'bounded_timeout',side_effect=lambda seconds:seconds), \
                    patch('time.monotonic',side_effect=lambda:clock.now), \
                    patch.object(ds.urllib.request,'urlopen',side_effect=request):
                with self.assertRaises(APITransportExhausted):client.create([],schema=SCHEMA)
            self.assertEqual(timeouts,[6,2])
            self.assertEqual(client.calls,2)
        with tempfile.TemporaryDirectory() as temp:
            client=self.deep_client('responses',temp,fallback=False)
            with patch.object(ds,'bounded_timeout',side_effect=lambda seconds:seconds), \
                    patch.object(ds.urllib.request,'urlopen',side_effect=self.deep_error('json_schema')) as request:
                with self.assertRaises(ds.ApiFailure):client.create([],schema=SCHEMA)
            self.assertEqual(request.call_count,1)


if __name__ == '__main__':
    unittest.main()
