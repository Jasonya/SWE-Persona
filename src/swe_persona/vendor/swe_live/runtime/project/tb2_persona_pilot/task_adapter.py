#!/usr/bin/env python3
"""Step 1 of the plan: turn a Terminal-Bench 2.0 task into a FROZEN manifest.

The manifest splits the task into three parts that never mix:

  public_contract  -- the original instruction, the deliverable paths, the
                      reference date, the "must actually change artifacts" rule.
                      The user simulator may answer agent questions from this.
  environment      -- controlled capability/permission facts (image, cpus,
                      memory, no network, registered tools, registered fixtures).
                      Personas may bind requirements to these, never invent them.
  hidden           -- tests/ and solution/ paths only.  Nothing that reads the
                      manifest for prompting is allowed to touch this: the
                      renderers call `public_view()`, and `assert_no_leak()`
                      re-checks rendered text against the hidden material.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_TASK_ROOT = Path("/xuanwu-tank/center/whx/coding_data/tb2_tasks/log-summary-date-ranges")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class TaskManifest:
    task_id: str
    task_root: Path
    dataset: str
    dataset_version: str
    docker_image: str
    instruction: str
    limits: Dict[str, Any]
    reference_date: str
    deliverables: List[str]
    environment: Dict[str, Any] = field(default_factory=dict)
    hidden: Dict[str, str] = field(default_factory=dict)
    frozen_at: Optional[str] = None

    # ---- construction ---------------------------------------------------
    @classmethod
    def from_harbor_task(
        cls,
        task_root: Path = DEFAULT_TASK_ROOT,
        dataset: str = "terminal-bench",
        dataset_version: str = "2.0",
        reference_date: str = "2025-08-12",
        deliverables: Optional[List[str]] = None,
    ) -> "TaskManifest":
        task_root = Path(task_root)
        toml = tomllib.loads((task_root / "task.toml").read_text(encoding="utf-8"))
        env = toml.get("environment", {})
        instruction = (task_root / "instruction.md").read_text(encoding="utf-8").strip()
        return cls(
            task_id=task_root.name,
            task_root=task_root,
            dataset=dataset,
            dataset_version=dataset_version,
            docker_image=env["docker_image"],
            instruction=instruction,
            limits={
                "cpus": env.get("cpus", 1),
                "memory": env.get("memory", "2G"),
                "storage": env.get("storage", "10G"),
                "agent_timeout_sec": toml.get("agent", {}).get("timeout_sec"),
                "verifier_timeout_sec": toml.get("verifier", {}).get("timeout_sec"),
            },
            reference_date=reference_date,
            deliverables=deliverables or ["/app/summary.csv"],
            hidden={
                "tests_dir": str(task_root / "tests"),
                "solution_path": str(task_root / "solution" / "solve.sh"),
            },
        )

    # ---- views ----------------------------------------------------------
    def public_view(self) -> Dict[str, Any]:
        """Everything a persona / simulator / agent is allowed to see."""
        return {
            "task_id": self.task_id,
            "source": f"{self.dataset}@{self.dataset_version}",
            "original_instruction": self.instruction,
            "frozen_contract": {
                "deliverable_paths": self.deliverables,
                "reference_date": self.reference_date,
                "must_change_artifacts": True,
                "public_interface_floor": (
                    "the original deliverable keeps its path, CSV schema and row order; "
                    "an evolved requirement may add artifacts but may never relax or "
                    "replace the original acceptance"
                ),
                "final_judgement": "the original Terminal-Bench tests, run by an isolated outcome verifier",
            },
            "environment": self.environment,
            "limits": self.limits,
        }

    def frozen_digest(self) -> str:
        payload = json.dumps(self.public_view(), sort_keys=True, ensure_ascii=False)
        return sha256_text(payload)[:16]

    # ---- leak guard -----------------------------------------------------
    def hidden_markers(self) -> List[str]:
        """Distinctive strings that must never appear in a rendered prompt."""
        markers: List[str] = []
        tests_dir = Path(self.hidden["tests_dir"])
        for path in sorted(tests_dir.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("[") and line.endswith("],") and line.count('"') >= 4:
                    markers.append(line.rstrip(","))
        solution = Path(self.hidden["solution_path"])
        if solution.is_file():
            for line in solution.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("count_severity_in_range()") or "CURRENT_DATE=" in line:
                    markers.append(line)
        return markers

    def assert_no_leak(self, rendered: str, where: str) -> None:
        for marker in self.hidden_markers():
            if marker and marker in rendered:
                raise AssertionError(f"hidden material leaked into {where}: {marker[:60]!r}")


def build_environment_overlay(manifest: TaskManifest, bulk_fixture_path: str, harness_mount: str) -> Dict[str, Any]:
    """Controlled capability facts for this episode (never persona fiction)."""
    return {
        "container_image": manifest.docker_image,
        "cpus": manifest.limits["cpus"],
        "memory": manifest.limits["memory"],
        "network": "disabled in the working container",
        "runtime": "python 3.13 standard library only; no pip install (no network), no extra services",
        "tool_capabilities": ["shell", "file_read", "file_write", "run_python", "measure_tool"],
        "permissions": [
            "read-only inputs: /app/logs and the registered bulk workload",
            "no network access",
            "no new dependencies",
        ],
        "registered_tools": {
            f"{harness_mount}/measure.py": (
                "measurement protocol: `python3 measure.py --json OUT -- <cmd>` reports wall time and "
                "peak RSS (VmHWM + rusage of the whole process tree) for one cold-start run"
            ),
            f"{harness_mount}/gen_bulk_logs.py": "regenerates the registered bulk workload deterministically",
            f"{harness_mount}/gen_semantic_logs.py": "generates the registered semantic-case log set",
        },
        "registered_fixtures": {
            "/app/logs": "the task's own 164-file / 6 MB log set (41 days ending on the reference date)",
            bulk_fixture_path: (
                "registered bulk workload, read-only: same generator and severity mix as /app/logs, "
                "964 files / ~2.4M lines / ~160 MB, 241 days ending on the reference date"
            ),
        },
    }
