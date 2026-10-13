"""Exercise the production judge adapter and unchanged clause aggregation."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

import jsonschema

RUNTIME = Path(__file__).parents[1]/'src/swe_persona/vendor/swe_pro/shell/portable/runtime-v3'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class SchemaRepairFailed(RuntimeError):
    pass


class Client:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.calls = []

    def json_obj_once(self, messages, schema, **kwargs):
        self.calls.append((deepcopy(messages), deepcopy(schema), deepcopy(kwargs)))
        answer = deepcopy(next(self.answers))
        try:
            jsonschema.validate(answer, schema)
        except jsonschema.ValidationError as exc:
            raise SchemaRepairFailed('schema validation failed') from exc
        return answer

    json_obj = json_obj_once


class ClauseTruthTests(unittest.TestCase):
    def setUp(self):
        base = load('shell_truth_base_fixture', RUNTIME/'tb2_persona_pilot/rubric_checker.py')
        with patch.dict(sys.modules, {'tb2_persona_pilot.rubric_checker': base}), patch.object(sys, 'path', list(sys.path)):
            self.grader = load('shell_truth_registered_fixture', RUNTIME/'runner/rubric_checker.py')
        llm = types.ModuleType('llm')
        llm.SchemaRepairFailed = SchemaRepairFailed
        self.patch = patch.dict(sys.modules, {'llm': llm})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.spec = self.grader.RubricSpec.from_template({
            'met_if': ['All edits are in scope.'],
            'not_met_if': ['An unauthorized edit occurred.'],
            'unknown_if': ['The baseline is missing.'],
        }, samples=1)
        self.bundle = {'files': [{'path': '/app/receipt.json', 'text':
            'The baseline and complete change manifest are recorded.\n'}],
            'listing': [{'path': '/app/receipt.json'}]}

    def answer(self, met=True, negative=False, missing=False, ref='e00001'):
        return {'clauses': [{'clause_id': cid, 'condition_holds': truth,
                            'grounding': [] if truth is None else [{'reference_id': ref}],
                            'reason': 'The registered evidence establishes this condition assessment.'}
                           for cid, truth in [('met_if[0]', met), ('not_met_if[0]', negative),
                                              ('unknown_if[0]', missing)]],
                'overall_note': 'Literal conditions, independently assessed.'}

    def grade(self, answers, transcript=None):
        client = Client(answers)
        result = self.grader.grade(client, 'R1@1', 'Frozen requirement', self.spec,
                                   self.bundle, transcript or [])
        return client, result

    def test_literal_truth_controls_each_group_without_reversing_negative_conditions(self):
        for values, status, reason in [
            ((True, False, False), 'pass', 'rubric_all_met_clauses_grounded'),
            ((True, True, False), 'fail', 'rubric_violation_clause_satisfied'),
            ((False, False, False), 'fail', 'rubric_met_clause_violated'),
            ((True, False, True), 'unknown', 'rubric_unknown_clause_satisfied'),
            ((None, False, False), 'unknown', 'insufficient_evidence'),
        ]:
            with self.subTest(values=values):
                client, result = self.grade([self.answer(*values)])
                self.assertEqual(result[:2], (status, reason))
                self.assertEqual(len(client.calls), 1)
                self.assertIs(result[2]['clauses']['not_met_if[0]']['condition_holds'], values[1])
                self.assertEqual(result[2]['clause_protocol'], 'literal-clause-truth-v1')

    def test_wire_uses_boolean_or_null_and_original_clause_text(self):
        original = deepcopy(self.spec)
        client, _ = self.grade([self.answer()])
        messages, schema, _ = client.calls[0]
        props = schema['properties']['clauses']['items']['properties']
        self.assertNotIn('verdict', props)
        self.assertEqual(props['condition_holds']['type'], ['boolean', 'null'])
        payload = json.loads(messages[-1]['content'])
        self.assertEqual(payload['clauses_to_grade'], [
            {'clause_id': cid, 'group': group, 'clause': text}
            for cid, group, text in original.clause_index()])
        self.assertIn('not_met_if: true establishes a violation', messages[0]['content'])
        self.assertIn('unknown_if: true establishes', messages[0]['content'])
        self.assertNotIn('For every clause return exactly one verdict', messages[0]['content'])
        self.assertEqual(self.spec, original)

    def test_three_sample_majority_and_disagreement_keep_existing_semantics(self):
        self.spec.samples = 3
        for answers, expected in [
            ([self.answer(), self.answer(), self.answer(negative=True)], 'pass'),
            ([self.answer(), self.answer(negative=True), self.answer(negative=True)], 'fail'),
            ([self.answer(), self.answer(met=False), self.answer(met=None)], 'unknown'),
        ]:
            with self.subTest(expected=expected):
                client, result = self.grade(answers)
                self.assertEqual(result[0], expected)
                self.assertEqual(len(client.calls), 3)

    def test_invalid_boolean_is_repaired_once_on_identical_evidence(self):
        bad = self.answer()
        bad['clauses'][0]['condition_holds'] = 'false'
        client, result = self.grade([bad, self.answer()])
        self.assertEqual(result[0], 'pass')
        self.assertEqual(len(client.calls), 2)
        first, second = client.calls
        self.assertEqual(first[0], second[0][:-1])
        self.assertEqual(first[1:], second[1:])
        self.assertEqual([x['status'] for x in result[2]['judge_audit']], ['judge_error', 'valid'])

    def test_legacy_verdicts_numeric_booleans_and_invented_refs_cannot_bypass_protocol(self):
        legacy = self.answer()
        for clause in legacy['clauses']:
            clause['verdict'] = 'satisfied'
            del clause['condition_holds']
        numeric = self.answer()
        numeric['clauses'][0]['condition_holds'] = 1
        for answer in [legacy, numeric, self.answer(ref='invented')]:
            with self.subTest(answer=answer):
                client = Client([answer, answer])
                with self.assertRaises(self.grader.JudgeOutputError) as caught:
                    self.grader.grade(client, 'R1', 'Frozen', self.spec, self.bundle, [])
                self.assertEqual(len(client.calls), 2)
                self.assertTrue(all(x['status'] == 'judge_error' for x in caught.exception.audit))

    def test_false_condition_also_requires_eligible_grounding(self):
        answer = self.answer()
        answer['clauses'][1]['grounding'] = []
        with self.assertRaises(self.grader.JudgeOutputError):
            self.grade([answer, answer])

    def test_prose_cannot_override_an_explicit_true_violation(self):
        answer = self.answer(negative=True)
        answer['clauses'][1]['reason'] = 'No unauthorized edit occurred.'
        _, result = self.grade([answer])
        self.assertEqual(result[0], 'fail')
        self.assertIs(result[2]['clauses']['not_met_if[0]']['condition_holds'], True)

    def test_agent_claim_alone_cannot_prove_or_disprove_code_condition(self):
        self.bundle = {'files': [], 'listing': []}
        transcript = [{'role': 'agent', 'message_id': 'a1', 'text': 'All edits are in scope.'}]
        answer = self.answer()
        with self.assertRaises(self.grader.JudgeOutputError):
            self.grade([answer, answer], transcript)

    def test_failing_deterministic_subcheck_still_wins(self):
        client = Client([self.answer()])
        result = self.grader.grade(client, 'R1', 'Frozen', self.spec, self.bundle, [],
            {'checker': 'task_patch_manifest@1', 'status': 'fail', 'reason_code': 'out_of_scope'})
        self.assertEqual(result[:2], ('fail', 'subcheck_failed:out_of_scope'))


if __name__ == '__main__':
    unittest.main()
