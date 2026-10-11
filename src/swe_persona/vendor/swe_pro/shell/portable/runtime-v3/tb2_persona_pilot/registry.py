#!/usr/bin/env python3
"""Candidate registry + binder (plan section 3).

Requirements are NOT invented by an LLM at conversation time.  They come from
the offline persona template pool (../human-interactive-coding-personas.v3.json)
and are bound here to this Terminal-Bench 2.0 task's real environment facts and
registered fixtures.  A template whose slots cannot be bound is `skipped` /
`not_applicable` -- never bound to something the environment does not have.

Binding for `log-summary-date-ranges` + persona `laptop_data_scientist`:

  laptop_measured_resource_budget -> req.resource_budget.01
      bound to the container's real limits (1 cpu / 2G), the registered bulk
      workload, and the registered measurement tool.  Checker: resource_budget.
  laptop_preserve_data_semantics  -> req.data_semantics.01
      bound to clauses that are all in the ORIGINAL instruction (bracketed
      severity field, inclusive windows, month-to-date start, fixed row order)
      and to the registered semantic-case fixture.  Checker: semantic_matrix.
  laptop_reproducible_local_delivery -> req.repro_handoff.01
      bindable, but held back by the evolved-requirement budget (max 2), so it
      stays `skipped` unless --max-evolved 3.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ledger import RequirementEntry

PERSONA_FILE = Path(os.environ.get(
    "PCB_PERSONA_FILE",
    Path(__file__).resolve().parent.parent / "human-interactive-coding-personas.v3.json",
)).resolve()

ENTRYPOINT = "python3 /app/summarize.py --logs <LOG_DIR> --reference-date <YYYY-MM-DD> --out <CSV_PATH>"
BULK_WORKLOAD = "/app/logs_bulk"
CAP_PEAK_RSS_KIB = 131072  # 128 MiB
CAP_WALL_SEC = 120.0
SEMANTIC_REF_DATE = "2025-08-13"

# The pool names trigger events in the persona's vocabulary; the harness emits typed events.
# `first_plan` maps to `approach_visible`, which fires whether the approach was stated in a
# message or is already visible in the workspace: an agent that starts coding immediately
# must not make a required requirement unreachable (that happened, and the requirement was
# then only disclosed by its deadline).
TRIGGER_MAP = {"first_plan": "approach_visible"}


def harness_trigger(template: Dict[str, Any]) -> str:
    event = (template.get("trigger") or {}).get("event", "first_plan")
    return TRIGGER_MAP.get(event, event)


def load_persona(persona_id: str) -> Dict[str, Any]:
    data = json.loads(PERSONA_FILE.read_text(encoding="utf-8"))
    for persona in data["personas"]:
        if persona["id"] == persona_id:
            return persona
    raise KeyError(f"persona {persona_id} not in {PERSONA_FILE}")


def render_slots(text: str, bindings: Dict[str, str]) -> str:
    """Deterministic {{slot}} substitution -- no LLM in the loop."""
    def sub(match: "re.Match[str]") -> str:
        key = match.group(1).strip()
        if key not in bindings:
            raise KeyError(f"unbound slot {{{{{key}}}}}")
        return bindings[key]

    return re.sub(r"\{\{([a-zA-Z0-9_]+)\}\}", sub, text)


@dataclass
class Candidate:
    candidate_id: str
    requirement_id: str
    template_id: str
    persona_id: str
    bindings: Dict[str, str]
    checker: Dict[str, Any]
    schedule: Dict[str, Any]
    contract_compat: str
    evidence_refs: List[str]
    addendum: str = ""
    disclosure_markers: List[str] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)
    revision: Optional[Dict[str, Any]] = None
    template: Dict[str, Any] = field(default_factory=dict)
    applicable: bool = True
    skip_reason: str = ""

    @property
    def judge_tier(self) -> str:
        """trusted_harness | hybrid | grounded_llm, as declared by the template."""
        return (self.template.get("satisfaction") or {}).get("judge", "unknown")

    def statement(self) -> str:
        """Canonical statement: the pool template's assertions, slots bound."""
        base = render_slots(self.template["user_requirement"], self.bindings)
        return (base + ("\n" + self.addendum if self.addendum else "")).strip()

    def to_entry(self) -> RequirementEntry:
        return RequirementEntry(
            id=self.requirement_id,
            version=1,
            template_id=self.template_id,
            persona_id=self.persona_id,
            source={
                "candidate_id": self.candidate_id,
                "template_id": self.template_id,
                "evidence_refs": list(self.evidence_refs),
                "contract_compat": self.contract_compat,
                "trigger": self.template.get("trigger", {}),
                "satisfaction_policy": self.template.get("satisfaction", {}),
                "judge_tier": self.judge_tier,
            },
            statement=self.statement(),
            checker=dict(self.checker),
            schedule=dict(self.schedule),
            bindings=dict(self.bindings),
            depends_on=list(self.depends_on),
        )


def _laptop_candidates(persona_id: str = "laptop_data_scientist") -> List[Candidate]:
    persona = load_persona(persona_id)
    templates = {t["id"]: t for t in persona["attributes"]["evolved_requirements"]}

    resource_budget = Candidate(
        candidate_id="cand-01",
        requirement_id="req.resource_budget.01",
        template_id="laptop_measured_resource_budget",
        persona_id=persona_id,
        template=templates["laptop_measured_resource_budget"],
        bindings={
            "resource_limits": (
                f"peak RSS ≤ 128 MiB ({CAP_PEAK_RSS_KIB} KiB) and wall clock ≤ {int(CAP_WALL_SEC)} s "
                "for one cold-start run, counted over the whole process tree (subprocesses included), "
                "inside the container's real 1 cpu / 2G limits"
            ),
            "measurement_protocol": (
                "`python3 /harness/measure.py --json /tmp/m.json -- "
                f"python3 /app/summarize.py --logs {BULK_WORKLOAD} --reference-date 2025-08-12 "
                "--out /tmp/bulk_summary.csv`; one cold start, sampled tree RSS, the reported "
                "`peak_rss_kib` and `wall_sec` are what count; the run must process the whole "
                f"registered workload {BULK_WORKLOAD} (964 files / ~2.4M lines / ~146 MB)"
            ),
            "allowed_runtime": (
                "the task container as shipped: python 3.13 standard library only, no network, "
                "no new dependencies, no extra services; /app/logs and /app/logs_bulk are read-only"
            ),
        },
        addendum=(
            "The delivery entry point is fixed: `" + ENTRYPOINT + "`. It must write the original "
            "CSV schema (`period,severity,count`, the same 15 rows in the same order) to the path "
            "given by --out, and it must not overwrite /app/summary.csv when --out points elsewhere."
        ),
        checker={
            "id": "resource_budget",
            "version": "1",
            "parameters": {
                "workload": BULK_WORKLOAD,
                "reference_date": "2025-08-12",
                "cap_peak_rss_kib": CAP_PEAK_RSS_KIB,
                "cap_wall_sec": CAP_WALL_SEC,
                "entrypoint": ENTRYPOINT,
            },
        },
        schedule={"trigger_event": harness_trigger(templates["laptop_measured_resource_budget"]),
                  "earliest_turn": 2, "deadline_turn": 4, "priority": 1, "required": True},
        disclosure_markers=["/app/summarize.py", "128 MiB", "measure.py", "--reference-date"],
        contract_compat=(
            "adds a second artifact (a parameterized entry point) and a measured budget; does not "
            "touch /app/summary.csv's required content, the CSV schema, or the original tests"
        ),
        evidence_refs=[
            "env:task.toml[environment].cpus=1,memory=2G",
            "fixture:logs_bulk@gen_bulk_logs.py(days_back=240,min=2000,max=3000)",
            "tool:/harness/measure.py@2",
        ],
    )

    data_semantics = Candidate(
        candidate_id="cand-02",
        requirement_id="req.data_semantics.01",
        template_id="laptop_preserve_data_semantics",
        persona_id=persona_id,
        template=templates["laptop_preserve_data_semantics"],
        bindings={
            "semantic_contract": (
                "the clauses already in the task statement: a line's severity is its first bracketed "
                "field (a [WARNING] line whose text contains the word ERROR is still WARNING), [DEBUG] "
                "is not reported, the windows are inclusive on both ends (last_7_days = [ref-6, ref], "
                "last_30_days = [ref-29, ref]), month_to_date starts on the 1st of the reference "
                "month, and the CSV keeps the 15 rows in the fixed order"
            ),
            "semantic_cases": (
                "the registered case set from `python3 /harness/gen_semantic_logs.py --out <DIR>` "
                "(trap lines with ERROR inside a [WARNING] message, a [DEBUG] line, an unbracketed "
                "ERROR line, and boundary files at ref-6 / ref-7 / ref-29 / ref-30 / the 1st of the "
                f"month / the last day of the previous month), run with --reference-date {SEMANTIC_REF_DATE}"
            ),
            "comparison_rule": (
                "exact integer equality on all 15 rows against the harness's own independent oracle; "
                "no tolerance, no re-ordering"
            ),
        },
        checker={
            "id": "semantic_matrix",
            "version": "1",
            "parameters": {"fixture": "semantic", "reference_date": SEMANTIC_REF_DATE, "comparison": "exact", "entrypoint": ENTRYPOINT},
        },
        schedule={"trigger_event": "first_patch", "earliest_turn": 3, "deadline_turn": 6, "priority": 2, "required": True},
        disclosure_markers=["gen_semantic_logs.py", "2025-08-13", "bracketed"],
        contract_compat=(
            "every clause is a restatement of the original instruction applied to a registered "
            "fixture; it cannot change what /app/summary.csv must contain for the reference date"
        ),
        evidence_refs=[
            "contract:instruction.md#severity-levels-are-exactly",
            "contract:instruction.md#including-today",
            "fixture:logs_semantic@gen_semantic_logs.py",
        ],
        depends_on=["req.resource_budget.01@1"],
        revision={
            "trigger_event": "reference_date_ignored",
            "earliest_turn": 4,
            "deadline_turn": 7,
            "reason_code": "probe_showed_reference_date_not_honoured",
            "markers": ["--reference-date", "windows"],
            "addendum": (
                "Revision: the --reference-date you are passed must actually drive the windows. A probe "
                "ran your entry point twice on the same logs with two different --reference-date values "
                "and got the same `today` row, so the date is baked in. Keep the 2025-08-12 deliverable "
                "correct and make the windows follow the argument."
            ),
        },
    )

    repro_handoff = Candidate(
        candidate_id="cand-03",
        requirement_id="req.repro_handoff.01",
        template_id="laptop_reproducible_local_delivery",
        persona_id=persona_id,
        template=templates["laptop_reproducible_local_delivery"],
        bindings={
            "local_entrypoint": "`" + ENTRYPOINT + "` as delivered in the container",
            "reproduction_inputs": "/app/logs with --reference-date 2025-08-12 in a clean container (no session state)",
            "output_contract": "the original 15-row CSV at the path given by --out, identical to /app/summary.csv",
        },
        checker={
            "id": "rubric_judge",
            "version": "1",
            "parameters": {
                "requirement": "req.repro_handoff.01@1",
                # the deterministic half: does the documented path actually reproduce?
                "subcheck": {"id": "clean_room_repro", "version": "1",
                             "parameters": {"workload": "/app/logs", "reference_date": "2025-08-12"}},
                # the judged half: is the handoff itself complete and locatable?
                "evidence_paths": ["/app/summarize.py", "/app/summary.csv"],
                "samples": 3,
            },
        },
        schedule={"trigger_event": "completion_claim", "earliest_turn": 3, "deadline_turn": 6, "priority": 3, "required": False},
        disclosure_markers=["clean container"],
        contract_compat="delivery-only requirement; re-runs the entry point in a fresh container",
        evidence_refs=["env:container is recreated per verification", "tool:docker commit snapshot"],
        depends_on=["req.resource_budget.01@1"],
    )

    return [resource_budget, data_semantics, repro_handoff]


def _manager_candidates(persona_id: str = "manager_no_code") -> List[Candidate]:
    """A persona with ZERO trusted_harness templates, made decidable by the rubric judge.

    Bound to this task's real facts: the business vocabulary maps to /app/summary.csv and
    the delivered entry point, the acceptance scenarios are the registered fixtures, and
    the authorisation boundary is the container's real permission set.
    """
    persona = load_persona(persona_id)
    templates = {t["id"]: t for t in persona["attributes"]["evolved_requirements"]}

    conclusion_only = Candidate(
        candidate_id="cand-m1",
        requirement_id="req.manager_conclusions.01",
        template_id="manager_conclusion_only_delivery",
        persona_id=persona_id,
        template=templates["manager_conclusion_only_delivery"],
        bindings={},  # the template declares no slots
        checker={
            "id": "rubric_judge",
            "version": "1",
            "parameters": {
                "requirement": "req.manager_conclusions.01@1",
                # `evidence_required` asks for the actual work status the messages reference,
                # so the grader gets a deterministic status probe it can cite. The verdict is
                # still LLM-graded; this only gives truthfulness something to be checked against.
                "subcheck": {"id": "deliverable_shape", "version": "1",
                             "parameters": {"deliverable": "/app/summary.csv"}},
                "evidence_paths": ["/app/summary.csv", "/app/summarize.py"],
                # met_if[0] judges the SHAPE of the replies, so the messages are the artifact
                "message_clauses": ["met_if[0]"],
                "samples": 3,
            },
        },
        schedule={"trigger_event": harness_trigger(templates["manager_conclusion_only_delivery"]),
                  "earliest_turn": 2, "deadline_turn": 4, "priority": 1, "required": True},
        # "three points" / "no code" are this persona's own style words, so they are not
        # usable as leak markers (see controller.distinctive); the acceptance disclosure
        # carries the fixture name instead.
        disclosure_markers=["three points"],
        contract_compat=(
            "constrains the form of the messages addressed to the user and the truthfulness of the reported "
            "status; it cannot cancel the original deliverable or the original tests"
        ),
        evidence_refs=[
            "contract:instruction.md (the task's user-visible outcome is the severity report)",
            "env:messages are the only channel to this user",
        ],
    )

    business_acceptance = Candidate(
        candidate_id="cand-m2",
        requirement_id="req.manager_acceptance.01",
        template_id="manager_business_acceptance_and_risk",
        persona_id=persona_id,
        template=templates["manager_business_acceptance_and_risk"],
        bindings={
            "business_cases": (
                "the report a person actually reads: for the log set in /app/logs at reference date 2025-08-12, "
                "the ERROR / WARNING / INFO counts for today, the last 7 days, the last 30 days, month to date and "
                "the whole history, as delivered in /app/summary.csv"
            ),
            "risk_boundary": (
                "what the container's real permissions leave uncovered: no network and no new dependencies, "
                "/app/logs is read-only, the reference date is fixed at 2025-08-12 rather than 'now', and the "
                "registered boundary cases from `python3 /harness/gen_semantic_logs.py --out <DIR>` "
                "(ref-6 / ref-7 / ref-29 / ref-30, the 1st of the month, the last day of the previous month) "
                "are the only ones actually checked"
            ),
        },
        checker={
            "id": "rubric_judge",
            "version": "1",
            "parameters": {
                "requirement": "req.manager_acceptance.01@1",
                # the deterministic half: is the acceptance claim consistent with what ran?
                "subcheck": {"id": "semantic_matrix", "version": "1",
                             "parameters": {"reference_date": SEMANTIC_REF_DATE}},
                "evidence_paths": ["/app/summary.csv", "/app/summarize.py"],
                "message_clauses": ["met_if[1]"],
                "samples": 3,
            },
        },
        schedule={"trigger_event": "completion_claim", "earliest_turn": 3, "deadline_turn": 6, "priority": 2, "required": True},
        disclosure_markers=["gen_semantic_logs.py"],
        contract_compat=(
            "adds a delivery summary bound to the registered acceptance scenarios and to the real permission "
            "boundary; the numbers themselves are still judged by the original tests"
        ),
        evidence_refs=[
            "fixture:logs_semantic@gen_semantic_logs.py",
            "env:no network / no new dependencies / read-only inputs",
            "contract:instruction.md#reference-date",
        ],
        depends_on=["req.manager_conclusions.01@1"],
    )
    return [conclusion_only, business_acceptance]


def _student_candidates(persona_id: str = "student_needing_explanation") -> List[Candidate]:
    """Kept deliberately as a NOT-APPLICABLE example.

    The pool's own episode_binding says the pair is not applicable when disclosable error
    material cannot be provided. This task starts from an empty workspace: there is no old
    buggy code and no error case to trace, so the binder refuses instead of inventing one.
    """
    persona = load_persona(persona_id)
    templates = {t["id"]: t for t in persona["attributes"]["evolved_requirements"]}
    reason = (
        "episode_binding.missing_inputs: no disclosable error scenario or old code exists for this task "
        "(the workspace starts empty), so counterexample and patch-explanation slots cannot be bound"
    )
    out: List[Candidate] = []
    for i, (tid, rid) in enumerate(
        [("student_error_counterexample", "req.student_counterexample.01"),
         ("student_patch_and_boundary_reasoning", "req.student_patch_reasoning.01"),
         ("student_learning_constraints_and_check", "req.student_learning_check.01")],
        start=1,
    ):
        out.append(Candidate(
            candidate_id=f"cand-s{i}",
            requirement_id=rid,
            template_id=tid,
            persona_id=persona_id,
            template=templates[tid],
            bindings={},
            checker={"id": "rubric_judge", "version": "1", "parameters": {"requirement": f"{rid}@1"}},
            schedule={"trigger_event": "first_patch", "earliest_turn": 2, "deadline_turn": 6,
                      "priority": i, "required": False},
            contract_compat="n/a",
            evidence_refs=["binder:not_applicable"],
            applicable=False,
            skip_reason=reason,
        ))
    return out


CANDIDATE_BUILDERS = {
    "laptop_data_scientist": _laptop_candidates,
    "manager_no_code": _manager_candidates,
    "student_needing_explanation": _student_candidates,
}


def build_candidates(persona_id: str = "laptop_data_scientist", max_evolved: int = 2) -> List[Candidate]:
    if persona_id not in CANDIDATE_BUILDERS:
        raise KeyError(
            f"no task binding for persona {persona_id!r}; bound personas: {sorted(CANDIDATE_BUILDERS)}"
        )
    return CANDIDATE_BUILDERS[persona_id](persona_id)


def applicable_candidates(candidates: List[Candidate], max_evolved: int) -> List[Candidate]:
    """The evolved-requirement budget only spends on candidates that actually bound."""
    return [c for c in candidates if c.applicable][: max(0, max_evolved)]


def selected_candidates(candidates: List[Candidate], max_evolved: int) -> List[Candidate]:
    return candidates[:max_evolved]


def skipped_candidates(candidates: List[Candidate], max_evolved: int) -> List[Candidate]:
    return candidates[max_evolved:]
