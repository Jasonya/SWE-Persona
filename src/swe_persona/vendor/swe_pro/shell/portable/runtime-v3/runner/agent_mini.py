"""mini-swe-agent 2.4.6 using the recipient model and container broker."""
from __future__ import annotations
import json
import hashlib
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
import yaml
from minisweagent.agents.default import DefaultAgent
from minisweagent.exceptions import FormatError, InterruptAgentFlow, LimitsExceeded, Submitted
from minisweagent.models.litellm_response_model import LitellmResponseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tb2_persona_pilot.agent import AgentTurn, Step

HARNESS_PROTOCOL_VERSION = "mini-swe-agent-2.4.6-responses-configurable-v5"
ROOT = Path(__file__).resolve().parents[3]
CONFIG = Path(os.environ.get("PCB_MINI_CONFIG", Path(__import__("minisweagent").__file__).parent / "config/benchmarks/swebench.yaml"))


class HardTimeoutLitellmResponseModel(LitellmResponseModel):
    """Enforce a process-level deadline around Responses API calls.

    LiteLLM's ``timeout`` is still passed to the provider, but some stalled
    Responses requests have remained blocked beyond it.  Calibration and batch
    workers call the model from their main thread on Linux, so SIGALRM gives us
    a real upper bound that the provider SDK cannot silently extend.
    """

    def _query(self, messages, **kwargs):
        seconds = float(os.environ.get("PCB_AGENT_API_HARD_TIMEOUT_SECONDS", "330"))
        if seconds <= 0:
            return super()._query(messages, **kwargs)

        previous_handler = signal.getsignal(signal.SIGALRM)

        def expire(_signum, _frame):
            raise TimeoutError(f"Responses API call exceeded hard timeout ({seconds:g}s)")

        signal.signal(signal.SIGALRM, expire)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            return super()._query(messages, **kwargs)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)


class DockerxMiniEnvironment:
    def __init__(self, container, *, cwd="/app", timeout=900.0,
                 environment_card="", max_output_chars=3000):
        self.container = container
        self.config = SimpleNamespace(cwd=cwd, timeout=timeout)
        self.environment_card = environment_card
        self.max_output_chars = max_output_chars
        self.exec_log = []
        self.host_exec_log = []

    def execute(self, action, cwd=""):
        command = action.get("command", "")
        try:
            rc, out, err = self.container.sh(
                command, timeout=self.config.timeout, workdir=cwd or self.config.cwd
            )
            combined = (out or "") + (("\n[stderr]\n" + err) if err else "")
            full_output = combined
            submission_lines = combined.lstrip().splitlines(keepends=True)
            if len(combined) > self.max_output_chars:
                half = self.max_output_chars // 2
                omitted = len(combined) - self.max_output_chars
                combined = combined[:half] + f"\n...[{omitted} chars trimmed]...\n" + combined[-half:]
            output = {"returncode": rc, "output": combined, "exception_info": ""}
        except Exception as exc:
            output = {"returncode": 1, "output": "", "exception_info": repr(exc)}
            full_output = ""
            submission_lines = []
        self.exec_log.append((command, output))
        self.host_exec_log.append((command, dict(output, output=full_output)))
        if submission_lines and submission_lines[0].strip() == "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" \
                and output["returncode"] == 0:
            submission = "".join(submission_lines[1:])
            raise Submitted({
                "role": "exit", "content": submission,
                "extra": {"exit_status": "Submitted", "submission": submission},
            })
        return output

    def get_template_vars(self, **kwargs):
        return {"cwd": self.config.cwd, **kwargs}

    def serialize(self):
        return {"environment": {
            "type": "dockerx-apptainer", "cwd": self.config.cwd,
            "timeout": self.config.timeout,
            "environment_card_sha256": hashlib.sha256(
                self.environment_card.encode("utf-8")
            ).hexdigest(),
        }}


class MultiTurnDefaultAgent(DefaultAgent):
    """Continue an official DefaultAgent trajectory after a user follow-up.

    DefaultAgent.run() intentionally resets the message list, because the upstream
    benchmark is single-turn.  Persona episodes need the same agent history and
    working container to survive across user turns.  This method preserves both,
    removes only the prior terminal marker, appends the new user message, and then
    uses the upstream step/error semantics until the agent exits again.
    """

    def begin_turn(self, turn: int, step_budget: int) -> None:
        if step_budget < 0:
            raise ValueError("step_budget must be non-negative")
        self._active_turn = turn
        self._turn_start_calls = self.n_calls
        self._turn_call_limit = self.n_calls + step_budget
        if not hasattr(self, "_seen_format_responses"):
            self._seen_format_responses = set()

    def query(self) -> dict:
        if self.n_calls >= getattr(self, "_turn_call_limit", self.config.step_limit):
            raise LimitsExceeded({
                "role": "exit", "content": "TurnStepLimitExceeded",
                "extra": {"exit_status": "TurnStepLimitExceeded", "submission": ""},
            })
        return super().query()

    def serialize(self, *extra_dicts) -> dict:
        turn_limit = getattr(self, "_turn_call_limit", self.n_calls)
        turn_start = getattr(self, "_turn_start_calls", self.n_calls)
        return super().serialize({
            "info": {"multiturn": {
                "active_turn": getattr(self, "_active_turn", None),
                "turn_step_budget": max(0, turn_limit - turn_start),
                "turn_steps_used": max(0, self.n_calls - turn_start),
                "global_model_calls": self.n_calls,
            }}
        }, *extra_dicts)

    @staticmethod
    def _response_text(response: object) -> str:
        """Return natural assistant text persisted on a parser FormatError."""
        if not isinstance(response, dict):
            return ""
        chunks = []
        for item in response.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content") or []:
                if not isinstance(content, dict):
                    continue
                if content.get("type") in {"output_text", "text"} and content.get("text"):
                    chunks.append(str(content["text"]))
        return "\n".join(chunks).strip()

    def _turn_boundary_from_format_error(self, exc: FormatError) -> dict | None:
        """Convert a text-only model response into a resumable dialogue boundary.

        mini-swe-agent correctly rejects text-only responses for its native
        single-turn benchmark.  In an interactive episode that same response is
        the assistant's user-facing message.  It must be retained in model
        history and exposed to the simulator, without adding the parser's
        synthetic error prompt or pretending the whole benchmark was submitted.
        """
        error = exc.messages[0] if exc.messages else {}
        extra = error.get("extra", {}) if isinstance(error, dict) else {}
        response = extra.get("response")
        text = self._response_text(response)
        # A recovery belongs to the model call that just failed in this turn.
        # Reject a response object reused by a later FormatError (the old bridge
        # could accidentally surface an earlier turn's text).
        response_key = hashlib.sha256(
            json.dumps(response, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        seen = getattr(self, "_seen_format_responses", set())
        current_call = getattr(self, "n_calls", 0) > getattr(self, "_turn_start_calls", -1)
        if not text or not current_call or response_key in seen:
            return None
        seen.add(response_key)
        self._seen_format_responses = seen
        self.cost += extra.get("cost", 0.0)
        preserved = dict(response)
        preserved["extra"] = {
            **(preserved.get("extra") or {}),
            "cost": extra.get("cost", 0.0),
            "multiturn_boundary": True,
            "turn": getattr(self, "_active_turn", None),
            "model_call": getattr(self, "n_calls", 0),
        }
        self.add_messages(
            preserved,
            {"role": "exit", "content": "TurnComplete",
             "extra": {"exit_status": "TurnComplete", "submission": text}},
        )
        self.save(self.config.output_path)
        return self.messages[-1]["extra"]

    def _run_until_boundary(self) -> dict:
        while True:
            try:
                self.step()
                self.n_consecutive_format_errors = 0
            except FormatError as exc:
                boundary = self._turn_boundary_from_format_error(exc)
                if boundary is not None:
                    return boundary
                self.cost += exc.messages[0].get("extra", {}).get("cost", 0.0)
                self.n_consecutive_format_errors += 1
                if (0 < self.config.max_consecutive_format_errors
                        <= self.n_consecutive_format_errors):
                    self.add_messages(
                        *exc.messages,
                        {"role": "exit", "content": "RepeatedFormatError",
                         "extra": {"exit_status": "RepeatedFormatError", "submission": ""}},
                    )
                else:
                    self.add_messages(*exc.messages)
            except Submitted as exc:
                # Upstream submission exits before formatting the bash result.
                # A later persona turn may defer acceptance and continue this
                # trajectory, so close every function call before the exit.
                self._complete_pending_tool_calls(
                    "Submission received; acceptance is deferred to the interactive controller."
                )
                self.add_messages(*exc.messages)
            except InterruptAgentFlow as exc:
                self.add_messages(*exc.messages)
            except Exception as exc:
                self.handle_uncaught_exception(exc)
                raise
            finally:
                self.save(self.config.output_path)
            if self.messages[-1].get("role") == "exit":
                return self.messages[-1].get("extra", {})

    def _complete_pending_tool_calls(self, output: str) -> None:
        completed = {message.get("call_id") for message in self.messages
                     if message.get("type") == "function_call_output"}
        pending = []
        for message in self.messages:
            if message.get("object") != "response":
                continue
            for item in message.get("output") or []:
                if (isinstance(item, dict) and item.get("type") == "function_call"
                        and item.get("call_id") not in completed):
                    pending.append(item["call_id"])
        for call_id in pending:
            self.add_messages({"type": "function_call_output", "call_id": call_id,
                               "output": output, "extra": {"multiturn_submission_boundary": True}})

    def run(self, task: str = "", **kwargs) -> dict:
        self.extra_template_vars |= {"task": task, **kwargs}
        self.messages = []
        self.add_messages(
            self.model.format_message(role="system", content=self._render_template(self.config.system_template)),
            self.model.format_message(role="user", content=self._render_template(self.config.instance_template)),
        )
        return self._run_until_boundary()

    def continue_with(self, user_message: str) -> dict:
        if not self.messages or self.messages[-1].get("role") != "exit":
            raise RuntimeError("mini-swe-agent continuation requires a terminal prior turn")
        self.messages.pop()
        self.n_consecutive_format_errors = 0
        self.add_messages(self.model.format_message(role="user", content=user_message))
        return self._run_until_boundary()


class TerminalAgent:
    def __init__(self, llm, container, environment_card, max_steps_per_turn=14,
                 max_output_chars=3000, workdir="/app", command_timeout=900.0):
        self.container = container
        self.llm = llm
        self.run_dir = Path(os.environ["PCB_RUN_DIR"])
        self.agent = None
        self.environment = None
        self.delivery_receipts = []
        self.environment_card = environment_card
        self.max_steps = int(max_steps_per_turn)
        self.max_output_chars = int(max_output_chars)
        self.workdir = workdir
        self.command_timeout = float(command_timeout)

    @staticmethod
    def _sha(text):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _receipt(self, turn, user_message):
        messages = self.agent.messages
        candidates = []
        for index, message in enumerate(messages):
            if message.get("role") != "user":
                continue
            content = str(message.get("content") or "")
            if content == user_message or (turn == 1 and user_message in content):
                candidates.append((index, content))
        if not candidates:
            raise RuntimeError("user input is absent from saved mini trajectory")
        index, content = candidates[-1]
        return {
            "receipt_version": "mini-trajectory-input-v1",
            "turn": turn,
            "text": user_message,
            "message_index": index,
            "message_sha256": self._sha(user_message),
            "trajectory_content_sha256": self._sha(content),
            "match": "exact" if content == user_message else "embedded_instance_template",
        }

    def run_turn(self, user_message, turn, step_budget=None):
        result = AgentTurn(turn=turn, message_id=f"a-{turn}", reply="", status="working")
        result.turn_end_reason = "reply"
        result.budget_rejections = 0
        if self.agent is None:
            cfg = yaml.safe_load(CONFIG.read_text())
            spec = self.llm.spec
            if spec.provider != "azure_responses":
                raise ValueError("The mini adapter requires a Responses-compatible model; "
                                 "use --adapter portable_shell:ShellAgent for Chat Completions")
            key = os.environ.get(spec.api_key_env)
            if not key:
                raise RuntimeError(f"{spec.api_key_env} is not set")
            self.run_dir.mkdir(parents=True, exist_ok=True)
            (self.run_dir / "agent-model.json").write_text(json.dumps({
                "requested_model": self.llm.model, "effective_model": spec.name,
                "base_url": spec.base_url, "provider": spec.provider,
                "api_key_env": spec.api_key_env, "adapter": HARNESS_PROTOCOL_VERSION,
            }, indent=2))
            os.environ["OPENAI_API_KEY"] = key
            self.environment = DockerxMiniEnvironment(
                self.container, cwd=self.workdir, timeout=self.command_timeout,
                environment_card=self.environment_card,
                max_output_chars=self.max_output_chars,
            )
            model_cfg = cfg["model"]
            model = HardTimeoutLitellmResponseModel(
                model_name="openai/" + spec.name.removeprefix("openai/"),
                # Keep retry ownership in mini-swe-agent's bounded outer loop.
                # LiteLLM otherwise retries inside each outer attempt and can
                # multiply a 300-second timeout into an effectively unbounded
                # turn from the scheduler's point of view.
                model_kwargs={"api_base": spec.base_url, "timeout": 300, "num_retries": 0,
                              **({"extra_body": spec.extra_body} if spec.extra_body else {}),
                              **({"extra_headers": spec.default_headers} if spec.default_headers else {})},
                cost_tracking="ignore_errors",
                observation_template=model_cfg["observation_template"],
                format_error_template=model_cfg["format_error_template"],
            )
            agent_cfg = dict(cfg["agent"])
            agent_cfg["instance_template"] = agent_cfg["instance_template"].replace("/testbed", "/app")
            agent_cfg["system_template"] += (
                "\n\nInteractive harness limits:\n"
                f"- Working directory: {self.workdir}\n"
                f"- Per-turn model-step limit: {self.max_steps}\n"
                f"- Shell command timeout: {self.command_timeout:g} seconds\n"
                "- A submission command is provisional in this interactive harness. If the user follows up, "
                "you may continue inspecting and testing the same working tree, then submit the updated final patch.\n"
                "- Environment facts:\n" + self.environment_card
            )
            # The upstream global limit is disabled; begin_turn installs the
            # strict limit for each interactive turn.
            agent_cfg["step_limit"] = 0
            agent_cfg["cost_limit"] = 0
            agent_cfg["wall_time_limit_seconds"] = 7200
            agent_cfg["output_path"] = self.run_dir / "minisweagent" / "trajectory.json"
            self.agent = MultiTurnDefaultAgent(model, self.environment, **agent_cfg)
            self.agent.extra_template_vars["environment_card"] = self.environment_card
            budget = self.max_steps if step_budget is None else int(step_budget)
            self.agent.begin_turn(turn, budget)
            start_log = 0
            outcome = self.agent.run(user_message)
        else:
            budget = self.max_steps if step_budget is None else int(step_budget)
            self.agent.begin_turn(turn, budget)
            start_log = len(self.environment.exec_log)
            outcome = self.agent.continue_with(user_message)
        trajectory_path = self.run_dir / "minisweagent" / "trajectory.json"
        if trajectory_path.is_file():
            data = json.loads(trajectory_path.read_text())
            model_kwargs = data["info"]["config"]["model"].get("model_kwargs", {})
            if "api_key" in model_kwargs:
                model_kwargs["api_key"] = "[REDACTED]"
                trajectory_path.write_text(json.dumps(data, indent=2) + "\n")
        for index, (command, output) in enumerate(self.environment.host_exec_log[start_log:], 1):
            result.steps.append(Step(index=index, command=command,
                                     exit_code=int(output["returncode"]),
                                     stdout=str(output["output"]), thought=""))
        exit_status = str(outcome.get("exit_status") or "")
        result.reply = str(outcome.get("submission") or exit_status or "mini-swe-agent completed")
        # RepeatedFormatError is an upstream terminal agent failure after the
        # configured retry budget, not a successful completion claim.  Keeping
        # it distinct prevents the controller from repeatedly asking an agent
        # that has already terminated to repair the same response.
        if exit_status == "RepeatedFormatError":
            result.status = "format_error"
        elif exit_status == "TurnComplete":
            result.status = "working"
        elif exit_status in {"TurnStepLimitExceeded", "LimitsExceeded", "TimeExceeded"}:
            result.status = "budget_exhausted"
        elif exit_status == "Submitted":
            result.status = "done"
        else:
            result.status = "format_error"
        result.turn_end_reason = {
            "working": "assistant_reply",
            "done": "submitted",
            "budget_exhausted": "step_budget_exhausted",
            "format_error": "format_error",
        }[result.status]
        # This is independent evidence pointing into the persisted mini
        # trajectory, rather than a synthetic provider CallLog entry.
        self.delivery_receipts.append(self._receipt(turn, user_message))
        return result
