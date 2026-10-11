"""Versioned interaction rules and host-owned evidence; no model or Docker calls."""
import copy
import hashlib
import json

VERSION = 'matched-total-budget-judge-v3.3'
FEEDBACK_LIMIT = 2400


def before_implementation(req):
    return req.get('template') in {'student_error_counterexample', 'designer_recovery_inventory'}


def explanation_artifact(req):
    params=req['checker'].get('parameters',{})
    if params.get('artifact'):
        return params['artifact']
    names=params.get('artifacts',[])
    if len(names)==1:
        return names[0]
    if not names or 'explanation.md' in names:
        return 'explanation.md'
    raise ValueError('Preimplementation requirement must identify one explanation artifact')


def effective_requirements(requirements):
    """Compile public protocol clarifications without changing published bindings."""
    result = copy.deepcopy(requirements)
    for req in result:
        extra = ''
        if before_implementation(req):
            artifact = explanation_artifact(req)
            extra = (f' Protocol {VERSION}: deliver the required artifact at /deliverables/{artifact} in a separate '
                     'tool action BEFORE the first source modification. You may revise it before editing source; '
                     'the harness freezes the last delivered preimplementation version of that artifact '
                     'and grades its contents. Writing it in the same tool action as a source edit, or replacing '
                     'it after implementation, cannot satisfy this ordering requirement. Read-only inspection '
                     'and exploratory checks in /tmp are allowed before delivery.')
        elif req['checker']['kind'] == 'reviewable_handoff':
            extra = (f' Protocol {VERSION}: report actual earlier failed validation attempts, including public '
                     'examples, temporary smoke tests and compilation checks, even if later fixed. Distinguish '
                     'historical failures from final validation and from non-test inspection/setup errors. '
                     'A final passing run does not mean that no earlier validation failed.')
        if extra and extra not in req.get('statement', ''):
            req['statement'] = req.get('statement', '') + extra
    return result


def execution_history(records, final_code_hash):
    """Keep every tool execution, not a keyword-selected subset of past tests."""
    if records is None:
        raise ValueError('Missing host execution history')
    result, seen = [], set()
    for record in records:
        action = record['action']
        if action['action'] not in ('shell', 'validate', 'terminal'):
            raise ValueError('Unexpected action in host execution history')
        number = record['action_number']
        if type(number) is not int or number <= 0 or number in seen:
            raise ValueError('Invalid or duplicate execution action number')
        seen.add(number)
        value = record['result']
        value = json.loads(value) if isinstance(value, str) else value
        terminal=action['action']=='terminal'
        if terminal:
            if value.get('code') is not None or value.get('command_exit_status')!='unknown':
                raise ValueError('Terminal observation must not claim a shell exit code')
        elif type(value.get('code')) is not int:
            raise ValueError('Missing execution exit code')
        output = value.get('output', value.get('output_tail', ''))
        result.append({'action_id': f'action-{number}', 'turn': record.get('turn'),
                       'kind': action['action'], 'command': action['content'],
                       'exit_code': value['code'],
                       **({'evidence_scope':'Terminal observation, not command completion; exit status unknown.'}
                          if terminal else {}),
                       'output_excerpt': output[:1500] + ('\n[... middle omitted ...]\n' + output[-2500:]
                                                         if len(output) > 4000 else output[1500:]),
                       'code_before': record.get('code_before'), 'code_after': record.get('code_after'),
                       'matches_final_code': bool(final_code_hash) and record.get('code_after') == final_code_hash})
    return result


class PreimplementationAudit:
    """Observe action boundaries, never infer historical ordering from final prose."""
    def __init__(self, requirements, initial_code_hash):
        self.records = {req['id']: {'artifact': explanation_artifact(req),
                                   'initial_code_sha256': initial_code_hash, 'disclosed_action': None,
                                   'first_source_change_action': None, 'early_artifact': None}
                        for req in requirements if before_implementation(req)}

    def disclose(self, key, action_number):
        if key in self.records:
            self.records[key]['disclosed_action'] = action_number

    def observe(self, action_number, before, after, artifacts):
        for record in self.records.values():
            initial = record['initial_code_sha256']
            if before != after and record['first_source_change_action'] is None:
                record['first_source_change_action'] = action_number
            content = artifacts.get(record['artifact'])
            if (record['first_source_change_action'] is None
                    and record['disclosed_action'] is not None and action_number > record['disclosed_action']
                    and before == after == initial and isinstance(content, str) and content.strip()):
                record['early_artifact'] = {'action_number': action_number, 'content': content,
                                            'sha256': hashlib.sha256(content.encode()).hexdigest(),
                                            'code_sha256': after}

    def snapshot(self):
        return copy.deepcopy(self.records)


def temporal_evidence(req, audits):
    """Return the immutable early explanation or a demonstrated ordering failure."""
    record = audits.get(req['id']) if isinstance(audits, dict) else None
    if not isinstance(record, dict) or not record.get('initial_code_sha256'):
        raise ValueError('Missing host preimplementation audit')
    early, changed, disclosed = (record.get(k) for k in
                                  ('early_artifact', 'first_source_change_action', 'disclosed_action'))
    if type(disclosed) is not int or disclosed < 0:
        raise ValueError('Missing disclosure in preimplementation audit')
    if changed is not None and (type(changed) is not int or changed <= 0):
        raise ValueError('Invalid first source change in preimplementation audit')
    if early is None:
        return None, ('The required artifact was not delivered in a separate action before the first source '
                      'modification.' if changed is not None else 'The required preimplementation artifact was not delivered.')
    expected = explanation_artifact(req)
    if (record.get('artifact') != expected or not isinstance(early.get('content'), str)
            or hashlib.sha256(early['content'].encode()).hexdigest() != early.get('sha256')
            or type(early.get('action_number')) is not int
            or early.get('code_sha256') != record['initial_code_sha256']):
        raise ValueError('Malformed host preimplementation artifact evidence')
    if early['action_number'] <= disclosed or (changed is not None and early['action_number'] >= changed):
        return None, 'The artifact was not delivered after disclosure and before the first source modification.'
    return early['content'], None


def concise_feedback(requirements, checks, disclosed_ids):
    """Bounded public feedback. Never forwards judge votes, citations or raw responses."""
    lines = ['Please address the following remaining requirements using the disclosed task and public evidence.']
    for req in requirements:
        key = req['id']
        if key not in disclosed_ids or checks.get(key, {}).get('status') == 'met':
            continue
        result = checks.get(key, {})
        artifact = req['checker'].get('parameters', {}).get('artifact')
        if result.get('repairable') is False:
            detail = 'The required order was missed and cannot be repaired retroactively; finish the remaining work honestly.'
        elif req['checker']['kind'] == 'grounded_explanation':
            detail = f'Check /deliverables/{artifact or "explanation.md"} against the disclosed example and actual code; provide concrete causal reasoning.'
        elif req['checker']['kind'] == 'reviewable_handoff':
            detail = 'Check the PR draft and validation manifest, including earlier failed checks, later repairs, final validation and unrun checks.'
        elif req['checker']['kind'] == 'isolated_patch':
            detail = 'Check the declared scope manifest, committed paths, protected content and remaining workspace changes.'
        else:
            detail = 'Recheck this disclosed requirement and its registered public validation evidence.'
        if result.get('status') == 'unknown':
            detail = 'Satisfaction is not established by the available evidence. ' + detail
        line = f'- {key}: {detail}'
        # Select only concise deterministic errors, never arbitrary judge text.
        errors = result.get('errors') or []
        if errors:
            line += ' Check: ' + ' '.join(str(errors[0]).split())[:180]
        if len('\n'.join(lines + [line])) > FEEDBACK_LIMIT - 130:
            lines.append('Additional disclosed requirements remain; consult the previous user requests.')
            break
        lines.append(line)
    return '\n'.join(lines)
