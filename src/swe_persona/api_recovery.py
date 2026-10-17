"""Bounded API transport recovery and evidence-based interrupted-run scoring."""
import json
import ssl
from pathlib import Path
import time
import urllib.error

from openai import APIConnectionError, InternalServerError, RateLimitError

POLICY = 'api-recovery-and-artifact-assessment-v1'
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (1, 2)


class APITransportExhausted(RuntimeError):
    """A transient API failure persisted after bounded recovery attempts."""
    def __init__(self, attempts, error_type):
        self.attempts = attempts
        self.error_type = error_type
        super().__init__(f'API transport recovery exhausted after {attempts} attempts ({error_type})')


def is_transport_error(error):
    # urllib may wrap a handshake error, while response reads can raise the SSL
    # exception directly. SDK connection errors may wrap it via __cause__.
    current, seen = error, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLError):
            if isinstance(current, ssl.SSLCertVerificationError):
                return False
            return isinstance(current, (ssl.SSLEOFError, ssl.SSLZeroReturnError)) or (
                getattr(current, 'reason', None) == 'UNEXPECTED_EOF_WHILE_READING')
        current = (current.reason if isinstance(current, urllib.error.URLError)
                   and isinstance(current.reason, BaseException) else current.__cause__)
    if isinstance(error, (APIConnectionError, InternalServerError, RateLimitError, urllib.error.URLError)):
        if isinstance(error, urllib.error.HTTPError):
            return error.code in (408, 429) or error.code >= 500
        return True
    if type(error) is TimeoutError:
        return True
    # LiteLLM uses its own classes; avoid importing it into every profile.
    return type(error).__module__.startswith('litellm') and type(error).__name__ in {
        'Timeout', 'APIConnectionError', 'RateLimitError', 'ServiceUnavailableError', 'InternalServerError'}


def is_api_interruption(error):
    """Follow explicit exception causes, never classify arbitrary error strings."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, APITransportExhausted) or (type(error) is not TimeoutError and is_transport_error(error)):
            return True
        error = error.__cause__
    return False


def retry_transport(operation, *, request_timeout, deadline=None, audit=None):
    """Retry only API requests. Tool execution and episode clocks never restart.

    request_timeout limits each HTTP attempt. An explicit deadline bounds all
    attempts and backoff together; otherwise at most three request timeouts are
    available. Authentication, schema errors, and cancellation are not retried.
    """
    if deadline is None:
        deadline = time.monotonic() + MAX_ATTEMPTS * request_timeout + sum(BACKOFF_SECONDS)
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        started = time.monotonic()
        try:
            result = operation(min(request_timeout, remaining))
        except Exception as error:
            if not is_transport_error(error):
                raise
            last = error
            remaining = deadline - time.monotonic()
            retrying = attempt < MAX_ATTEMPTS and remaining > 0
            if audit:
                audit({'policy': POLICY, 'attempt': attempt, 'status': 'retrying' if retrying else 'exhausted',
                       'error_type': type(error).__name__, 'elapsed_sec': time.monotonic() - started,
                       'usage_unknown': True})
            if not retrying:
                raise APITransportExhausted(attempt, type(error).__name__) from error
            time.sleep(min(BACKOFF_SECONDS[attempt - 1], remaining))
        else:
            if audit and attempt > 1:
                audit({'policy': POLICY, 'attempt': attempt, 'status': 'recovered'})
            return result
    if audit:
        audit({'policy': POLICY, 'attempt': attempt - 1, 'status': 'exhausted',
               'error_type': type(last).__name__ if last else 'RequestBudgetExpired', 'usage_unknown': True})
    raise APITransportExhausted(attempt - 1, type(last).__name__ if last else 'RequestBudgetExpired') from last


def append_audit(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        stream.write(json.dumps(record) + '\n')


def transport_was_exhausted(directory):
    """Detect audited failures even when a legacy checker catches the exception."""
    for path in Path(directory).rglob('transport-recovery.jsonl'):
        for line in path.read_text().splitlines():
            record = json.loads(line)
            if record.get('policy') == POLICY and record.get('status') == 'exhausted':
                return True
    return False


def assess_interrupted(native, persona, *, artifacts_available, progress, integrity=True, budget_exhausted=False):
    """Score preserved work after an API interruption, without inventing grades.

    An attempt with no usable work remains infrastructure_error. Preserved work
    is scored using independent final verification. Only verified native AND
    persona success earns a pass; missing grades on preserved work earn zero
    joint credit and remain unknown in component fields.
    """
    complete = isinstance(native, bool) and isinstance(persona, bool)
    if budget_exhausted:
        status, reason = 'fail', 'Interaction or task time budget exhausted; zero joint credit'
    elif not integrity:
        status, reason = 'invalid', 'Artifact or evaluation integrity check failed'
    elif not artifacts_available or not progress:
        status, reason = 'infrastructure_error', 'API interruption left no usable agent work or final artifacts'
    elif native is True and persona is True:
        status, reason = 'pass', 'Preserved artifacts pass independent native and persona verification'
    else:
        status, reason = 'fail', 'Preserved work has an unmet or unverified required component; zero joint credit'
    return {'status': status, 'reason': reason, 'policy': POLICY,
            'verification_complete': complete, 'api_interrupted': True,
            'native_pass': native, 'persona_pass': persona,
            'budget_exhausted': bool(budget_exhausted),
            'artifacts_available': bool(artifacts_available), 'agent_progress': bool(progress)}
