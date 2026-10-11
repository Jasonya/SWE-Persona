"""Bounded single-turn model baseline, followed by isolated official evaluation.

This is NOT a persona run. Reuses the team's TerminalAgent and Azure Llm.
No host directories or credentials are mounted in the agent container.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from task_adapter import ROOT, digest, public_view, read_task
from patch_export import export_prediction, untracked_fingerprints

sys.path.insert(0, str(ROOT / "tb2_persona_pilot"))
from agent import TerminalAgent
from llm import CallLog, Llm, load_local_env


class BudgetExhausted(RuntimeError):
    pass


class BoundedLlm:
    def __init__(self, client, calls, seconds):
        self.client, self.limit, self.calls = client, calls, 0
        self.deadline = time.monotonic() + seconds

    def text(self, messages, **kwargs):
        if self.calls >= self.limit or time.monotonic() >= self.deadline:
            raise BudgetExhausted("Model call or rollout wall-time budget exhausted")
        self.calls += 1
        print(f"[model] call {self.calls}/{self.limit} ({kwargs.get('role', 'agent')})", flush=True)
        # Do not let nested transport retries multiply the pilot's call budget.
        return self.client.text(messages, attempts=1, **kwargs)


class AgentContainer:
    def __init__(self, docker_client, image):
        self.container = docker_client.containers.run(
            image, command=["sleep", "infinity"], detach=True,
            name="persona-multilang-" + uuid.uuid4().hex[:12],
            network_mode="none", mem_limit="16g", nano_cpus=4_000_000_000,
            working_dir="/testbed",
        )
        self.commands = []

    def sh(self, script, timeout=120, workdir="/testbed"):
        # GNU timeout is verified before the model starts; unlike a host timeout,
        # it kills the command inside the container instead of orphaning it.
        seconds = max(1, int(timeout))
        print(f"[shell] command {len(self.commands) + 1}, timeout={seconds}s", flush=True)
        result = self.container.exec_run(
            ["timeout", "--kill-after=5s", f"{seconds}s", "bash", "-lc", script],
            workdir=workdir, demux=True,
        )
        stdout, stderr = result.output
        out, err = (stdout or b"").decode("utf-8", "replace"), (stderr or b"").decode("utf-8", "replace")
        self.commands.append({"command": script, "exit_code": result.exit_code,
                              "stdout": out, "stderr": err})
        print(f"[shell] exit={result.exit_code}", flush=True)
        return result.exit_code, out, err

    def remove(self):
        self.container.remove(force=True)


def save(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--task", type=Path, default=ROOT / ".local/multilang-smoke/task.private.jsonl")
    p.add_argument("--max-calls", type=int, default=24)
    p.add_argument("--max-steps", type=int, default=20)
    p.add_argument("--command-timeout", type=int, default=120)
    p.add_argument("--rollout-seconds", type=int, default=1800)
    p.add_argument("--check-only", action="store_true")
    p.add_argument("--skip-evaluation", action="store_true")
    args = p.parse_args()
    if min(args.max_calls, args.max_steps, args.command_timeout, args.rollout_seconds) <= 0:
        p.error("All budgets must be positive")
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    for directory in (Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Docker/Docker/resources/bin",
                      Path(os.environ.get("LOCALAPPDATA", ".")) / "Programs/DockerDesktop/resources/bin"):
        if (directory / "docker-credential-desktop.exe").exists():
            os.environ["PATH"] = str(directory) + os.pathsep + os.environ["PATH"]
            break
    load_local_env()
    task = read_task(args.task)
    import docker
    client = docker.from_env(timeout=180)
    try:
        if client.info()["OSType"] != "linux":
            raise RuntimeError("Docker must use Linux containers")
        key_ready = bool(os.environ.get("AZURE_OPENAI_API_KEY", "").strip())
        print(json.dumps({"docker": "linux", "instance_id": task["instance_id"],
                          "model": "gpt-6-astra", "azure_key_configured": key_ready}))
        if not key_ready:
            print("Add AZURE_OPENAI_API_KEY to D:\\PersonaCode\\.env; no model call was made.")
            return 2
        if args.check_only:
            return 0
        # Credentials are checked before allocating a container or starting a run.
        run = ROOT / ".local/multilang-pilot/runs" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
        run.mkdir(parents=True)
        print("Run directory:", run, flush=True)
        call_log = CallLog(path=run / "llm_calls.jsonl")
        llm = Llm(model="gpt-6-astra", call_log=call_log)
        llm._client.timeout = min(180, args.rollout_seconds)
        bounded = BoundedLlm(llm, args.max_calls, args.rollout_seconds)
        public = public_view(task)
        save(run / "task.public.json", public)
        summary = {"arm": "single_turn", "persona": None, "model": "gpt-6-astra",
                   "public_contract_digest": digest(public), "max_calls": args.max_calls,
                   "max_steps": args.max_steps, "rollout_seconds": args.rollout_seconds,
                   "status": "running", "base_task_success": None}
        save(run / "summary.json", summary)
        # Save private evaluator input outside the agent container; never pass it
        # to TerminalAgent or to the provider.
        (run / "task.private.jsonl").write_text(json.dumps(task) + "\n", encoding="utf-8")
        runtime = None
        prediction_ready = False
        try:
            image = client.images.get(task["docker_image"])
            summary["image_id"] = image.id
            summary["image_digests"] = image.attrs.get("RepoDigests", [])
            runtime = AgentContainer(client, image.id)
            check = runtime.container.exec_run(["bash", "-lc", "command -v timeout && git rev-parse HEAD"], workdir="/testbed")
            output = check.output.decode("utf-8", "replace").strip().splitlines()
            if check.exit_code != 0 or not output or output[-1] != task["base_commit"]:
                raise RuntimeError("Image base commit or command-timeout tool does not match the frozen task")
            initial_untracked = untracked_fingerprints(runtime)
            save(run / "initial_untracked.json", initial_untracked)
            agent = TerminalAgent(bounded, runtime, environment_card=json.dumps(public["environment"]),
                                  max_steps_per_turn=args.max_steps, workdir="/testbed",
                                  command_timeout=args.command_timeout)
            try:
                turn = agent.run_turn(task["problem_statement"], turn=1, step_budget=args.max_steps)
                save(run / "agent_turn.json", turn.to_json())
                summary["agent_claim"] = turn.status
                summary["status"] = "rollout_finished"
            except BudgetExhausted:
                summary["status"] = "budget_exhausted"
            finally:
                save(run / "agent_history.json", agent.history)
            # Include new files as well as tracked edits without committing them.
            patch = export_prediction(runtime, task["base_commit"], initial_untracked)
            save(run / "predictions.json", {task["instance_id"]: {"model_patch": patch}})
            prediction_ready = True
        except Exception as exc:
            summary["status"] = "infrastructure_error"
            summary["error_type"] = type(exc).__name__
            raise
        finally:
            if runtime is not None:
                save(run / "commands.json", runtime.commands)
                runtime.remove()
            summary["usage"] = call_log.totals()
            save(run / "summary.json", summary)
        if prediction_ready and not args.skip_evaluation:
            # A fresh instance evaluates the frozen patch. Nothing is fed back
            # into the completed rollout.
            evaluation_dir = run / "evaluation"
            print("[evaluation] Running official tests; log:", run / "evaluation.log", flush=True)
            command = [sys.executable, str(Path(__file__).with_name("run_evaluation.py")),
                       "--dataset", str(run / "task.private.jsonl"), "--patch_dir", str(run / "predictions.json"),
                       "--platform", "linux", "--workers", "1", "--overwrite", "0",
                       "--output_dir", str(evaluation_dir)]
            with (run / "evaluation.log").open("w", encoding="utf-8") as log:
                result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
            report_path = evaluation_dir / "results.json"
            report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
            valid = result.returncode == 0 and report.get("submitted") == 1 and not report.get("error", 0) and not report.get("incomplete", 0)
            summary["evaluation_valid"] = valid
            summary["base_task_success"] = (report.get("success") == 1) if valid else None
            save(run / "summary.json", summary)
        print("Run results:", run)
        print(json.dumps(summary, indent=2))
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
