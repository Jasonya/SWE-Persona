"""Explicit phase budgets and concurrent independent rubric samples."""
from concurrent.futures import ThreadPoolExecutor
import copy
import time


class BudgetExpired(TimeoutError):
    pass


class Deadline:
    def __init__(self, seconds, phase, expires=None):
        self.phase, self.seconds = phase, seconds
        self.started = time.monotonic()
        self.expires = expires if expires is not None else self.started + seconds if seconds else None

    def remaining(self):
        if self.expires is None:
            return float("inf")
        value = self.expires - time.monotonic()
        if value <= 0:
            raise BudgetExpired(self.phase + " budget exhausted")
        return value


class BudgetClient:
    """The routed API owns three transport attempts within the phase deadline.

    Per-request SDK options use a shallow client copy, so three independent
    samples cannot race on a shared transport timeout setting.
    """
    def __init__(self, client, request_seconds=90, attempts=3):
        self.client, self.model = client, client.model
        self.request_seconds, self.attempts = request_seconds, attempts
        self.deadline = Deadline(900, "checker_calibration")

    def _invoke(self, method, args, kwargs):
        from llm import SchemaRepairFailed
        from swe_persona.api_recovery import is_api_interruption
        last = None
        routed = hasattr(self.client,'_api')
        attempts = 1 if routed else self.attempts
        for attempt in range(1, attempts + 1):
            remaining = self.deadline.remaining()
            timeout = min(3*self.request_seconds+3 if routed else self.request_seconds, remaining)
            bound = copy.copy(self.client)
            bound._request_timeout=min(self.request_seconds,remaining)
            bound._client = self.client._client.with_options(timeout=timeout, max_retries=0)
            options = dict(kwargs, attempts=1)
            if method == "json_obj":
                options["repairs"] = 0
            role = kwargs.get("role", "model")
            started = time.monotonic()
            print(f"[api] {role}; attempt={attempt}/{attempts}; timeout={timeout:.1f}s", flush=True)
            try:
                result = getattr(bound, method)(*args, **options)
                self.deadline.remaining()  # a late response cannot extend a phase
                print(f"[api] {role}; finished in {time.monotonic() - started:.1f}s", flush=True)
                return result
            except (SchemaRepairFailed, BudgetExpired):
                raise
            except Exception as exc:
                last = exc
                print(f"[api] {role}; {type(exc).__name__} after {time.monotonic() - started:.1f}s", flush=True)
                if routed or not is_api_interruption(exc):raise
                if attempt < attempts:
                    time.sleep(min(attempt, self.deadline.remaining()))
        raise RuntimeError(f"{self.model} transport failed after {attempts} attempts ({type(last).__name__})") from last

    def text(self, *args, **kwargs):
        return self._invoke("text", args, kwargs)

    def json_obj(self, *args, **kwargs):
        return self._invoke("json_obj", args, kwargs)


class IndependentSamples:
    """Execute the same three independent prompts concurrently, in stable vote order."""
    def __init__(self, client, samples=3):
        self.client, self.samples = client, samples
        self.pool = None
        self.futures = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.pool:
            self.pool.shutdown(wait=True, cancel_futures=True)

    def json_obj(self, *args, **kwargs):
        role = kwargs["role"]
        prefix, suffix = role.rsplit(".sample", 1)
        index = int(suffix)
        if not self.futures:
            if index != 1:
                raise ValueError("Rubric samples must begin at sample1")
            self.pool = ThreadPoolExecutor(max_workers=self.samples)
            for sample in range(1, self.samples + 1):
                options = dict(kwargs, role=f"{prefix}.sample{sample}")
                self.futures[sample] = self.pool.submit(self.client.json_obj, *copy.deepcopy(args), **options)
        return self.futures[index].result()
