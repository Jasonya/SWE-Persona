"""Baseline-pass/persona-fail selection under the same interaction wall limit."""


def benchmark_outcome(run):
    episode = run.get('episode', {})
    terminal = run.get('terminal', episode.get('terminal'))
    elapsed = episode.get('interaction_seconds', episode.get('charged_seconds', 0))
    limit = episode.get('time_limit_seconds', run.get('time_limit_seconds', 0))
    # Deadline failure takes precedence over later diagnostic grades and errors.
    timed_out = episode.get('clock') == 'agent_user_wall' and (
        terminal == 'budget_exhausted' or (limit > 0 and elapsed >= limit)
    )
    if timed_out:
        return {'status': 'fail', 'score': 0, 'failure_reasons': ['wall_timeout']}
    if run.get('exception') or run.get('api_errors') or terminal not in {'ready','turn_limit','user_abandoned','agent_action_limit'}:
        return {'status': 'invalid', 'score': None, 'failure_reasons': [],
                'reason': 'Infrastructure, provider, protocol or controller error'}
    loop = episode.get('requirement_loop')
    if loop and terminal == 'ready' and not loop.get('perceived_complete'):
        return {'status': 'invalid', 'score': None, 'failure_reasons': [],
                'reason': 'Controller finished with unresolved requirement reviews'}
    reward = run.get('original_reward')
    failures = ['original_task_failure'] if reward is not None and reward < 1 else []
    unknown = reward is None
    if run.get('arm') in {'persona','persona_static','vague_no_evolution'}:
        checks = run.get('persona_check', {}).get('checks', {})
        ids = episode.get('disclosed_requirement_ids', [])
        required = set(run.get('required_requirement_ids', checks))
        for key in ids:
            status = checks.get(key, {}).get('status')
            if status == 'fail': failures.append('persona_requirement_failure:' + key)
            elif status != 'pass': unknown = True
        if set(ids) != required: unknown = True
        if terminal in {'turn_limit','user_abandoned','agent_action_limit'}:
            failures.append(terminal)
    # Unknown alone cannot establish failure; a proven unmet requirement can.
    if failures:
        return {'status': 'fail', 'score': 0, 'failure_reasons': failures}
    if unknown:
        return {'status': 'invalid', 'score': None, 'failure_reasons': [],
                'reason': 'No proven failure; required grading evidence is unavailable'}
    return {'status': 'pass', 'score': 1, 'failure_reasons': []}


def original_pass(run):
    return benchmark_outcome(dict(run, arm='single_turn'))['status'] == 'pass'


def negative_control(run):
    """Original-solution extension checks are diagnostic evidence only."""
    checks = run.get('persona_check', {}).get('checks', {})
    return bool(checks) and all(c['status'] == 'fail' for c in checks.values())


def conversion_candidate(baseline, persona):
    reasons = []
    if not original_pass(baseline):
        reasons.append('baseline_not_clean_pass')
    outcome = benchmark_outcome(dict(persona, arm='persona'))
    if outcome['status'] != 'fail':
        reasons.append('persona_' + outcome['status'])
    return {'candidate': not reasons, 'reasons': reasons,
            'failure_reasons': outcome['failure_reasons'], 'persona_outcome': outcome}
