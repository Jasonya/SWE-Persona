"""Conservative adaptive timing: censor long references without guessing P20."""
import math


def round_ceiling(initial, maximum, index):
    if not 0 < initial <= maximum or index < 1:
        raise ValueError('Invalid adaptive calibration bounds')
    return min(maximum, initial * 2 ** (index - 1))


def next_reference_cap(records, total_tasks, ceiling):
    # At least ceil(.2 * full population) completed observations establish an
    # upper bound on the eventual P20, including any future completions. That
    # upper bound can only decrease. Never tighten from a small running pilot.
    rank = math.ceil(total_tasks * .2)
    completed = sorted(r['seconds'] for r in records if r['status'] == 'completed')
    return min(ceiling, completed[rank - 1]) if len(completed) >= rank else ceiling


def can_extend(records):
    return any(r['status'] in ('timeout', 'gold_preflight_timeout') for r in records)
