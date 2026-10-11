"""Interactive extension of pinned Pier Trial, preserving its collect/verifier path.

The benchmark coordinator is outside BaseAgent.run. Only coding model/tool
operations enter that interface. Their persistent backend, context and task
environment are never reset at disclosure boundaries.
"""
import asyncio
from contextvars import copy_context
from concurrent.futures import Future
from datetime import datetime, timezone
import json
import threading
import time

from pier.agents.base import BaseAgent
from pier.models.agent.context import AgentContext
from pier.models.trial.config import TrialConfig
from pier.models.trial.result import TimingInfo
from pier.models.trajectories.trajectory import Trajectory
from pier.trial.hooks import TrialEvent
from pier.trial.trial import Trial, VerifierTimeoutError

from .pier_box import PierBox
from .coding_budget import active_deadline, CodingBudgetExceeded
from .pier_trace import Trace, RecordingClient, RecordingBackend, export
from .pier_native_grading import native_task_bundle, regular_bytes, import_grade
from .runtime_backend import runtime_metadata


class CodingAgent(BaseAgent):
    SUPPORTS_ATIF = True
    @staticmethod
    def name(): return 'dspilot-native-act'
    def version(self): return 'pier-native-v2'
    async def setup(self, environment):
        self.operation = None
        self.segments = []

    async def run(self, instruction, environment, context):
        # Never feed Trial's task instruction into the coding conversation.
        # The host coordinator supplies one existing backend operation at a time.
        if self.operation is None:
            raise ValueError('Coding operation missing')
        kind, function = self.operation
        self.operation = None
        start = time.monotonic()
        row = {'kind': kind, 'started_at': datetime.now(timezone.utc).isoformat()}
        worker = asyncio.create_task(asyncio.to_thread(function))
        try:
            deadline = getattr(function, 'coding_deadline', None)
            if deadline is None:
                self.value = await asyncio.shield(worker)
            else:
                done, _ = await asyncio.wait({worker}, timeout=max(0, deadline-time.monotonic()))
                if not done:
                    # Wait for finite I/O cancellation before snapshot/collect.
                    try: await worker
                    except Exception: pass
                    raise CodingBudgetExceeded('Cumulative coding time budget exhausted') from None
                self.value = worker.result()
            row['status'] = 'completed'
        except asyncio.CancelledError:
            row['status'] = 'cancelled'
            # The synchronous client/tool has its own finite deadline. Do not
            # tear down its environment while it is still executing.
            try: await worker
            except Exception: pass
            raise
        except Exception:
            row['status'] = 'error'
            raise
        finally:
            row.update(finished_at=datetime.now(timezone.utc).isoformat(),
                       seconds=time.monotonic()-start)
            self.segments.append(row)
            context.n_agent_steps = sum(s['kind']=='coding_model' for s in self.segments)
            context.metadata = dict(context.metadata or {},
                coding_execution_seconds=sum(s['seconds'] for s in self.segments),
                coding_segments=self.segments,
                timing_scope='Sum of coding model and agent-requested tool segments; excludes simulator, persona checks and final grader')
            (self.logs_dir/'coding-segments.json').write_text(json.dumps(self.segments, indent=2))


class InteractionBridge:
    """Thread-safe rendezvous; one coordinator and one operation in flight."""
    def __init__(self, loop):
        self.loop = loop
        self.queue = asyncio.Queue()
        self.cancelled = threading.Event()
        self.pending = set()
        self.lock = threading.Lock()

    def call(self, kind, payload):
        future = Future()
        if callable(payload):
            original, context = payload, copy_context()
            payload = lambda: context.run(original)
            payload.coding_deadline = active_deadline()
        with self.lock:
            if self.cancelled.is_set():
                raise RuntimeError('Pier trial stopped; no further operation permitted')
            self.pending.add(future)
        self.loop.call_soon_threadsafe(self.queue.put_nowait, (kind, payload, future))
        try: return future.result()
        finally:
            with self.lock: self.pending.discard(future)

    def stop(self):
        with self.lock:
            self.cancelled.set()
            for future in self.pending:
                if not future.done():
                    future.set_exception(RuntimeError('Pier trial stopped before completing operation'))


class InteractiveTrial(Trial):
    """Only extend interaction; upstream run owns setup/collect/verify/cleanup."""
    async def _verify_with_retry(self):
        """One bounded original verifier attempt; disable Pier's timeout retry."""
        started = time.monotonic()
        record = {'timeout_sec': self._verifier_timeout_sec, 'max_attempts': 1,
                  'started_at': datetime.now(timezone.utc).isoformat(), 'status': 'running'}
        path = self.trial_dir / 'verifier-policy.json'
        path.write_text(json.dumps(record, indent=2))
        try:
            self.result.verifier_result = await asyncio.wait_for(
                self._verify_once(step_cfg=None), timeout=self._verifier_timeout_sec)
            record['status'] = 'completed'
        except asyncio.TimeoutError as exc:
            record['status'] = 'timeout'
            raise VerifierTimeoutError(
                f'Verifier total budget exhausted after {self._verifier_timeout_sec} seconds; no retry') from exc
        except BaseException as exc:
            record['status'] = 'cancelled' if isinstance(exc, asyncio.CancelledError) else 'error'
            record['error_type'] = type(exc).__name__
            raise
        finally:
            record.update(seconds=time.monotonic()-started,
                          finished_at=datetime.now(timezone.utc).isoformat())
            path.write_text(json.dumps(record, indent=2))

    async def _execute_agent(self):
        from .api import Client
        from .coding_backend import create_backend
        from . import run_staged
        await self._invoke_hooks(TrialEvent.AGENT_START)
        self.result.agent_execution = TimingInfo(started_at=datetime.now(timezone.utc))
        self.result.agent_result = AgentContext(metadata={
            'outer_agent_execution_scope': 'Interaction envelope including coordinator waits; use coding_execution_seconds for active coding time'})
        self.bridge = InteractionBridge(asyncio.get_running_loop())
        self.trace = Trace(self._trial_paths.agent_dir/'benchmark-events.jsonl')
        box = PierBox(self._environment, self.bridge.loop, self.bridge.cancelled)
        trace, bridge = self.trace, self.bridge

        class BoundedClient(RecordingClient):
            @property
            def execution_cancelled(self):
                return bridge.cancelled.is_set()

            def create(self, messages, **kwargs):
                if bridge.cancelled.is_set():
                    raise RuntimeError('Pier cancellation forbids further model calls')
                return super().create(messages, **kwargs)

        class RoutedBackend(RecordingBackend):
            def next_action(self, number):
                return bridge.call('coding_model', lambda: self.inner.next_action(number))

        def official(patch, out, *, official):
            assert official
            return bridge.call('official', (patch, out))

        def coordinate():
            return run_staged.episode(self.config.trial_name, self.benchmark_arm,
                client_factory=lambda *a, **k: BoundedClient(Client(*a, **k), trace),
                box_factory=lambda **k: box,
                backend_factory=lambda *a, **k: RoutedBackend(create_backend(*a, **k), trace),
                tool_executor=lambda kind, fn: bridge.call('tool_'+kind, fn),
                official_evaluator=official)

        self.coordinator = asyncio.create_task(asyncio.to_thread(coordinate))
        self.coordinator.add_done_callback(lambda _: bridge.queue.put_nowait(('complete', None, None)))
        self.grade_request = None
        try:
            while True:
                kind, payload, future = await bridge.queue.get()
                if kind == 'complete':
                    await self.coordinator  # propagate setup/error; never fabricate grade
                    raise RuntimeError('Coordinator finished without a grading snapshot')
                if kind == 'official':
                    self.grade_request = (payload, future)
                    break  # resume upstream native collect and separate verification
                self._agent.operation = (kind, payload)
                try:
                    await self._execution.run_agent(instruction='', context=self.result.agent_result)
                    if not future.done(): future.set_result(self._agent.value)
                except asyncio.CancelledError:
                    bridge.stop()
                    raise
                except Exception as exc:
                    if not future.done(): future.set_exception(exc)
        finally:
            self.result.agent_execution.finished_at = datetime.now(timezone.utc)
            self._save_trace()

    def _save_trace(self):
        if any(e['kind']=='user' or (e['kind']=='response' and e['role']=='agent') for e in self.trace.events):
            trajectory = Trajectory.model_validate(export(self.trace.events,
                self.config.trial_name, self._agent.model_name))
            (self._trial_paths.agent_dir/'trajectory.json').write_text(
                trajectory.model_dump_json(indent=2, exclude_none=True))

    async def _maybe_download_logs(self, **kwargs):
        # Canonical host traces must never be overwritten by agent-created files.
        pass

    async def _maybe_upload_agent_logs(self):
        # Controller/simulator sidecars contain future context; never send them
        # back into the agent or verifier container.
        pass

    async def _run_verification(self):
        if self.grade_request is None:
            raise RuntimeError('No frozen benchmark patch for verification')
        (patch, out), future = self.grade_request
        try:
            collected = regular_bytes(self._trial_paths.artifacts_dir/'model.patch')
            if collected != patch.encode():
                raise ValueError('Native collected patch differs from benchmark snapshot')
            await super()._run_verification()
            grade = import_grade(self, patch, out)
            if not future.done(): future.set_result(grade)
            self.benchmark_result = await asyncio.shield(self.coordinator)
            self._save_trace()
            if not self.benchmark_result['evaluation_valid']:
                raise RuntimeError('Benchmark outcome invalid; original evidence retained')
        except BaseException as exc:
            # A verifier failure is not cancellation of the host-side final
            # assessment. Let it record the failure and finish its finite review
            # before cleanup stops the bridge. Actual cancellation forbids calls.
            if isinstance(exc, asyncio.CancelledError) and hasattr(self, 'bridge'):
                self.bridge.stop()
            if not future.done():
                future.set_exception(RuntimeError('Native verifier failed: '+type(exc).__name__))
            if hasattr(self, 'coordinator'):
                try:
                    self.benchmark_result = await asyncio.shield(self.coordinator)
                except Exception:
                    pass  # Preserve the original verifier exception.
            raise

    async def _cleanup_and_finalize(self):
        try:
            if hasattr(self, 'bridge'):
                self.bridge.stop()
                if hasattr(self, 'coordinator'):
                    try: await asyncio.shield(self.coordinator)
                    except Exception: pass
                self._save_trace()
        finally:
            # Malformed trajectory evidence must not prevent Docker teardown.
            await super()._cleanup_and_finalize()


async def run_trial(run_id, arm):
    from .task import ROOT, TASK, IMAGE, RESOURCES, EPISODE
    from .planning_staged import require_execution_admission
    from .prepare import verify_cache
    from .api import config
    metadata = runtime_metadata()
    if metadata['name'] != 'pier_native':
        raise ValueError('Native Pier entrypoint requires --runtime pier_native and a matching fresh freeze')
    require_execution_admission(EPISODE); verify_cache()
    if (ROOT/'runs'/run_id).exists() or (ROOT/'runs/pier-trials'/run_id).exists():
        raise ValueError('Run already exists; preserve original evidence')
    bundle = native_task_bundle(ROOT/'runs/pier-tasks'/run_id, TASK, IMAGE, RESOURCES,
        agent_timeout_sec=EPISODE.binding.raw['staged'].get('pier_agent_timeout_sec'),
        verifier_timeout_sec=EPISODE.binding.raw['staged'].get('pier_verifier_timeout_sec'))
    cfg = TrialConfig.model_validate({
        'task': {'path': bundle}, 'trials_dir': ROOT/'runs/pier-trials', 'trial_name': run_id,
        'agent': {'import_path': 'dspilot.pier_native_runtime:CodingAgent', 'model_name': config()['AGENT_MODEL']},
        'environment': {'import_path': 'dspilot.pier_native_grading:AuditedDockerEnvironment',
                        'delete': True, 'kwargs': {'mounts_json': []}},
        'verifier': {'disable': False}})
    trial = await InteractiveTrial.create(cfg)
    trial.benchmark_arm = arm
    (trial.trial_dir/'runtime-identity.json').write_text(json.dumps(metadata, indent=2))
    result = await trial.run()
    if result.exception_info is not None:
        raise RuntimeError('Pier native trial incomplete; inspect retained exception and benchmark evidence')
    return trial.benchmark_result


def run(run_id, arm): return asyncio.run(run_trial(run_id, arm))
