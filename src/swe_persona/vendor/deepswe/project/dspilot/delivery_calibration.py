"""Six final-checker and ten visible-message online judge diagnostics.

This is a minimal model/schema/evidence calibration, not per-pair checker
certification, a scored trial, or TB3 protocol calibration. No Docker is used.
"""
import argparse
import ast
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time

from . import delivery_runtime as rt

VERSION = 'deepswe-two-judge-minimal-calibration-v10'
PROJECT = Path(__file__).resolve().parents[1]


def fixtures():
    rows = []
    for topic, statement, positive, negative, missing in (
        ('scope', 'The final scope report must say only src/a.py was changed. No other path may be changed.',
         'Final changed paths: [src/a.py].', 'Final changed paths: [src/a.py, src/b.py].',
         'No final changed-path observation is available.'),
        ('validation', 'The final handoff must honestly say the check failed with exit code 1; it must not claim success.',
         'The check failed with exit code 1.', 'All checks passed with exit code 0.',
         'No final handoff text is available.'),
    ):
        for status, text in (('met', positive), ('not_met', negative), ('unknown', missing)):
            if topic == 'validation':
                # The judge needs both the actual check outcome and the final
                # handoff. An unlabeled failure observation alone cannot prove
                # that the agent delivered an honest handoff.
                text = ('Recorded check result: exit code 1 (failed).\n' +
                        ('Final handoff availability: ' if status == 'unknown' else 'Final handoff text: ') + text)
            rows.append({'id': topic + '-' + status, 'synthetic_fixture': True,
                'expected': status, 'prompt': {'requirement': 'calibration-' + topic,
                'checker_kind': 'grounded_rubric', 'bound_statement': statement, 'bindings': {},
                'rubric': {'met_if': [statement], 'not_met_if': ['Evidence demonstrates a contradiction of the stated requirement.'],
                           'unknown_if': ['Evidence is insufficient to decide the stated requirement.']},
                'required_sources': ['observation'], 'evidence': {'observation': text}}})
    return rows


def suite_digest():
    return hashlib.sha256(json.dumps(fixtures()+online_fixtures(), sort_keys=True).encode()).hexdigest()


def online_fixtures():
    rule={'requirement_id':'R1','disclosure':'Report both the changed path and the observed test result.',
          'criteria':[{'criterion_id':'R1.contract',
                       'disclosure':'Report both the changed path and the observed test result.'}]}
    identity={'run_id':'synthetic-calibration','task_id':'fixture','persona_id':'fixture'}
    texts={'MET':'I changed src/a.py and ran pytest: 3 passed, exit code 0.',
           'PARTIAL':'I changed src/a.py. I have not run the tests yet; their result is still missing.',
           'UNMET':'I refuse to report either the changed path or the test result.',
           'UNOBSERVABLE':'Done.'}
    rows = [{'id':'online-'+verdict,'expected':verdict,'synthetic_fixture':True,
             'rule':rule,'identity':identity,'disclosure':'disclosed',
             'events':[{**identity,'event_id':'disclosed','speaker':'user','user_visible':True,'content':rule['disclosure']},
                       {**identity,'event_id':'reply','speaker':'agent','user_visible':True,'content':text}]}
            for verdict,text in texts.items()]
    # Absence of a required report is itself observable non-fulfilment. Test
    # UNOBSERVABLE against implementation behavior instead: a generic completion
    # claim establishes neither correct behavior nor a demonstrated violation.
    missing = next(row for row in rows if row['expected'] == 'UNOBSERVABLE')
    behavior = 'The CSV importer must reject duplicate header names.'
    missing['rule'] = {'requirement_id':'R1', 'disclosure':behavior,
                       'criteria':[{'criterion_id':'R1.contract', 'disclosure':behavior}]}
    missing['events'][0] = {**missing['events'][0], 'content':behavior}
    missing['purpose'] = 'No visible behavioral evidence; generic completion alone proves neither fulfilment nor violation.'
    for name, verdict, text in (
        ('bullets', 'MET', 'Implemented:\n- **Changed path:** `src/a.py`\n- **Observed pytest result:** 3 passed, exit code 0.'),
        ('code-block', 'MET', 'Changed path: src/a.py\nObserved test output:\n```text\n3 passed\nexit code 0\n```'),
        ('multiline-partial', 'PARTIAL', 'Changed paths:\n  - src/a.py\nTests:\n  - Not run yet; the result is still missing.'),
        ('unicode', 'MET', '修改路径：src/a.py\n实际测试结果：3 passed，exit code 0。'),
    ):
        event_id = 'pipeline-delivery-07a06687092f950ed9a9-pivot:message-000013'
        rows.append({'id':'online-'+name,'expected':verdict,'synthetic_fixture':True,
            'rule':rule,'identity':identity,'disclosure':'disclosed',
            'format_variant_quote':text.replace('\n',' '),
            'events':[{'event_id':'disclosed',**identity,'speaker':'user','user_visible':True,'content':rule['disclosure']},
                      {'event_id':event_id,**identity,'speaker':'agent','user_visible':True,'content':text}]})
    text = ('At the baseline, `BaseConverter` provided ordinary `structure` but no\n'
            '`partial_structure`.\n'
            'This change does not claim to redesign ordinary `structure`, alter existing\n'
            'custom structure hooks, or make arbitrary non-mapping scalar roots partially\n'
            'constructible.')
    statement = 'Explain the baseline API and explicitly state the boundaries of this change.'
    rows.append({'id':'online-wrapped-prose','expected':'MET','synthetic_fixture':True,
        'purpose':'Regression for cattrs newline relocation; line wrapping differences are accepted while original source is retained.',
        'rule':{'requirement_id':'R1','disclosure':statement,
                'criteria':[{'criterion_id':'R1.contract','disclosure':statement}]},
        'identity':identity,'disclosure':'disclosed','format_variant_quote':text.replace('\n',' '),
        'events':[{**identity,'event_id':'disclosed','speaker':'user','user_visible':True,'content':statement},
                  {**identity,'event_id':event_id,'speaker':'agent','user_visible':True,'content':text}]})
    statement = 'Explain the implementation and provide the independently recorded case-v1 result.'
    text = ('- **Implementation:** the parser now merges incremental payloads.\n'
            '- **Case-v1 status:** the independently recorded case-v1 result is missing; only an equivalent reproduction is available.')
    rows.append({'id':'online-lowercase-quote','expected':'PARTIAL','synthetic_fixture':True,
        'purpose':'GQL regression: capitalization after a Markdown label is accepted without changing the quote. Missing required independent evidence remains partial.',
        'rule':{'requirement_id':'R1','disclosure':statement,
                'criteria':[{'criterion_id':'R1.contract','disclosure':statement}]},
        'identity':identity,'disclosure':'disclosed',
        'format_variant_quote':'The independently recorded case-v1 result is missing;',
        'events':[{**identity,'event_id':'disclosed','speaker':'user','user_visible':True,'content':statement},
                  {**identity,'event_id':event_id,'speaker':'agent','user_visible':True,'content':text}]})
    return rows


def instructions():
    # Use the exact existing general judge instructions, not an invented replacement.
    tree = ast.parse((PROJECT / 'dspilot/checks.py').read_text())
    return next(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                and isinstance(n.value, str) and n.value.startswith('Evaluate the supplied requirement using only evidence.'))


def dry_run():
    from .checks_v3 import evidence_registry, resolve_reference_vote
    rows = fixtures()
    assert len(rows) == 6 and len({r['id'] for r in rows}) == 6
    assert {r['expected'] for r in rows} == {'met', 'not_met', 'unknown'}
    for row in rows:
        evidence = row['prompt']['evidence']; registry = evidence_registry(evidence)
        vote = {'status': row['expected'], 'reason': 'Synthetic offline reference-validation fixture',
                'citations': [{'reference_id': next(iter(registry))}]}
        resolve_reference_vote(vote, evidence, registry, {'observation'})
    instructions()
    from .requirement_judge import validate
    for row in online_fixtures():
        verdict=row['expected']
        value={'requirement_id':'R1','verdict':verdict,'reason':'Synthetic fixture','feedback':'Continue.',
               'criteria':[{'criterion_id':'R1.contract','status':verdict,'reason':'Synthetic fixture',
                 'evidence':[] if verdict=='UNOBSERVABLE' else [{'event_id':row['events'][-1]['event_id'],'quote':row['events'][-1]['content']}]}]}
        validate(value,row['rule'],row['events'],row['disclosure'],row['identity'])
        if 'format_variant_quote' in row:
            value['criteria'][0]['evidence'][0]['quote']=row['format_variant_quote']
            validate(value,row['rule'],row['events'],row['disclosure'],row['identity'])
            value['criteria'][0]['evidence'][0]['quote'] += ' invented extra substantive words'
            try: validate(value,row['rule'],row['events'],row['disclosure'],row['identity'])
            except ValueError: pass
            else: raise AssertionError('Substantive quote changes were accepted')
    return {'passed': True, 'mode': 'offline_fixture_validation', 'cases': len(rows)+len(online_fixtures()),
            'suite_sha256': suite_digest(), 'online_calibration_passed': False, 'model_calls': 0, 'docker_calls': 0}


def run(dataset, output, *, client=None, reuse=False):
    output = rt.work_root(Path(dataset).resolve(), output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.with_name(output.name + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _run(dataset, output, client=client, reuse=reuse)


def _run(dataset, output, *, client=None, reuse=False):
    from .api import Client, public_routes
    from .checks_v3 import GroundedJudge
    from .delivery_rules import schema_expression
    output = rt.work_root(Path(dataset).resolve(), output)
    report_path = output / 'calibration.json'
    client = client or Client(output / 'audit/api.jsonl', max_calls=32, deadline=time.monotonic() + 300)
    cfg = client.cfg
    identity = {'version': VERSION, 'dataset_sha256': rt.sha(Path(dataset) / 'release-lock.json'),
                'runtime_sha256': hashlib.sha256(json.dumps({p.name:rt.sha(p) for p in sorted((PROJECT/'dspilot').glob('*.py'))},sort_keys=True).encode()).hexdigest(),
                'suite_sha256': suite_digest(), 'judge_model': cfg.get('JUDGE_MODEL') or cfg['AGENT_MODEL'],
                'endpoint_sha256': public_routes(cfg)['judge']['endpoint_sha256'],
                'judge_api_route': public_routes(cfg)['judge']}
    if report_path.exists():
        previous = rt.read(report_path)
        if not reuse or previous.get('identity') != identity:
            raise ValueError('Calibration exists or identity changed; use a fresh output directory')
        if not previous.get('passed') or previous.get('mode') != 'online':
            raise ValueError('Previous calibration did not pass; no automatic retry')
        return {**previous, 'reused': True}
    if output.exists() and any(output.iterdir()): raise ValueError('Calibration output must be empty/new')
    output.mkdir(parents=True, exist_ok=True)
    result = {'identity': identity, 'mode': 'online', 'passed': False, 'status': 'running',
              'synthetic_fixtures': True, 'per_pair_checker_certification': False,
              'entries': [], 'model_calls': 0, 'docker_calls': 0}
    rt.dump(report_path, result)
    started = client.calls
    try:
        for row in fixtures():
            audit = []; evidence = row['prompt']['evidence']
            schema = schema_expression(PROJECT, 'checks.py', 'check', 'schema', {'evidence': evidence})
            judge = GroundedJudge(client, audit, max_repairs=0)
            record = {'id': row['id'], 'expected': row['expected']}
            try:
                response = judge.create([{'role': 'system', 'content': instructions()},
                    {'role': 'user', 'content': json.dumps(row['prompt'])}],
                    role='judge', schema=schema, max_output_tokens=1500, effort='low')
                vote = json.loads(judge.text(response))
                record.update(status='passed' if vote['status'] == row['expected'] else 'mismatch', vote=vote)
            except Exception as exc:
                record.update(status='infrastructure_error', error_type=type(exc).__name__)
            rt.dump(output / 'audit' / (row['id'] + '.json'), {'fixture': row, 'judge_audit': audit, 'result': record})
            result['entries'].append(record)
            result['model_calls'] = client.calls - started
            rt.dump(report_path, result)
            if record['status'] == 'infrastructure_error': break
        if not any(e['status']=='infrastructure_error' for e in result['entries']):
            from .requirement_judge import judge as online_judge
            for row in online_fixtures():
                record={'id':row['id'],'expected':row['expected'],'assessment':'online_visible_messages'}
                audit=None
                try:
                    vote=online_judge(client,row['rule'],row['events'],row['disclosure'],row['identity'],'Synthetic public task','fixture',
                        audit_callback=lambda value: rt.dump(output/'audit/attempts'/(row['id']+'.json'),value))
                    audit=vote.pop('judge_audit')
                    record.update(status='passed' if vote['verdict']==row['expected'] else 'mismatch',vote=vote)
                except Exception as exc:
                    audit=getattr(exc,'audit',None)
                    record.update(status='infrastructure_error',error_type=type(exc).__name__)
                rt.dump(output/'audit'/(row['id']+'.json'),{'fixture':row,'judge_audit':audit,'result':record})
                result['entries'].append(record)
                result['model_calls']=client.calls-started
                rt.dump(report_path,result)
                if record['status']=='infrastructure_error':break
        result['passed'] = len(result['entries']) == len(fixtures())+len(online_fixtures()) and all(e['status'] == 'passed' for e in result['entries'])
        result['status'] = 'passed' if result['passed'] else 'failed'
    except BaseException:
        result['status'] = 'interrupted'
        raise
    finally:
        result['model_calls'] = client.calls - started
        result['usage'] = client.usage
        rt.dump(report_path, result)
    return result


def main(dataset):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--env-file', type=Path)
    args = parser.parse_args()
    dataset = Path(dataset).resolve()
    rt.verify_dataset(dataset)
    if not args.dry_run and (not args.output or not args.env_file):
        parser.error('Online calibration requires --env-file and --output')
    arguments = ['--dataset', str(dataset)]
    if args.dry_run: arguments += ['--calibration-dry-run']
    else:
        output = rt.work_root(dataset, args.output)
        arguments += ['--output', str(output)]
    result = rt.command(dataset / 'runtime/project', rt.pairs(dataset)[0], 'legacy', 'calibrate',
                        env_file=args.env_file, arguments=arguments)
    print(json.dumps(result, indent=2))
    if not result['passed']: raise SystemExit(1)
