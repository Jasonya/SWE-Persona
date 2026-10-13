"""Run one trial of one condition and write the TB3 v6 section 9 trial files.

Budget (v6 section 5): one wall clock of `agent.timeout_sec` starts with the first agent
turn and covers the agent, simulator, online judge, tools and snapshots; at most 24 user
turns and 64 simulator decisions.  Exhausting any of them ends the conversation as a
failure; nothing is skipped.  Environment setup before the first turn and the objective
evaluation after the freeze are timed separately.

Outcome (v6 section 6): the online verdicts, the original verifier and the persona
checker are recorded separately.  `benchmark_outcome` is
  pass                  online `done` (every requirement MET on the final recheck) AND
                        original verifier pass AND every checker requirement pass;
                        single_turn needs only the original verifier
  fail                  the agent side did not deliver: original verifier fail, a
                        checker fail, or the budget ran out before `done`
  invalid               the protocol was broken (e.g. the simulator leaked hidden material)
  infrastructure_error  API, schema, evidence-validation, Docker or evaluator failure, or
                        a checker that could not decide
"""

from __future__ import annotations

import json
import signal
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

from . import PROTOCOL_VERSION, RUNTIME_VERSION
from .controller import Controller
from .judge import JudgeProtocolError, RequirementJudge
from .llm import LlmError
from .simulator import UserSimulator
from .task import TaskBundle

FAIL_END_STATES = ("time_limit_exceeded", "user_turns_exhausted", "simulator_decisions_exhausted")
INFRA_END_STATES = ("protocol_error", "infrastructure_error", "agent_error")
# A budget that ran out while the agent made no step for this long was lost to the harness (a
# model request that kept timing out), not spent by the agent: one agent command is capped at
# 600 s and every step updates the trajectory.
STALL_LIMIT_SEC = 900


@dataclass
class AgentReply:
    text: str
    status: str            # working (asked the user) | done (submitted)
    steps: int = 0


class AgentTimeout(RuntimeError):
    pass


class _DeadlineInterrupt(BaseException):
    """Do not let provider retry handlers swallow an episode deadline."""


class AgentFailure(RuntimeError):
    pass


class AgentSession(Protocol):
    def start(self) -> None: ...
    def turn(self, message: str, turn: int, timeout_sec: float, interactive: bool) -> AgentReply: ...
    def quiesce(self) -> Dict[str, Any]: ...
    def freeze(self) -> str: ...
    def stop(self) -> None: ...


class Evaluator(Protocol):
    def original(self, image: str) -> Dict[str, Any]: ...
    def persona_checker(self, image: str, transcript: List[Dict[str, Any]],
                        requirements: List[Dict[str, Any]],
                        disclosed_turns: Dict[str, int]) -> Dict[str, Any]: ...


@dataclass
class Budget:
    timeout_sec: float
    max_user_turns: int = 24
    max_decisions: int = 64
    started: Optional[float] = None
    user_turns: int = 0
    decisions: int = 0
    clock: Any = time.monotonic

    def start(self) -> None:
        self.started = self.clock()

    def used(self) -> float:
        return 0.0 if self.started is None else self.clock() - self.started

    def remaining(self) -> float:
        return self.timeout_sec - self.used()

    def check(self) -> None:
        if self.remaining() <= 0:
            raise AgentTimeout("episode wall-clock budget exhausted")

    @contextmanager
    def guard(self):
        self.check()
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("episode calls must run on the main thread for deadline enforcement")
        old_handler = signal.getsignal(signal.SIGALRM)
        old_timer = signal.getitimer(signal.ITIMER_REAL)
        start = time.monotonic()
        def expire(signum, frame):
            raise _DeadlineInterrupt()
        signal.signal(signal.SIGALRM, expire)
        signal.setitimer(signal.ITIMER_REAL, self.remaining())
        try:
            yield
        except _DeadlineInterrupt as exc:
            raise AgentTimeout("episode wall-clock budget exhausted during model call") from exc
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old_handler)
            if old_timer[0] > 0:
                signal.setitimer(signal.ITIMER_REAL,
                                max(0.001, old_timer[0] - (time.monotonic() - start)), old_timer[1])
        self.check()

    def public(self) -> Dict[str, Any]:
        return {"seconds_left": max(0, int(self.remaining())),
                "user_turns_left": self.max_user_turns - self.user_turns}

    def to_json(self) -> Dict[str, Any]:
        return {"timeout_sec": self.timeout_sec, "used_sec": round(self.used(), 3),
                "max_user_turns": self.max_user_turns, "user_turns": self.user_turns,
                "max_simulator_decisions": self.max_decisions, "simulator_decisions": self.decisions}


@dataclass
class Timing:
    seconds: Dict[str, float] = field(default_factory=dict)
    clock: Any = time.monotonic

    @contextmanager
    def measure(self, phase: str):
        started = self.clock()
        try:
            yield
        finally:
            self.seconds[phase] = round(self.seconds.get(phase, 0.0) + self.clock() - started, 3)


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def benchmark_outcome(condition: str, end_state: Optional[str], original: Dict[str, Any],
                      checker: Optional[Dict[str, Any]]) -> Dict[str, str]:
    if end_state in INFRA_END_STATES:
        return {"outcome": "infrastructure_error", "reason": end_state}
    if end_state == "invalid":
        return {"outcome": "invalid", "reason": "protocol_violation"}
    if end_state in FAIL_END_STATES:
        return {"outcome": "fail", "reason": end_state}
    status = original.get("status")
    if status == "unknown" and original.get("reason_code") != "hidden_test_patch_conflict":
        return {"outcome": "infrastructure_error", "reason": f"original_verifier:{original.get('reason_code')}"}
    original_pass = status == "pass"
    if condition == "single_turn":
        return {"outcome": "pass" if original_pass else "fail",
                "reason": "original_pass" if original_pass else "original_fail"}
    if end_state != "done":
        return {"outcome": "fail", "reason": end_state or "incomplete"}
    if not original_pass:
        return {"outcome": "fail", "reason": "original_fail"}
    statuses = [item["status"] for item in (checker or {}).get("requirements", {}).values()]
    if "fail" in statuses:
        return {"outcome": "fail", "reason": "checker_fail"}
    if "unknown" in statuses or (condition != "vague_no_evolution" and not statuses):
        return {"outcome": "infrastructure_error", "reason": "checker_unknown"}
    return {"outcome": "pass", "reason": "all_passed"}


class Episode:
    def __init__(self, task: TaskBundle, condition: str, agent: AgentSession, evaluator: Evaluator,
                 judge: Optional[RequirementJudge], simulator: Optional[UserSimulator],
                 out_dir: Path, models: Dict[str, str], clock: Any = time.monotonic) -> None:
        self.task = task
        self.condition = condition
        self.agent = agent
        self.evaluator = evaluator
        self.judge = judge
        self.simulator = simulator
        self.out = Path(out_dir)
        self.models = models
        policy = task.policy.get("budget", {})
        self.budget = Budget(task.timeout_sec, policy.get("max_user_turns", 24),
                             policy.get("max_simulator_decisions", 64), clock=clock)
        self.timing = Timing(clock=clock)
        self.final_samples = int(task.policy.get("judge", {}).get("final_recheck_samples", 3))
        self.online_samples = int(task.policy.get("judge", {}).get("online_samples", 1))
        self.decisions: List[Dict[str, Any]] = []
        self.judgments: List[Dict[str, Any]] = []
        self.errors: List[str] = []
        self.frozen: Optional[Dict[str, Any]] = None

    # ---- helpers ----------------------------------------------------------------
    def _judge(self, controller: Controller, item, turn: int, purpose: str, samples: int) -> None:
        with self.budget.guard(), self.timing.measure("judge"):
            result = self.judge.judge(item.rubric(), self.task.persona_card, self.task.public_task,
                                      controller.transcript, item.disclosed_turn,
                                      samples=samples, purpose=purpose)
        record = result.to_json()
        controller.record_verdict(item, record, turn, purpose)
        index = len(self.judgments) + 1
        self.judgments.append({"turn": turn, **record})
        _write(self.out / "agent-private" / "requirement-judge-audit"
               / f"{index:03d}-{item.requirement_id}-{purpose}.json",
               {"turn": turn, "result": record, "attempts": result.audit})

    def _agent_turn(self, message: str, turn: int, interactive: bool):
        remaining = self.budget.remaining()
        if remaining <= 0:
            raise AgentTimeout("budget exhausted before the agent turn")
        with self.timing.measure("agent"):
            return self.agent.turn(message, turn, remaining, interactive)

    # ---- conditions -------------------------------------------------------------
    def _single_turn(self) -> Dict[str, Any]:
        transcript = [{"event_id": "u-1", "role": "user", "turn": 1,
                       "text": self.task.original_instruction, "source": "original_instruction"}]
        end_state = "done"
        try:
            reply = self._agent_turn(self.task.original_instruction, 1, interactive=False)
            transcript.append({"event_id": "a-1", "role": "agent", "turn": 1,
                               "text": reply.text, "status": reply.status})
            if reply.status == 'api_interrupted':
                end_state = 'api_interrupted'
        except AgentTimeout:
            end_state = "time_limit_exceeded"
        except AgentFailure as exc:
            end_state = "agent_error"
            self.errors.append(f"agent: {exc}")
        self.budget.user_turns = 1
        return {"end_state": end_state, "transcript": transcript, "controller": None}

    def _loop(self) -> Dict[str, Any]:
        controller = Controller(self.condition, self.task.requirements(), self.task.native_facts(),
                                self.task.hidden_markers)
        turn = 1
        message = self.task.opening
        controller.add_user_message(message, turn, source="frozen_opening")
        self.budget.user_turns = 1
        try:
            while True:
                reply = self._agent_turn(message, turn, interactive=True)
                controller.add_agent_message(reply.text, turn, reply.status)
                if reply.status == 'api_interrupted':
                    controller.end_state = 'api_interrupted'
                    break
                for item in controller.needs_judgment(turn):
                    if self.budget.remaining() <= 0:
                        raise AgentTimeout("budget exhausted during online judging")
                    self._judge(controller, item, turn, "online", self.online_samples)
                if self.budget.remaining() <= 0:
                    raise AgentTimeout("budget exhausted after the agent turn")
                if self.budget.user_turns >= self.budget.max_user_turns:
                    controller.end_state = "user_turns_exhausted"
                    break
                if self.budget.decisions >= self.budget.max_decisions:
                    controller.end_state = "simulator_decisions_exhausted"
                    break
                next_turn = turn + 1
                decision, source = self._decide(controller, next_turn)
                self.budget.check()
                if decision["action"] == "done":
                    if self._final_recheck(controller, turn):
                        self.budget.check()
                        controller.end_state = "done"
                        controller.event("done", turn, message=decision["user_message"][:500])
                        break
                    decision = controller.fallback_decision(
                        controller.observation(next_turn, self.task.public_task, self.budget.public()))
                    source = "final_recheck_followup"
                message = decision["user_message"]
                controller.add_user_message(message, next_turn, source=source)
                self.budget.user_turns += 1
                turn = next_turn
        except AgentTimeout:
            controller.end_state = "time_limit_exceeded"
        except AgentFailure as exc:
            controller.end_state = "agent_error"
            self.errors.append(f"agent: {exc}")
        except JudgeProtocolError as exc:
            controller.end_state = "protocol_error"
            self.errors.append(f"judge: {exc}")
        except LlmError as exc:
            from swe_persona.api_recovery import is_api_interruption
            controller.end_state = 'api_interrupted' if is_api_interruption(exc) else "infrastructure_error"
            self.errors.append(f"llm: {exc}")
        return {"end_state": controller.end_state, "transcript": controller.transcript,
                "controller": controller}

    def _decide(self, controller: Controller, turn: int):
        observation = controller.observation(turn, self.task.public_task, self.budget.public())
        with self.budget.guard(), self.timing.measure("simulator"):
            outcome = self.simulator.decide(observation, controller.validate_decision)
        self.budget.decisions += 1
        decision = outcome["decision"]
        source = "simulator"
        if outcome["fallback"]:
            decision = controller.fallback_decision(observation)
            source = "fallback"
        self.decisions.append({"turn": turn, "source": source, "decision": decision,
                               "attempts": outcome["attempts"]})
        _write(self.out / "agent-private" / "audit" / f"simulator-{turn:03d}.json",
               {"observation": observation, **self.decisions[-1]})
        return decision, source

    def _final_recheck(self, controller: Controller, turn: int) -> bool:
        """Re-judge every disclosed requirement; any regression reopens the conversation."""
        for item in controller.active():
            self._judge(controller, item, turn, "final_recheck", self.final_samples)
        regressed = [item.requirement_id for item in controller.active() if item.verdict != "MET"]
        if regressed:
            controller.event("final_recheck_regression", turn, requirements=regressed)
        return not regressed and controller.done_allowed()

    # ---- run ----------------------------------------------------------------------
    def run(self) -> Dict[str, Any]:
        self.out.mkdir(parents=True, exist_ok=True)
        image = None
        quiesce: Dict[str, Any] = {}
        run: Dict[str, Any] = {"end_state": "infrastructure_error", "transcript": [], "controller": None}
        try:
            with self.timing.measure("setup"):
                self.agent.start()
            self.budget.start()
            run = self._single_turn() if self.condition == "single_turn" else self._loop()
            with self.timing.measure("agent_quiesce"):
                quiesce = self.agent.quiesce()
            with self.timing.measure("snapshot"):
                image = self.agent.freeze()
        except Exception as exc:  # noqa: BLE001 - environment failures are infrastructure errors
            self.errors.append(f"environment: {type(exc).__name__}: {exc}")
            run["end_state"] = "infrastructure_error"
            if run.get("controller") is not None:
                run["controller"].end_state = "infrastructure_error"
        finally:
            # The timed episode ends at the frozen snapshot, before teardown.
            budget_used = self.budget.to_json()
            expired = self.budget.remaining() <= 0
            try:
                self.agent.stop()
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"cleanup: {exc}")
        if run["end_state"] == "done" and expired:
            run["end_state"] = "time_limit_exceeded"
            if run.get("controller") is not None:
                run["controller"].end_state = "time_limit_exceeded"
        original: Dict[str, Any] = {"status": "unknown", "reward": None, "reason_code": "not_run"}
        checker: Optional[Dict[str, Any]] = None
        if image is not None:
            with self.timing.measure("evaluation"):
                if hasattr(self.evaluator, "export_frozen"):
                    try:
                        self.frozen = self.evaluator.export_frozen(image, self.out / "agent-private" / "frozen")
                    except Exception as exc:  # noqa: BLE001
                        self.errors.append(f"export_frozen: {exc}")
                try:
                    original = self.evaluator.original(image)
                except Exception as exc:  # noqa: BLE001
                    self.errors.append(f"original_verifier: {exc}")
                if self.condition not in ("single_turn", "vague_no_evolution"):
                    controller = run.get("controller")
                    disclosed = {item.requirement_id: item.disclosed_turn
                                 for item in (controller.requirements if controller else [])
                                 if item.disclosed_turn is not None}
                    try:
                        checker = self.evaluator.persona_checker(
                            image, run["transcript"], self.task.requirements_spec, disclosed)
                    except Exception as exc:  # noqa: BLE001
                        self.errors.append(f"persona_checker: {exc}")
                        checker = {"requirements": {item["requirement_id"]: {
                            "status": "unknown", "reason": "checker_error"}
                            for item in self.task.requirements_spec}}
        return self._write_results(run, original, checker, budget_used, quiesce)

    def _write_results(self, run, original, checker, budget_used, quiesce) -> Dict[str, Any]:
        controller: Optional[Controller] = run.get("controller")
        outcome = benchmark_outcome(self.condition, run["end_state"], original, checker)
        if run['end_state'] == 'api_interrupted':
            from swe_persona.api_recovery import assess_interrupted
            frozen = self.out / 'agent-private' / 'frozen' / 'final.patch'
            progress = frozen.exists() and bool(frozen.read_text().strip()) or any(
                item.get('role') == 'agent' and item.get('text','').strip() for item in run['transcript'])
            statuses = [item.get('status') for item in (checker or {}).get('requirements',{}).values()]
            persona = False if 'fail' in statuses else True if statuses and all(s == 'pass' for s in statuses) else None
            if self.condition == 'single_turn': persona = True
            native = original['status'] == 'pass' if original.get('status') in ('pass','fail') else None
            assessment = assess_interrupted(native,persona,artifacts_available=(self.frozen or {}).get('ok') is True,
                                            progress=progress,budget_exhausted=budget_used.get('used_sec',0)>=budget_used.get('timeout_sec',float('inf')))
            outcome = dict(assessment,outcome=assessment['status'])
        stalled = (quiesce or {}).get("seconds_since_progress")
        if run["end_state"] == "time_limit_exceeded" and stalled is not None and stalled > STALL_LIMIT_SEC:
            outcome = {"outcome": "infrastructure_error", "reason": f"agent_no_progress_for_{int(stalled)}s"}
        online = controller.state() if controller else {"condition": self.condition,
                                                        "end_state": run["end_state"]}
        result = {
            "protocol": PROTOCOL_VERSION,
            "runtime": RUNTIME_VERSION,
            "task_id": self.task.task_id,
            "condition": self.condition,
            "trial_name": self.out.name,
            "models": self.models,
            "requirement_judgment": online,
            "original_reward": original.get("reward"),
            "original": original,
            "checker": checker,
            "benchmark_outcome": outcome["outcome"],
            "api_recovery_assessment": outcome if run['end_state'] == 'api_interrupted' else None,
            "outcome_reason": outcome["reason"],
            "budget": budget_used,
            "timing_sec": self.timing.seconds,
            "frozen_artifacts": self.frozen,
            "errors": self.errors,
            "note": ("online MET is user-perceived satisfaction from visible evidence; it does not "
                     "override the original verifier or the persona checker"),
        }
        _write(self.out / "persona-result.json", result)
        if checker is not None:
            _write(self.out / "persona-check.json", checker)
        _write(self.out / "agent-private" / "episode.json", {
            "protocol": PROTOCOL_VERSION,
            "task_id": self.task.task_id,
            "condition": self.condition,
            "transcript": run["transcript"],
            "events": controller.events if controller else [],
            "state": online,
            "simulator_decisions": self.decisions,
            "judgments": self.judgments,
            "budget": budget_used,
            "quiesce": quiesce,
        })
        return result
