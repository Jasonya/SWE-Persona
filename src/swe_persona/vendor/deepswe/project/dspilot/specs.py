"""Declarative task/persona registry used by the configurable launchers.

The runtime consumes an already-bound episode.  Adding a task or a compatible
task/persona pair is therefore a data operation: add one task JSON and one
binding JSON, then run the catalog validator.  New checker *kinds* still need a
checker plug-in; bindings may only name kinds registered by the runtime.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .source_scope import root_pattern


ROOT = Path(__file__).resolve().parents[1]
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SUPPORTED_TRIGGERS = {"first_plan", "first_patch", "first_artifact", "completion_claim", "registered_change_observed"}
SUPPORTED_CHECKERS = {
    "isolated_patch",
    "grounded_explanation",
    "reviewable_handoff",
    "grounded_rubric",
    "trusted_probe",
}
PAIRING_STATUSES = {"runnable", "applicable_pending_binding", "not_applicable", "pending_review"}


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot load registry file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Registry file must contain a JSON object: {path}")
    return value


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def _relative(value: str, label: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} must be a safe relative path: {value!r}")
    return value


@dataclass(frozen=True)
class TaskSpec:
    id: str
    path: Path
    raw: dict[str, Any]

    @property
    def cache_dir(self) -> Path:
        return ROOT / ".cache" / "tasks" / self.raw.get("cache_dir", self.id)

    @property
    def contract_path(self) -> Path:
        return ROOT / _relative(self.raw["contract_file"], "contract_file")

    @property
    def base_commit(self) -> str:
        return self.raw["base_commit"]

    @property
    def image(self) -> str:
        override=ROOT/"environment-images.json"
        images=json.loads(override.read_text()) if override.exists() else {}
        return images.get(self.id,self.raw["image"])

    @property
    def source_roots(self) -> list[str]:
        return list(self.raw["source_roots"])

    @property
    def protected_files(self) -> list[dict[str, str]]:
        return list(self.raw.get("protected_files", []))

    @property
    def artifact_files(self) -> list[str]:
        return list(self.raw.get("artifact_files", []))


@dataclass(frozen=True)
class BindingSpec:
    task_id: str
    persona_id: str
    path: Path
    raw: dict[str, Any]

    @property
    def requirements(self) -> list[dict[str, Any]]:
        return list(self.raw["requirements"])

    @property
    def public_probes(self) -> dict[str, dict[str, Any]]:
        return {item["id"]: item for item in self.raw.get("public_probes", [])}


@dataclass(frozen=True)
class EpisodeSpec:
    task: TaskSpec
    persona: dict[str, Any]
    binding: BindingSpec

    @property
    def requirements(self) -> list[dict[str, Any]]:
        return self.binding.requirements


def persona_catalog() -> dict[str, dict[str, Any]]:
    path = ROOT / "data" / "personas.v3.json"
    data = _load(path)
    values = data.get("personas")
    if not isinstance(values, list):
        raise ValueError(f"Missing personas[] in {path}")
    result: dict[str, dict[str, Any]] = {}
    for persona in values:
        identifier = _identifier(persona.get("id"), "persona id")
        if identifier in result:
            raise ValueError(f"Duplicate persona id {identifier}")
        result[identifier] = persona
    return result


def pairing_plan() -> list[dict[str, Any]]:
    """Load the preregistered cross-product without treating drafts as runnable."""
    path = ROOT / "data" / "pairings.v1.json"
    data = _load(path)
    if data.get("design") != "full_cross_product":
        raise ValueError(f"Unsupported pairing design in {path}")
    task_axis = data.get("tasks")
    persona_axis = data.get("personas")
    cells = data.get("cells")
    fixtures = data.get("fixture_plans")
    if not isinstance(task_axis, list) or not task_axis:
        raise ValueError(f"Missing tasks[] in {path}")
    if not isinstance(persona_axis, list) or not persona_axis:
        raise ValueError(f"Missing personas[] in {path}")
    if not isinstance(cells, list) or not cells:
        raise ValueError(f"Missing cells[] in {path}")
    if not isinstance(fixtures, dict):
        raise ValueError(f"Missing fixture_plans object in {path}")
    catalog = persona_catalog()
    tasks = [_identifier(value, "pairing task id") for value in task_axis]
    personas = [_identifier(value, "pairing persona id") for value in persona_axis]
    if len(tasks) != len(set(tasks)):
        raise ValueError("Pairing task axis contains duplicates")
    if len(personas) != len(set(personas)):
        raise ValueError("Pairing persona axis contains duplicates")
    if set(personas) != set(catalog):
        raise ValueError("Pairing persona axis must contain every catalog persona exactly once")
    expected = {(task_id, persona_id) for task_id in tasks for persona_id in personas}
    seen: set[tuple[str, str]] = set()
    pairs: list[dict[str, Any]] = []
    for cell in cells:
        pair = dict(cell)
        task_id = _identifier(pair.get("task_id"), "pairing task id")
        persona_id = _identifier(pair.get("persona_id"), "pairing persona id")
        key = (task_id, persona_id)
        if key not in expected:
            raise ValueError(f"Pairing cell lies outside the declared axes: {key}")
        if key in seen:
            raise ValueError(f"Duplicate pairing cell: {key}")
        if persona_id not in catalog:
            raise ValueError(f"Pairing plan references unknown persona: {persona_id}")
        if pair.get("status") not in PAIRING_STATUSES:
            raise ValueError(f"Unsupported pairing status for {task_id}/{persona_id}: {pair.get('status')!r}")
        if not isinstance(pair.get("applicability"), str) or not pair["applicability"].strip():
            raise ValueError(f"Pairing {task_id}/{persona_id} needs applicability")
        fixture = fixtures.get(persona_id)
        if not isinstance(fixture, str) or not fixture.strip():
            raise ValueError(f"Pairing fixture plan is missing for persona {persona_id}")
        pair["fixture_plan"] = fixture
        pair["ordinal"] = tasks.index(task_id) * len(personas) + personas.index(persona_id) + 1
        pairs.append(pair)
        seen.add(key)
    if seen != expected:
        missing = sorted(expected - seen)
        raise ValueError(f"Cross-product pairing plan is incomplete: {missing}")
    task_ids = set(task_paths())
    bindings = set(binding_paths())
    for pair in pairs:
        key = (pair["task_id"], pair["persona_id"])
        registered = key in bindings and pair["task_id"] in task_ids
        if pair["status"] == "runnable" and not registered:
            raise ValueError(f"Runnable pairing has no registered task/binding: {key}")
        if pair["status"] != "runnable" and registered:
            raise ValueError(f"Registered task/binding must be marked runnable: {key}")
    return sorted(pairs, key=lambda pair: pair["ordinal"])


def task_paths() -> dict[str, Path]:
    result: dict[str, Path] = {}
    for path in sorted((ROOT / "data" / "tasks").glob("*.json")):
        task_id = _identifier(_load(path).get("id"), "task id")
        if task_id in result:
            raise ValueError(f"Duplicate task id {task_id}")
        result[task_id] = path
    return result


def binding_paths() -> dict[tuple[str, str], Path]:
    result: dict[tuple[str, str], Path] = {}
    for path in sorted((ROOT / "data" / "bindings").glob("*.json")):
        raw = _load(path)
        key = (_identifier(raw.get("task_id"), "binding task id"),
               _identifier(raw.get("persona_id"), "binding persona id"))
        if key in result:
            raise ValueError(f"Duplicate task/persona binding {key}")
        result[key] = path
    return result


def load_task(task_id: str) -> TaskSpec:
    task_id = _identifier(task_id, "task id")
    paths = task_paths()
    if task_id not in paths:
        raise KeyError(f"Unknown task {task_id!r}; available tasks: {', '.join(paths) or '(none)'}")
    path = paths[task_id]
    raw = _load(path)
    for field in ("base_commit", "image", "contract_file", "source_roots",
                  "environment", "public_validation", "official_evaluation"):
        if field not in raw:
            raise ValueError(f"Task {task_id} is missing {field}")
    if not raw["source_roots"] or not all(isinstance(v, str) and v for v in raw["source_roots"]):
        raise ValueError(f"Task {task_id} must declare nonempty source_roots")
    for root in raw['source_roots']:
        root_pattern(root)  # Validate opt-in selectors; literal roots unchanged.
    if not (ROOT / _relative(raw["contract_file"], "contract_file")).is_file():
        raise ValueError(f"Task {task_id} contract_file does not exist")
    for fixture in raw.get("protected_files", []):
        _relative(fixture["path"], "protected file")
        if not isinstance(fixture.get("content"), str):
            raise ValueError(f"Task {task_id} protected file needs string content")
    for name in raw.get("artifact_files", []):
        _relative(name, "artifact file")
    return TaskSpec(task_id, path, raw)


def load_binding(task_id: str, persona_id: str, *, path: Path | None = None) -> BindingSpec:
    task_id = _identifier(task_id, "task id")
    persona_id = _identifier(persona_id, "persona id")
    if path is None:
        paths = binding_paths()
        key = (task_id, persona_id)
        if key not in paths:
            pairs = ", ".join(f"{task}/{persona}" for task, persona in paths) or "(none)"
            raise KeyError(f"No registered binding for {task_id}/{persona_id}; available bindings: {pairs}")
        path = paths[key]
    raw = _load(path)
    if (raw.get('task_id'), raw.get('persona_id')) != (task_id, persona_id):
        raise ValueError('Binding identity does not match requested task/persona')
    requirements = raw.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise ValueError(f"Binding {task_id}/{persona_id} needs requirements[]")
    ids: set[str] = set()
    triggers: set[str] = set()
    for requirement in requirements:
        req_id = _identifier(requirement.get("id"), "requirement id")
        if req_id in ids:
            raise ValueError(f"Duplicate requirement id {req_id}")
        ids.add(req_id)
        trigger = requirement.get("trigger")
        if not isinstance(trigger, str) or not trigger:
            raise ValueError(f"Requirement {req_id} needs its persona template trigger")
        runtime_trigger = requirement.get("runtime_trigger", trigger)
        if runtime_trigger not in SUPPORTED_TRIGGERS:
            raise ValueError(f"Unsupported runtime trigger {runtime_trigger!r} in {req_id}")
        if runtime_trigger in triggers:
            raise ValueError(
                f"Only one requirement per runtime trigger is currently supported: {runtime_trigger}"
            )
        triggers.add(runtime_trigger)
        checker = requirement.get("checker", {})
        if checker.get("kind") not in SUPPORTED_CHECKERS:
            raise ValueError(f"Unsupported checker kind in {req_id}: {checker.get('kind')!r}")
        if not isinstance(requirement.get("statement"), str) or not requirement["statement"].strip():
            raise ValueError(f"Requirement {req_id} needs a bound statement")
        if runtime_trigger == "first_artifact":
            artifact = requirement.get("trigger_artifact")
            if not isinstance(artifact, str) or not artifact:
                raise ValueError(f"First-artifact requirement {req_id} needs trigger_artifact")
            _relative(artifact, "trigger_artifact")
    probes = raw.get("public_probes", [])
    probe_ids = {probe.get("id") for probe in probes}
    if len(probe_ids) != len(probes):
        raise ValueError(f"Binding {task_id}/{persona_id} has duplicate public probe ids")
    for requirement in requirements:
        probe = requirement.get("public_probe")
        if probe is not None and not isinstance(probe, str):
            raise ValueError(f"Requirement {requirement['id']} public_probe must be a string ID referencing public_probes, not an object")
        if probe is not None and probe not in probe_ids:
            raise ValueError(f"Requirement {requirement['id']} references unknown public probe {probe}")
        if requirement['checker']['kind']=='grounded_explanation' and not probe:
            raise ValueError(f"Grounded explanation {requirement['id']} needs a public_probe")
        if requirement['checker']['kind']=='trusted_probe' and not probe:
            raise ValueError(f"Trusted probe {requirement['id']} needs a public_probe")
        checker_probe=requirement['checker'].get('parameters',{}).get('public_probe')
        if checker_probe is not None and checker_probe not in probe_ids:
            raise ValueError(f"Requirement {requirement['id']} references unknown checker probe {checker_probe}")
    for probe in probes:
        if not isinstance(probe.get('script'),str) or not isinstance(probe.get('command'),str):
            raise ValueError(f"Public probe {probe.get('id')} needs script and command")
        for extra in probe.get('extra_files',[]):
            if not isinstance(extra.get('filename'),str) or not isinstance(extra.get('script'),str):
                raise ValueError(f"Public probe {probe.get('id')} has an invalid extra file")
            _relative(extra['filename'],'public probe extra filename')
    if not isinstance(raw.get('opening'),str) or not raw['opening'].strip():
        raise ValueError(f"Binding {task_id}/{persona_id} needs an opening message")
    calibration=raw.get('calibration',{})
    if calibration:
        _relative(calibration.get('reference_patch','solution/solution.patch'),'reference_patch')
        for artifact,source_path in calibration.get('positive_artifacts',{}).items():
            _relative(artifact,'calibration artifact')
            source=ROOT/_relative(source_path,'calibration artifact source')
            if not source.is_file():raise ValueError(f"Calibration artifact source does not exist: {source}")
        for artifact,content in calibration.get('positive_artifact_contents',{}).items():
            _relative(artifact,'calibration artifact')
            if not isinstance(content,str) or not content.strip():
                raise ValueError(f"Calibration artifact content must be nonempty: {artifact}")
        if 'positive_turns' in calibration and not isinstance(calibration['positive_turns'],list):
            raise ValueError('calibration positive_turns must be a list')
    return BindingSpec(task_id, persona_id, path, raw)


def compile_episode(task_id: str, persona_id: str, *, binding_path: Path | None = None) -> EpisodeSpec:
    task = load_task(task_id)
    personas = persona_catalog()
    if persona_id not in personas:
        raise KeyError(f"Unknown persona {persona_id!r}; available personas: {', '.join(personas)}")
    binding = load_binding(task_id, persona_id, path=binding_path)
    template_values = personas[persona_id]["attributes"]["evolved_requirements"]
    templates = {item["id"] for item in template_values}
    unknown = [req["template"] for req in binding.requirements if req.get("template") not in templates]
    if unknown:
        raise ValueError(f"Binding references templates absent from persona {persona_id}: {unknown}")
    trigger_by_template = {
        item["id"]: item.get("trigger", {}).get("event") for item in template_values
    }
    mismatched = [
        req["id"] for req in binding.requirements
        if req.get("trigger") != trigger_by_template.get(req["template"])
    ]
    if mismatched:
        raise ValueError(
            f"Binding rewrites persona template trigger instead of using runtime_trigger: {mismatched}"
        )
    return EpisodeSpec(task, personas[persona_id], binding)


def selected_ids() -> tuple[str, str]:
    return (os.environ.get("DS_TASK_ID", "tomlkit-toml-table-converters"),
            os.environ.get("DS_PERSONA_ID", "first_time_pr_contributor"))


def selected_episode() -> EpisodeSpec:
    episode = compile_episode(*selected_ids())
    protocol = os.environ.get('DS_PROTOCOL', 'legacy')
    if protocol == 'staged':
        from .staged import adapt_episode
        return adapt_episode(episode)
    if protocol != 'legacy':
        raise ValueError('Unsupported DS_PROTOCOL')
    return episode


def catalog_summary() -> dict[str, Any]:
    return {
        "tasks": sorted(task_paths()),
        "personas": sorted(persona_catalog()),
        "bindings": [{"task_id": task, "persona_id": persona}
                     for task, persona in sorted(binding_paths())],
        "pairing_plan": pairing_plan(),
    }
