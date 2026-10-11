#!/usr/bin/env python3
"""Load task-persona candidates from versioned binding JSON files.

The old first-time-contributor binder remains as an explicit compatibility
fallback for batches that predate versioned data. New odd-v1 batches are data
driven and fail closed when their binding is missing or not ready.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tb2_persona_pilot.registry import Candidate, load_persona  # noqa: E402
from task_adapter import TaskManifest  # noqa: E402
from evidence_policy import corrected_parameters  # noqa: E402

HERE = Path(__file__).resolve().parent
BINDINGS = HERE / "bindings"
PERSONA_ID = "first_time_pr_contributor"
DEFAULT_DATA_VERSION = "legacy"
READY_STATUSES = {"ready", "validated"}


def persona_pool_version() -> Dict[str, str]:
    path = Path(os.environ.get(
        "PCB_PERSONA_FILE", HERE.parent / "human-interactive-coding-personas.v3.json"
    )).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "schema_version": str(data["schema_version"]),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def binding_path(manifest: TaskManifest, persona_id: str, data_version: str) -> Path:
    canonical = sorted((BINDINGS / data_version).glob(
        f"{manifest.task_id}-{persona_id}-????????/binding.json"))
    if len(canonical) > 1:
        raise ValueError("multiple dated bindings match; use an unambiguous data version")
    if canonical:
        return canonical[0]
    # odd-v1 predates the one-based filename convention and remains available
    # only as diagnostic data. New data versions match collaborator run names.
    row_number = manifest.row_index_zero if data_version == "odd-v1" else manifest.row_index_zero + 1
    return BINDINGS / data_version / f"row-{row_number:03d}" / f"{persona_id}.json"


def load_binding(manifest: TaskManifest, persona_id: str, data_version: str) -> Dict[str, Any]:
    path = binding_path(manifest, persona_id, data_version)
    if not path.is_file():
        raise FileNotFoundError(f"binding not found: {path}")
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("instance_id") != manifest.task_id:
        raise ValueError(f"binding instance mismatch in {path}")
    if int(document.get("row_index_zero", -1)) != manifest.row_index_zero:
        raise ValueError(f"binding row mismatch in {path}")
    if document.get("persona_id") != persona_id or document.get("data_version") != data_version:
        raise ValueError(f"binding identity mismatch in {path}")
    expected_contract = hashlib.sha256(
        json.dumps(
            manifest.public_contract,
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    if document.get("public_contract_sha256") != expected_contract:
        raise ValueError(f"binding public contract hash mismatch in {path}")
    version = persona_pool_version()
    recorded = document.get("persona_version") or {}
    if recorded.get("schema_version") != version["schema_version"] or recorded.get("sha256") != version["sha256"]:
        raise ValueError(f"binding persona version mismatch in {path}")
    return document


def _candidate_from_json(item: Dict[str, Any], persona_id: str) -> Candidate:
    persona = load_persona(persona_id)
    templates = {entry["id"]: entry for entry in persona["attributes"]["evolved_requirements"]}
    template_id = item["template_id"]
    if template_id not in templates:
        raise ValueError(f"unknown template {template_id!r} for persona {persona_id!r}")
    template = deepcopy(templates[template_id])
    rubric_override = item.get("rubric_override")
    if rubric_override is not None:
        if not isinstance(rubric_override, dict):
            raise ValueError("rubric_override must be an object")
        revision_id = rubric_override.get("revision_id")
        satisfaction = rubric_override.get("satisfaction")
        supplied_hash = rubric_override.get("sha256")
        if not isinstance(revision_id, str) or not revision_id.strip():
            raise ValueError("rubric_override requires a non-empty revision_id")
        required_keys = {"judge", "met_if", "not_met_if", "unknown_if", "evidence_required"}
        if not isinstance(satisfaction, dict) or set(satisfaction) != required_keys:
            raise ValueError("rubric_override satisfaction has an invalid shape")
        if satisfaction["judge"] != (template.get("satisfaction") or {}).get("judge"):
            raise ValueError("rubric_override cannot change the judge tier")
        for key in ("met_if", "not_met_if", "unknown_if", "evidence_required"):
            clauses = satisfaction[key]
            if (not isinstance(clauses, list)
                    or any(not isinstance(clause, str) or not clause.strip() for clause in clauses)):
                raise ValueError(f"rubric_override {key} must contain non-empty strings")
        canonical = json.dumps(satisfaction, sort_keys=True, ensure_ascii=False).encode("utf-8")
        expected_hash = hashlib.sha256(canonical).hexdigest()
        if supplied_hash != expected_hash:
            raise ValueError("rubric_override sha256 mismatch")
        template["satisfaction"] = deepcopy(satisfaction)
        # Preserve the exact rubric identity in the episode ledger. RubricSpec
        # consumes only its declared clause fields.
        template["satisfaction"]["revision_id"] = revision_id
        template["satisfaction"]["revision_sha256"] = expected_hash
    checker = deepcopy(item["checker"])
    if checker.get("id") == "rubric_judge":
        checker["parameters"] = corrected_parameters(
            template_id, template["satisfaction"], checker.get("parameters", {}))
    candidate = Candidate(
        candidate_id=item["candidate_id"],
        requirement_id=item["requirement_id"],
        template_id=template_id,
        persona_id=persona_id,
        template=template,
        bindings=dict(item["bindings"]),
        addendum=str(item.get("addendum") or ""),
        checker=checker,
        schedule=dict(item["schedule"]),
        contract_compat=str(item["newness_justification"]),
        evidence_refs=list(item["source_refs"]),
        disclosure_markers=list(item.get("disclosure_markers") or []),
        depends_on=list(item.get("depends_on") or []),
        # Candidate.revision is a controller schedule for revising a user
        # requirement. Rubric revision metadata stays in satisfaction_policy.
        revision=item.get("revision"),
        applicable=True,
    )
    candidate.statement()  # fail before an episode if any template slot is unbound
    return candidate


def _legacy_candidates(manifest: TaskManifest, persona_id: str) -> List[Candidate]:
    if persona_id != PERSONA_ID:
        raise KeyError(f"legacy pilot binds only {PERSONA_ID!r}, got {persona_id!r}")
    persona = load_persona(persona_id)
    templates = {item["id"]: item for item in persona["attributes"]["evolved_requirements"]}
    base = manifest.base_commit
    return [
        Candidate(
            candidate_id=f"cand-pr-scope-row-{manifest.row_index_zero}",
            requirement_id="req.first_pr.scope.01", template_id="first_pr_isolated_patch",
            persona_id=persona_id, template=templates["first_pr_isolated_patch"],
            bindings={
                "allowed_scope": "only repository files justified as necessary for the public task",
                "protected_changes": "the clean baseline and all content unrelated to the requested task",
                "baseline_snapshot": f"clean base commit `{base}` and its recorded empty git status",
            },
            checker={"id": "rubric_judge", "version": "1", "parameters": {
                "requirement": "req.first_pr.scope.01@1",
                "subcheck": {"id": "task_patch_manifest", "version": "1", "parameters": {"base_commit": base}},
                "evidence_paths": [], "message_clauses": ["met_if[1]"], "samples": 3,
            }},
            schedule={"trigger_event": "approach_visible", "earliest_turn": 2, "deadline_turn": 3,
                      "priority": 1, "required": True},
            contract_compat="adds an auditable task-only patch without narrowing the public task",
            evidence_refs=[f"dataset:base_commit={base}", "snapshot:initial_git_status=clean", "contract:public"],
            disclosure_markers=[base, "only repository files"],
        ),
        Candidate(
            candidate_id=f"cand-pr-explanation-row-{manifest.row_index_zero}",
            requirement_id="req.first_pr.explanation.01", template_id="first_pr_patch_explanation",
            persona_id=persona_id, template=templates["first_pr_patch_explanation"],
            bindings={
                "failure_example": "the concrete failure or acceptance example in the public contract",
                "patch_anchor": f"the current task diff and changed symbols relative to base commit `{base}`",
            },
            checker={"id": "rubric_judge", "version": "1", "parameters": {
                "requirement": "req.first_pr.explanation.01@1",
                "subcheck": {"id": "official_tests", "version": "1", "parameters": {}},
                "evidence_paths": [], "message_clauses": ["met_if[0]", "met_if[1]"], "samples": 3,
            }},
            schedule={"trigger_event": "first_patch", "earliest_turn": 3, "deadline_turn": 5,
                      "priority": 2, "required": True},
            contract_compat="adds a review explanation grounded in the public contract and current diff",
            evidence_refs=["contract:problem_statement", "contract:requirements", f"snapshot:task_diff_from={base}"],
            disclosure_markers=[base, "current task diff"], depends_on=["req.first_pr.scope.01@1"],
        ),
    ]


def build_candidates(
    manifest: TaskManifest,
    persona_id: str = PERSONA_ID,
    max_evolved: int = 2,
    data_version: Optional[str] = None,
) -> List[Candidate]:
    del max_evolved
    version = data_version or os.environ.get("PERSONA_DATA_VERSION", DEFAULT_DATA_VERSION)
    if version == DEFAULT_DATA_VERSION:
        return _legacy_candidates(manifest, persona_id)
    document = load_binding(manifest, persona_id, version)
    if document.get("applicability", {}).get("verdict") != "applicable":
        return []
    if document.get("status") not in READY_STATUSES:
        raise RuntimeError(
            f"binding {binding_path(manifest, persona_id, version)} is {document.get('status')!r}, not ready"
        )
    return [_candidate_from_json(item, persona_id) for item in document.get("requirements", [])]


def applicable_candidates(candidates: List[Candidate], max_evolved: int) -> List[Candidate]:
    return [candidate for candidate in candidates if candidate.applicable][:max(0, max_evolved)]


__all__ = [
    "BINDINGS", "DEFAULT_DATA_VERSION", "PERSONA_ID", "applicable_candidates", "binding_path",
    "build_candidates", "load_binding", "load_persona", "persona_pool_version",
]
