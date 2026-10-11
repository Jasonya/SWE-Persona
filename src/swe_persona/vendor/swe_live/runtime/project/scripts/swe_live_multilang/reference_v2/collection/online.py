"""Online, censored P20 estimates; no separate all-task timing pass.

This is an adaptive-budget experiment, not exact advance selection of the
globally fastest fifth. Preserve the cap and status of every measured sample.
"""
from collections import defaultdict
import hashlib
import math

PEER_INITIAL_SECONDS = 900.0
PEER_SOURCE = {
    'url': 'https://github.com/skyve2012/PersonaCodeBench/blob/3560b5a6b55e205d37d0916a99f768a09a9c1a55/deepswe_persona_pilot/log/first100-review/summary.json',
    'valid_records': 93, 'rank': 19, 'end_to_end_p20_seconds': 895.690609625075,
    'rounding': 'up to the next whole minute',
    'limitation': 'Cross-dataset initialization only; includes valid budget-terminated trajectories. Not a MultiLang quantile.'}


def censored_p20(records):
    """Kaplan-Meier P20 estimate under administrative censoring.

    Adaptive caps and task heterogeneity make this an online estimate, not an
    exactly identified order statistic of all uncapped completion times.
    """
    times = defaultdict(lambda: [0, 0])
    invalid = 0
    for record in records:
        status = record['status']
        if status not in ('completed', 'timeout'):
            invalid += 1
            continue
        value = record['seconds'] if status == 'completed' else record['cap_seconds']
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError('Invalid online timing observation')
        times[value][status == 'timeout'] += 1
    population = sum(sum(counts) for counts in times.values())
    risk, survival, threshold = population, 1.0, None
    for seconds, (events, censored) in sorted(times.items()):
        survival *= 1 - events / risk
        if threshold is None and survival <= .8 + 1e-12:
            threshold = seconds
        risk -= events + censored
    return {'seconds': threshold, 'population': population, 'excluded_invalid': invalid,
            'completed': sum(v[0] for v in times.values()), 'right_censored': sum(v[1] for v in times.values()),
            'method': 'Kaplan-Meier P20 online estimate; not exact global fastest-20% selection',
            'exact_global_rank_claim': False}


def budget_after(records, initial=PEER_INITIAL_SECONDS, maximum=3600, update_every=20):
    if not 0 < initial <= maximum or update_every < 1:
        raise ValueError('Invalid online budget configuration')
    eligible = [r for r in records if r['status'] in ('completed', 'timeout')]
    cap, history = initial, []
    for size in range(update_every, len(eligible) + 1, update_every):
        estimate = censored_p20(eligible[:size])
        target = estimate['seconds'] if estimate['seconds'] is not None else cap * 1.5
        cap = min(maximum, max(cap * .75, min(cap * 1.5, target)))
        history.append({'observations': size, 'next_cap_seconds': cap, 'estimate': estimate})
    return {'next_cap_seconds': cap, 'updates': history, 'estimate': censored_p20(records),
            'update_every': update_every, 'applies_to': 'future samples only; matched arms retain their frozen pair cap'}


def advance_budget(state, records, maximum=3600):
    """Incremental production path; do not replay all old updates per sample."""
    if len(records) != state.get('records_seen', 0) + 1:
        raise ValueError('Online history must advance by exactly one retained record')
    result = dict(state, records_seen=len(records))
    observed = state.get('observations_seen', 0) + int(records[-1]['status'] in ('completed', 'timeout'))
    result['observations_seen'] = observed
    if observed and observed % state['update_every'] == 0 and observed != state.get('observations_seen', 0):
        estimate = censored_p20(records)
        previous = state['next_cap_seconds']
        target = estimate['seconds'] if estimate['seconds'] is not None else previous * 1.5
        cap = min(maximum, max(previous * .75, min(previous * 1.5, target)))
        result.update(next_cap_seconds=cap, estimate=estimate, estimate_observations=observed,
                      updates=state['updates'] + [{'observations': observed, 'next_cap_seconds': cap, 'estimate': estimate}])
    return result


def ordered(items, identity, seed):
    return sorted(items, key=lambda item: hashlib.sha256((seed + ':' + identity(item)).encode()).hexdigest())
