"""Cumulative active coding time, shared by legacy and Pier coordinators.

Deadlines apply only inside model/tool operations. Cleanup and final assessment
run outside the deadline, so a timeout still has a recoverable scored artifact.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import time

DEFAULT_CODING_SECONDS = 3600
SCOPE = 'coding model calls (including retries), agent shell/terminal and agent-requested public validation; excludes simulator, judges, setup and final grading'
_deadline = ContextVar('coding_deadline', default=None)


class CodingBudgetExceeded(RuntimeError):
    pass


def coding_seconds(plan):
    value = plan.get('budgets', {}).get('coding_seconds', DEFAULT_CODING_SECONDS)
    if type(value) is not int or value <= 0:
        raise ValueError('coding_seconds must be a positive integer')
    return value


def active_deadline():
    return _deadline.get()


def bounded_timeout(seconds):
    deadline = active_deadline()
    if deadline is None:
        return seconds
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CodingBudgetExceeded('Cumulative coding time budget exhausted')
    return min(seconds, remaining)


@contextmanager
def suspended():
    """Always permit bounded cleanup; do not strand containers after expiration."""
    token = _deadline.set(None)
    try:
        yield
    finally:
        _deadline.reset(token)


class CodingBudget:
    def __init__(self, seconds=DEFAULT_CODING_SECONDS):
        self.limit = coding_seconds({'budgets': {'coding_seconds': seconds}})
        self.spent = 0.0
        self.segments = []
        self.exhausted = False

    def run(self, kind, function):
        if self.exhausted or self.spent >= self.limit:
            self.exhausted = True
            raise CodingBudgetExceeded('Cumulative coding time budget exhausted')
        started = time.monotonic()
        deadline = started + self.limit - self.spent
        token = _deadline.set(deadline)
        status = 'completed'
        try:
            return function()
        except BaseException:
            status = 'error'
            raise
        finally:
            elapsed = time.monotonic() - started
            _deadline.reset(token)
            self.spent += elapsed
            expired = time.monotonic() >= deadline
            self.exhausted |= expired
            self.segments.append({'kind': kind, 'seconds': elapsed,
                                  'status': 'coding_timeout' if expired else status})
            if expired:
                # A late model response must never dispatch another tool action.
                raise CodingBudgetExceeded('Cumulative coding time budget exhausted') from None

    def receipt(self):
        return {'limit_seconds': self.limit, 'execution_seconds': self.spent,
                'remaining_seconds': max(0.0, self.limit - self.spent),
                'exhausted': self.exhausted, 'scope': SCOPE,
                'segments': self.segments,
                'stop_policy': 'Stop coding at budget; bounded in-flight cancellation/cleanup may finish later; preserve snapshot and run final assessment'}
