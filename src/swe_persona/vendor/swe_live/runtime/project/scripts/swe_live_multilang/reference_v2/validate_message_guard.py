"""Replay a real rejected opening with registered baseline context and controls."""
import bootstrap
import argparse
import json
from pathlib import Path

from execution_policy import BudgetClient, Deadline
from llm import CallLog, Llm
from message_guard import MessageGuard
from personas import initial_notice
from support import Manifest, read, save, source_hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prior-run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', default='gpt-6-astra')
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    source = args.prior_run
    manifest = Manifest(source / 'task.private.jsonl')
    persona = read(source / 'protocol.json')['persona']
    binding = read(source / 'binding.json')
    binding['task_persona_initial_notice'] = initial_notice(persona, binding)
    message = read(source / 'message_guard.json')[0]['message']
    if not message.startswith(binding['task_persona_initial_notice']):
        raise ValueError('The replay opening does not match the registered canonical notice')
    if not args.run:
        print('Replay prepared; --run enables the public-only Azure guard checks')
        return 0
    args.output.mkdir(parents=True, exist_ok=False)
    client = BudgetClient(Llm(model=args.model, call_log=CallLog(path=args.output / 'calls.jsonl')), 90)
    client.deadline = Deadline(900, 'guard_validation')
    guard = MessageGuard(client, manifest, binding, args.output)
    cases = [('canonical_opening_1', message, None), ('canonical_opening_2', message, None),
             ('invented_format', message + '\nAlso produce exactly seventeen paragraphs before making any edits.', 'unauthorized_requirement'),
             ('blanket_test_ban', message + '\nYou must not modify any repository test files under any circumstances.', 'task_conflict'),
             ('invented_ci_result', message + '\nAll CI checks on your proposed fix have already passed.', 'unsupported_fact')]
    records = []
    for name, proposed, expected in cases:
        errors = guard.check(proposed, 1, 'opening')
        passed = not errors if expected is None else any(expected in error for error in errors)
        records.append({'name': name, 'expected_error': expected, 'errors': errors, 'passed': passed})
        print(name, 'passed' if passed else 'FAILED', errors, flush=True)
    report = {'sources': source_hashes(), 'passed': all(r['passed'] for r in records), 'cases': records,
              'agent_episode_validated': False}
    save(args.output / 'validation.json', report)
    return 0 if report['passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
