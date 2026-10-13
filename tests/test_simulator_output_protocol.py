"""Offline regression coverage of each runtime's actual simulator prompts."""
import asyncio
import copy
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

from jsonschema import Draft202012Validator, ValidationError
from swe_persona.simulator_output import response_envelope, prepend_envelope

ROOT = Path(__file__).parents[1]
VENDOR = ROOT / 'src/swe_persona/vendor'


def package_module(name, directory, module):
    package = types.ModuleType(name)
    package.__path__ = [str(directory)]
    sys.modules[name] = package
    return importlib.import_module(name + '.' + module)


def file_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deep = package_module('fixture_deep_envelope', VENDOR/'deepswe/project/dspilot', 'simulator_staged')
mini = package_module('fixture_mini_envelope', VENDOR/'swe_pro/mini/runtime/pcb_v6', 'simulator')
together = package_module('fixture_together_envelope', VENDOR/'swe_together/swe_together_pilot/persona_runtime', 'simulator')
live = file_module('fixture_live_envelope', VENDOR/'swe_live/runtime/project/scripts/swe_live_multilang/tb3_adapter/protocol.py')
with patch.dict(sys.modules, {'llm': types.SimpleNamespace(Llm=object)}):
    shell = file_module('fixture_shell_envelope', VENDOR/'swe_pro/shell/portable/runtime-v3/tb2_persona_pilot/simulator.py')


def tb3_module():
    directory = VENDOR/'tb3/runtime'
    # Use the shipped Persona library and real schema builder. Isolate its
    # top-level imports and path additions from other runtime fixtures.
    with patch.dict(sys.modules, {
            'protocol':file_module('fixture_tb3_protocol',directory/'protocol.py'),
            'azure_client':file_module('fixture_tb3_azure',directory/'azure_client.py')}), \
            patch.object(sys, 'path', list(sys.path)):
        value = file_module('fixture_tb3_envelope',directory/'persona_adapter.py')
    value.call = Mock()
    return value


tb3 = tb3_module()


def examples(prompt):
    block = prompt.split('## Output example\n', 1)[1].split('\n\n## Role\n', 1)[0]
    return [json.loads(line) for line in block.splitlines() if line.startswith('{')]


class EnvelopeTests(unittest.TestCase):
    def test_missing_required_fields_and_invalid_current_values_fail_early(self):
        schema = mini.decision_schema(['answer'])
        fields = {key: key for key in schema['properties']}
        good = {'action':'answer','user_message':'','satisfied':False,'rationale':''}
        for bad in ({k:v for k,v in good.items() if k != 'satisfied'}, {**good,'action':'done'}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                response_envelope(schema, bad, fields)

    def test_json_escaping_empty_values_and_key_order(self):
        schema = mini.decision_schema(['answer'])
        value = {'action':'answer','user_message':'line\n"quoted" \\ end','satisfied':False,'rationale':''}
        prompt = response_envelope(schema, value, {key:key for key in schema['properties']})
        self.assertEqual(examples(prompt), [value])
        self.assertIn(r'a newline is `\n`, a quote is `\"`, and a backslash is `\\`', prompt)
        self.assertIn('Key order is unrestricted', prompt)
        Draft202012Validator(schema).validate(dict(reversed(list(value.items()))))
        self.assertEqual(prepend_envelope(prompt+'original persona',prompt), prompt+'original persona')

    def test_new_state_replaces_stale_envelope_without_losing_role_instructions(self):
        fields = {key:key for key in mini.decision_schema()['properties']}
        old = response_envelope(mini.decision_schema(['done']),
            {'action':'done','user_message':'','satisfied':True,'rationale':''}, fields)
        current = response_envelope(mini.decision_schema(['answer']),
            {'action':'answer','user_message':'','satisfied':False,'rationale':''}, fields)
        prompt = prepend_envelope(old+'Frozen role instructions.', current)
        self.assertEqual(prompt, current+'Frozen role instructions.')
        self.assertEqual(prompt.count('## Response envelope'), 1)
        self.assertEqual(examples(prompt)[0]['action'], 'answer')


class DeepEnvelopeTests(unittest.TestCase):
    def client(self, results):
        client = Mock()
        client.create.side_effect = results
        client.text.side_effect = json.dumps
        return client

    def context(self):
        return {'operation':'render','mode':'persona','stage':'S1','requirements':{},
            'checks':{'R1':{'status':'pending'}},
            'controller_receipt':{'outcome':'accept_plan','accepted':True,'stage':'S1'},
            'required_content':['Source edits and commits remain unauthorized.', 'Exact\npublic "document".'],
            'implementation_authorized':False,'remaining_budget':{'user_turns':4,'actions':10}}

    def draft(self, message='Please address the current request.'):
        return {'message':message,'closing':'','stage':'S1','controller_outcome':'accept_plan',
            'acknowledged_checks':{'R1':'pending'},'public_documents':{'block_0':'block_0','block_1':'block_1'}}

    def test_proposal_examples_use_actual_current_enums(self):
        for extra in ({'awaiting_initial_plan':True,'initial_plan_progression':{'proposal':'accept_plan'}},
                      {'awaiting_initial_plan':True}, {'stage':'S4'}, {}):
            with self.subTest(extra=extra):
                client = self.client([{'proposal':'continue','focus_ids':[]}])
                deep.propose(client, {'stage':'S1','requirements':{},**extra}, {}, [])
                args = client.create.call_args
                example = examples(args.args[0][0]['content'])[0]
                Draft202012Validator(args.kwargs['schema']).validate(example)
                self.assertEqual(example['proposal'], 'continue')
                self.assertEqual(example['focus_ids'], [])

    def test_read_only_example_never_authorizes_branch_edits_or_commit(self):
        context = self.context()
        original = copy.deepcopy(context)
        client = self.client([self.draft(), {'verdict':'pass','issues':[]}])
        deep.render(client, context, [])
        args = client.create.call_args_list[0]
        prompt = args.args[0][0]['content']
        example = examples(prompt)[0]
        Draft202012Validator(args.kwargs['schema']).validate(example)
        self.assertIn('supplied permissions', example['message'])
        self.assertNotIn('implement', example['message'])
        self.assertNotIn('commit', example['message'])
        self.assertEqual(example['acknowledged_checks'], {'R1':'pending'})
        self.assertIn('Source edits and commits are permitted only when current authority explicitly allows them', prompt)
        self.assertEqual(context, original)

    def test_document_recovery_example_matches_its_stricter_schema(self):
        client = self.client([self.draft(), {'verdict':'reject','issues':['Contradiction.']},
                              self.draft(), {'verdict':'unknown','issues':[]}, self.draft('')])
        audit = []
        result = deep.render(client, self.context(), audit)
        renders = [c for c in client.create.call_args_list if c.kwargs['role']=='simulator']
        self.assertEqual(len(renders), 3)
        for args in renders:
            Draft202012Validator(args.kwargs['schema']).validate(examples(args.args[0][0]['content'])[0])
        recovered = examples(renders[-1].args[0][0]['content'])[0]
        self.assertEqual((recovered['message'], recovered['closing']), ('',''))
        self.assertEqual(recovered['public_documents'], {'block_0':'block_0','block_1':'block_1'})
        self.assertEqual(result.strip(), '\n\n'.join(self.context()['required_content']))
        self.assertEqual(audit[-1]['extra_agent_turns'], 0)


class MiniShellLiveTests(unittest.TestCase):
    def test_mini_examples_follow_allowed_actions_and_preserve_persona_and_repairs(self):
        persona = {'id':'frozen-persona','attributes':{'constraints':'Review only.'}}
        for allowed in (['answer'], ['disclose_requirement'], ['request_fix'], ['request_evidence'], ['done']):
            with self.subTest(allowed=allowed):
                messages = mini.render_messages('Frozen role: Review only.', persona,
                    {'allowed_actions':allowed}, {'decision':{},'errors':['missing current fact']})
                prompt = messages[0]['content']
                example = examples(prompt)[0]
                Draft202012Validator(mini.decision_schema(allowed)).validate(example)
                self.assertIn(example['action'], allowed)
                self.assertEqual(example['satisfied'], allowed==['done'])
                self.assertIn('Frozen role: Review only.', prompt)
                self.assertIn('frozen-persona', prompt)
                self.assertIn('missing current fact', messages[1]['content'])

    def test_shell_examples_copy_current_ids_and_exact_due_requirement(self):
        for action in ('answer','disclose','wait','end','request_verification','feedback'):
            with self.subTest(action=action):
                llm = Mock()
                sim = shell.UserSimulator(llm, 'Frozen five-layer persona and permission policy.')
                obs = {'turn':17,'observation_id':'actual-observation','allowed_actions':[action]}
                if action=='disclose':
                    obs['requirement_to_disclose'] = {'requirement_ref':'R-current','operation':'revise',
                        'replaces':['R-old'],'must_include_verbatim':['Exact "requirement".','second\nline']}
                original = copy.deepcopy(obs)
                sim.decide(obs, ['keep current permission'])
                args = llm.json_obj.call_args
                prompt = args.args[0][0]['content']
                example = examples(prompt)[0]
                Draft202012Validator(args.args[1]).validate(example)
                self.assertEqual((example['turn_id'],example['observation_id'],example['action']),
                                 (17,'actual-observation',action))
                if action=='disclose':
                    due = obs['requirement_to_disclose']
                    self.assertEqual(example['disclosures'], [{k:due[k] for k in ('requirement_ref','operation','replaces')}])
                    for text in due['must_include_verbatim']: self.assertIn(text, example['user_message'])
                else: self.assertEqual(example['disclosures'], [])
                self.assertEqual(example['end_state'], 'completed_pending_final' if action=='end' else None)
                self.assertIn('Frozen five-layer persona', prompt)
                self.assertIn('keep current permission', args.args[0][1]['content'])
                self.assertEqual(obs, original)

    def test_live_shipped_and_generated_prompts_have_one_envelope(self):
        for original in ('Old shipped Parquet persona prompt.', live.SIM_PROMPT):
            prompt = live.simulator_system_prompt(original)
            self.assertTrue(prompt.startswith('## Response envelope'))
            self.assertEqual(prompt.count('## Response envelope'), 1)
            self.assertIn(original, prompt)
            self.assertEqual(live.simulator_system_prompt(prompt), prompt)
            example = examples(prompt)[0]
            Draft202012Validator(live.SIM_SCHEMA).validate(example)
            self.assertFalse(example['satisfied'])


class TB3EnvelopeTests(unittest.TestCase):
    def fixture(self, observation):
        sim = tb3.ReactiveUser.__new__(tb3.ReactiveUser)
        sim.binding = {}; sim.audit_dir = 'unused'; sim.timeout = 30; sim.closed_loop = False
        sim.simulator = types.SimpleNamespace(step=lambda text: sim.complete(messages=[
            {'role':'system','content':'Frozen persona: Review only.'}, {'role':'user','content':text}]))
        tb3.call.reset_mock()
        tb3.call.return_value = {'message':'actual model decision'}
        sim.step(observation)
        return sim, tb3.call.call_args

    def test_actual_step_keeps_dynamic_actions_facts_and_boolean_restrictions(self):
        for allowed, facts, all_facts in ((['message'], [], False), (['message'], ['F-current'], True),
                                          (['continue'], [], False),
                                          (['done'], [], False), (['abandon'], [], False)):
            with self.subTest(allowed=allowed):
                sim, args = self.fixture({'phase':'opening','allowed_actions':allowed,
                    'allowed_fact_ids':facts,'require_all_facts':all_facts,'proposed_command':None})
                prompt = args.kwargs['messages'][0]['content']
                example = examples(prompt)[0]
                Draft202012Validator(args.args[2]).validate(example)
                self.assertIn(example['action'], allowed)
                self.assertEqual(example['fact_ids'], facts if all_facts else [])
                self.assertIs(example['approve_command'], False)
                self.assertIs(example['approve_plan'], False)
                self.assertIn('message_type', example)
                self.assertIn('Frozen persona: Review only.', prompt)
                self.assertIn('all permissible scope changes', sim.rules())

    def test_complete_does_not_mutate_messages_and_handles_missing_system_role(self):
        sim, _ = self.fixture({'phase':'opening','allowed_actions':['message'],
            'allowed_fact_ids':[],'require_all_facts':False,'proposed_command':None})
        messages = [{'role':'user','content':'Current user-visible observation.'}]
        original = copy.deepcopy(messages)
        sim.complete(messages=messages)
        result = tb3.call.call_args.kwargs['messages']
        self.assertEqual(messages, original)
        self.assertEqual(result[0]['role'], 'system')
        sim.complete(messages=result)
        self.assertEqual(tb3.call.call_args.kwargs['messages'][0]['content'].count('## Response envelope'), 1)


class TogetherEnvelopeTests(unittest.TestCase):
    def test_due_example_is_exact_and_acceptance_fields_are_conditional(self):
        due = {'id':'R-current','statement':'Exact "request".\nPreserve \\ paths.'}
        for current, ready in ((due, False), (due, True), (None, False), (None, True)):
            with self.subTest(current=current, ready=ready):
                values = examples(together.decision_envelope(current, acceptance_ready=ready))
                self.assertEqual(len(values), 2 if ready and current is None else 1)
                for value in values:
                    expected = {'action','kind','content','requirement_id','base_task_complete','completion_reason'}
                    if value['action']!='accept':
                        self.assertIsNone(value['base_task_complete'])
                        self.assertIsNone(value['completion_reason'])
                    Draft202012Validator(together.decision_schema()).validate(value)
                    self.assertEqual(set(value), expected)
                if current:
                    self.assertEqual(values[0]['content'], due['statement'])
                    self.assertEqual(values[0]['requirement_id'], due['id'])
                else:
                    self.assertEqual(values[0]['content'], '')
                    self.assertIsNone(values[0]['requirement_id'])

    def test_repair_rebuilds_envelope_when_acceptance_releases_a_due_requirement(self):
        root = VENDOR/'swe_together/swe_together_pilot'
        case = json.loads((root/'persona_runtime/calibration-cases.json').read_text())[0]
        binding = json.loads((root/case['binding_file']).read_text())
        req = binding['persona']['bound_evolved_requirements'][0]
        seen = []
        async def call(role, system, payload):
            self.assertEqual(role, 'user_simulator')
            seen.append((system, copy.deepcopy(payload)))
            if len(seen)==1:
                return {'action':'accept','kind':'wait','content':'','requirement_id':None,
                    'base_task_complete':True,'completion_reason':'Review is complete.'}
            return {'action':'message','kind':'persona','content':req['statement'],'requirement_id':req['id']}
        sim = together.UserSimulator(binding, call)
        sim.native_disclosed = {r['id']:0 for r in binding.get('native_phase_contract',{}).get('required_followups',[])}
        turn = req['earliest_turn']
        # Turn is ready, but its frozen trigger has not happened. The premature
        # acceptance makes the next requirement due during the internal repair.
        req['deadline_turn'] = None
        observation = {'turn':turn,'events':[],'evidence':case['evidence'],
            'agent_message':'Please review my progress.','completion_attempt':False,'remaining_agent_turns':10}
        result = asyncio.run(sim.step(observation))
        self.assertEqual(len(seen), 2)
        self.assertIsNone(seen[0][1]['due'])
        self.assertEqual(examples(seen[0][0])[0]['action'], 'no-op')
        self.assertEqual(examples(seen[1][0])[0]['requirement_id'], req['id'])
        self.assertEqual(examples(seen[1][0])[0]['content'], req['statement'])
        self.assertEqual(result['action'], 'message')
        self.assertFalse(sim.invalid)


if __name__ == '__main__':
    unittest.main()
