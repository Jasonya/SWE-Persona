"""Objective evaluation of a frozen final snapshot, independent of the conversation.

  original verifier  the task's own `tests/test.sh` (Harbor's verifier contract), which
                     applies the hidden test patch and runs the official SWE-bench Pro
                     runner; reward comes from /logs/verifier/reward.txt
  persona checker    `tests/persona/checker.py` on the same snapshot, one verdict per
                     requirement

Both run in fresh containers with no network.  Their results are written to the trial
directory and never reach the simulated user.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List


def _docker_run(image: str, mounts: List[str], command: str, timeout: float):
    cmd = ["docker", "run", "--rm", "--network", "none", "--cpus", "2", "--memory", "16g",
           "--entrypoint", "/bin/bash"]
    for mount in mounts:
        cmd += ["-v", mount]
    cmd += [image, "-c", command]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")


class DockerEvaluator:
    def __init__(self, task_root: Path, work_dir: Path, verifier_timeout: float = 4200.0) -> None:
        self.task_root = Path(task_root).resolve()
        self.work = Path(work_dir).resolve()
        self.verifier_timeout = verifier_timeout

    def original(self, image: str) -> Dict[str, Any]:
        logs = self.work / "verifier"
        logs.mkdir(parents=True, exist_ok=True)
        tests = self.task_root / "tests"
        # The persona rules are never visible to the original verifier run either.
        proc = _docker_run(image, [f"{tests}:/tests:ro", f"{logs}:/logs/verifier"],
                           "bash /tests/test.sh", self.verifier_timeout)
        (logs / "test-stdout.txt").write_text(proc.stdout[-200000:])
        (logs / "test-stderr.txt").write_text(proc.stderr[-200000:])
        check_path, reward_path = logs / "check.json", logs / "reward.txt"
        if not check_path.is_file():
            return {"status": "unknown", "reward": None, "reason_code": "verifier_produced_no_result"}
        check = json.loads(check_path.read_text())
        reward = float(reward_path.read_text().strip()) if reward_path.is_file() else None
        return {"status": check.get("status", "unknown"), "reward": reward,
                "reason_code": check.get("reason_code"),
                "summary": check.get("evidence", {}).get("summary_lines") or check.get("summary_lines")}

    def export_frozen(self, image: str, out: Path) -> Dict[str, Any]:
        """Keep the frozen artifacts so the objective checks can be re-run without the image.

        `final.patch` is the whole working-tree change against the base commit, untracked files
        included; `deliverables.tgz` is /deliverables; `head` is the final HEAD commit.
        """
        out = Path(out).resolve()
        out.mkdir(parents=True, exist_ok=True)
        base = json.loads((self.task_root / "tests" / "persona" / "binding.json").read_text())["base_commit"]
        proc = _docker_run(image, [f"{out}:/frozen"],
                           f"cd /app && git add -A . && git diff --cached --binary {base} > /frozen/final.patch && "
                           "git rev-parse HEAD > /frozen/head && tar -C / -czf /frozen/deliverables.tgz deliverables",
                           1800.0)
        files = {p.name: p.stat().st_size for p in out.iterdir() if p.is_file()}
        return {"ok": proc.returncode == 0, "base_commit": base, "files": files, "stderr": proc.stderr[-500:]}

    def persona_checker(self, image: str, transcript: List[Dict[str, Any]], requirements: List[Dict[str, Any]],
                        disclosed_turns: Dict[str, int]) -> Dict[str, Any]:
        work = self.work / "checker"
        work.mkdir(parents=True, exist_ok=True)
        (work / "input.json").write_text(json.dumps({"transcript": transcript, "disclosed_turns": disclosed_turns}))
        persona = self.task_root / "tests" / "persona"
        proc = _docker_run(image, [f"{persona}:/pcb/persona:ro", f"{work}:/pcb/input:ro"],
                           "python3 /pcb/persona/checker.py --binding /pcb/persona/binding.json "
                           "--input /pcb/input/input.json", 3600.0)
        (work / "stderr.txt").write_text(proc.stderr[-100000:])
        for line in reversed(proc.stdout.splitlines()):
            if line.startswith("{"):
                return json.loads(line)
        raise RuntimeError(f"checker printed no result (exit {proc.returncode}): {proc.stderr[-800:]}")
