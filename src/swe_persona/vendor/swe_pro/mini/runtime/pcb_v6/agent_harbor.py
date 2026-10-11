"""Host mini-swe-agent with a Harbor-managed, network-isolated task container.

Python, model requests, credentials and trajectories remain on the host. Shell
commands alone cross the Docker boundary. This is a new execution profile;
historical in-container-agent scores are retained separately.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from .episode import AgentFailure, AgentReply, AgentTimeout
from .network_probe import probe_kernel

BRIDGE = Path(__file__).resolve().parent / "bridge"
INTERACTIVE_NOTE = (
    "\nUse ask_user with one quoted argument to ask the requester a question. "
    "Only that argument or text AFTER COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT reaches the requester. "
    "THOUGHT and shell output are not delivered. Include the complete handoff in submission text. "
    "Submit in a separate action, with no preceding shell output: printf '%s\\n' "
    "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT 'complete handoff'. A bare marker is insufficient. "
    "A submission yields for review; later feedback can require more edits in the same workspace. "
    "When you have finished what the latest user message asks, even if nothing needed changing, submit your "
    "reply right away; placeholder commands such as `true` are never delivered to the user. "
    "Preparing patches, tests and isolated preview/undo checks is authorized. Ask confirmation only "
    "for restricted actions.")


def litellm_model(name: str) -> str:
    return name if "/" in name else "openai/" + name


class HarborMiniAgent:
    def __init__(self, *, image: str, out_dir: Path, model: str, api_base: str, api_key: str,
                 agent_brief: str, mini_venv: Path, cpus: int = 2, memory_mb: int = 16384) -> None:
        self.image = image
        self.out = Path(out_dir)
        self.trial = self.out / "harbor"
        self.model = litellm_model(model)
        self.api_base = api_base
        self.api_key = api_key
        self.agent_brief = agent_brief
        self.mini_venv = Path(mini_venv).resolve()
        self.cpus, self.memory_mb = cpus, memory_mb
        self.session = "pcbv6-" + uuid.uuid4().hex[:12]
        self.pid_file = f"/tmp/pcb-agent-{self.session}.pid"
        self.container: Optional[str] = None
        self.loop = asyncio.new_event_loop()
        self.env = None
        self.frozen: Optional[str] = None

    # ---- Harbor objects ----------------------------------------------------------------
    def _compose(self) -> Path:
        envdir = self.trial / "environment"
        envdir.mkdir(parents=True, exist_ok=True)
        # The agent and its Python interpreter run on the host. Nothing from the
        # evaluator environment (including credentials) is mounted into the task.
        volumes = []
        compose = {"services": {"main": {
            "image": self.image, "entrypoint": ["/bin/bash", "-c"], "command": ["sleep infinity"],
            "working_dir": "/app",
            "volumes": volumes}}}
        (envdir / "docker-compose.yaml").write_text(yaml.safe_dump(compose))
        return envdir

    def _environment(self, envdir: Path):
        from harbor.environments.docker.docker import DockerEnvironment
        from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
        from harbor.models.trial.paths import TrialPaths

        class ProbedDockerEnvironment(DockerEnvironment):
            def _egress_control_kernel_support(inner):
                return probe_kernel(DockerEnvironment, self.trial)

        paths = TrialPaths(self.trial)
        paths.mkdir()
        policy = NetworkPolicy(network_mode=NetworkMode.NO_NETWORK)
        env = ProbedDockerEnvironment(
            environment_dir=envdir, environment_name=self.session, session_id=self.session,
            trial_paths=paths, task_env_config=EnvironmentConfig(docker_image=self.image, cpus=self.cpus,
                                                                 memory_mb=self.memory_mb),
            network_policy=policy, phase_network_policies=[policy])
        env._use_prebuilt = True
        return env

    # ---- session ---------------------------------------------------------------------
    def start(self) -> None:
        envdir = self._compose()
        self.env = self._environment(envdir)
        self.loop.run_until_complete(self.env.start(force_build=False))
        names = subprocess.check_output(
            ["docker", "ps", "--filter", f"label=com.docker.compose.project={self.session}",
             "--filter", "label=com.docker.compose.service=main", "--format", "{{.Names}}"], text=True).split()
        if len(names) != 1:
            raise RuntimeError(f"cannot identify the Harbor main container: {names}")
        self.container = names[0]

    def turn(self, message: str, turn: int, timeout_sec: float, interactive: bool) -> AgentReply:
        directory = self.trial / "agent"
        directory.mkdir(parents=True, exist_ok=True)
        request = {"timeout": max(0.001, timeout_sec), "pid_file": self.pid_file,
                   "interactive": interactive, "turn": turn, "user_message": message,
                   "directory": str(directory.resolve()), "container": self.container,
                   "model": self.model, "api_base": self.api_base,
                   "task_brief": self.agent_brief, "interactive_note": INTERACTIVE_NOTE}
        request_path = directory / "request.json"
        request_path.write_text(json.dumps(request))
        environment = dict(os.environ, OPENAI_API_KEY=self.api_key,
                           LITELLM_LOCAL_MODEL_COST_MAP="True")
        command = [str(self.mini_venv / "bin/python"), str(BRIDGE / "mini_sidecar.py"),
                   "--request", str(request_path)]
        with (directory / f"sidecar-{turn}.log").open("w") as log:
            proc = subprocess.Popen(command, env=environment, stdout=log, stderr=log,
                                    start_new_session=True)
            try:
                proc.wait(timeout=timeout_sec)
            except subprocess.TimeoutExpired as exc:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                self.quiesce()
                raise AgentTimeout(f"agent turn {turn} exceeded its remaining budget") from exc
            except BaseException:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                raise
        if proc.returncode:
            raise AgentFailure(f"mini sidecar exited {proc.returncode}; inspect sidecar-{turn}.log")
        payload_path = directory / f"turn-{turn}.json"
        if not payload_path.is_file():
            raise AgentFailure(f"agent turn {turn} produced no reply")
        payload = json.loads(payload_path.read_text())
        (self.out / "agent").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(payload_path, self.out / "agent" / f"turn-{turn}.json")
        shutil.copyfile(directory / "trajectory.json", self.out / "agent" / "trajectory.json")
        return AgentReply(text=payload["reply"], status=payload["status"],
                          steps=len(payload.get("executions", [])))

    def quiesce(self) -> Dict[str, Any]:
        if not self.container:
            return {"stopped": False}
        script = ("for f in " + shlex.quote(self.pid_file) + " " + shlex.quote(self.pid_file + "-runner")
                  + '; do if test -f "$f"; then p=$(cat "$f"); kill -KILL -- -"$p" 2>/dev/null || true; rm -f "$f"; fi; done')
        done = subprocess.run(["docker", "exec", self.container, "bash", "-c", script], capture_output=True,
                              text=True, timeout=60)
        out = {"stopped": done.returncode == 0}
        # mini saves its trajectory after every step: a long gap means the agent was not working
        # (e.g. a model request that kept timing out), which the episode reports as infrastructure.
        trajectory = self.trial / "agent" / "trajectory.json"
        if trajectory.is_file():
            out["seconds_since_progress"] = round(time.time() - trajectory.stat().st_mtime, 1)
        return out

    def freeze(self) -> str:
        tag = f"pcb-v6-final-{self.session}:latest"
        # `docker commit` copies the container's labels; with the Compose project label left in
        # place, Harbor's `compose down --rmi local` deletes the snapshot before evaluation.
        relabel = ["--change", "LABEL com.docker.compose.project=pcb-v6-frozen "
                   "com.docker.compose.service=pcb-v6-frozen"]
        subprocess.run(["docker", "commit", *relabel, self.container, tag], check=True, capture_output=True,
                       text=True, timeout=1800)
        self.frozen = tag
        return tag

    def stop(self) -> None:
        if self.env is not None:
            self.loop.run_until_complete(self.env.stop(delete=True))
        self.loop.close()

    def discard_frozen(self) -> None:
        if self.frozen:
            subprocess.run(["docker", "rmi", "-f", self.frozen], capture_output=True, timeout=600)
