"""Load one packaged task (`tasks/<task_id>/`) into the objects the episode needs.

Everything the host controller reads comes from the task directory: the frozen opening
(`instruction.md`), the original instruction (`tests/persona/original/instruction.md`),
and `tests/persona/` with its `closed_loop/` rules.  Nothing here is mounted into the
agent environment.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from .controller import NativeFact, Requirement


@dataclass
class TaskBundle:
    task_id: str
    root: Path
    opening: str
    original_instruction: str
    agent_brief: str
    persona_card: Dict[str, Any]
    public_task: Dict[str, Any]
    native_facts_spec: List[Dict[str, str]]
    requirements_spec: List[Dict[str, Any]]
    rubric: Dict[str, Any]
    policy: Dict[str, Any]
    judge_prompt: str
    simulator_prompt: str
    hidden_markers: List[str] = field(default_factory=list)
    timeout_sec: float = 3600.0
    environment: Dict[str, Any] = field(default_factory=dict)

    def native_facts(self) -> List[NativeFact]:
        return [NativeFact(item["id"], item["wording"]) for item in self.native_facts_spec]

    def requirements(self) -> List[Requirement]:
        criteria = {item["requirement_id"]: item["criteria"] for item in self.rubric["requirements"]}
        return [
            Requirement(
                requirement_id=item["requirement_id"], source_ref=item["source_ref"],
                disclosure=item["disclosure"], criteria=criteria[item["requirement_id"]],
                depends_on=list(item.get("depends_on", [])),
                artifact_paths=list(item.get("artifact_paths", [])),
            )
            for item in self.requirements_spec
        ]

    @classmethod
    def from_dir(cls, root: Path) -> "TaskBundle":
        root = Path(root)
        persona_dir = root / "tests" / "persona"
        closed = persona_dir / "closed_loop"

        def load(path: Path) -> Any:
            return json.loads(path.read_text(encoding="utf-8"))

        config = tomllib.loads((root / "task.toml").read_text(encoding="utf-8"))
        interaction = load(persona_dir / "interaction.json")
        blurred = load(persona_dir / "blurred_task.json")
        evolved = load(persona_dir / "evolved_requirements.json")
        persona = load(persona_dir / "persona.json")
        return cls(
            task_id=config["task"]["name"],
            root=root,
            opening=(root / "instruction.md").read_text(encoding="utf-8").strip(),
            original_instruction=(persona_dir / "original" / "instruction.md").read_text(encoding="utf-8"),
            agent_brief=interaction["agent_brief"],
            persona_card=persona["card"],
            public_task=blurred["public_task"],
            native_facts_spec=blurred["original_facts"],
            requirements_spec=evolved["requirements"],
            rubric=load(closed / "rubric.json"),
            policy=load(closed / "policy.json"),
            judge_prompt=(closed / "judge_prompt.md").read_text(encoding="utf-8"),
            simulator_prompt=(closed / "simulator_prompt.md").read_text(encoding="utf-8"),
            hidden_markers=list(interaction.get("leak_guard", {}).get("markers", [])),
            timeout_sec=float(config["agent"]["timeout_sec"]),
            environment=dict(config.get("environment", {})),
        )
