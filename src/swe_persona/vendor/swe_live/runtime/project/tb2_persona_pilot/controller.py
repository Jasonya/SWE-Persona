#!/usr/bin/env python3
"""Deterministic controller (plan sections 4 and 5).

Everything that must not depend on an LLM lives here: snapshotting, typed
events, staleness, checker scheduling and de-duplication, the disclosure
schedule and its deadlines, and validation of the simulator's decision object.
The LLM only picks wording and one allowed action on an observation this class
has already narrowed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from checkers import CheckerRegistry, CheckResult
from dockerx import Container
from ledger import Ledger, LedgerError
import re

from registry import Candidate
from task_adapter import TaskManifest

ROLE_OVERLAYS = {
    "laptop_data_scientist": {
        "id": "resource_aware_local_runner",
        "behaviour": (
            "resource-aware reviewer of a data job: first settle feasibility under the bound limits, "
            "then check that the numbers did not change, then ask for a path you can re-run yourself. "
            "Answer the agent's questions from the public contract, in few words, no tutorials."
        ),
        "forbidden": (
            "inventing a CSV/notebook storyline, inventing memory, CPU or OS numbers, demanding a "
            "different machine, shrinking the input, or adding requirements unrelated to this task's "
            "data processing"
        ),
    },
    "manager_no_code": {
        "id": "conclusion_only_decision_maker",
        "behaviour": (
            "decision-oriented: you want the conclusion, the current status and what you must decide, in "
            "business words. You never ask for code, SQL, commands or logs, and you never let 'no code' "
            "cancel the actual fix or its verification. You state plainly when a reported status is not yet "
            "backed by evidence."
        ),
        "forbidden": (
            "fabricating a release incident, a customer complaint or a business loss; authorising a release, "
            "a data migration or work by an external team; waiving the original functional task"
        ),
    },
    "student_needing_explanation": {
        "id": "explanation_on_demand_learner",
        "behaviour": (
            "explanation-on-demand: you want the error path first and the patch reasoning second, one small "
            "verifiable example at a time, and explanations are deliverables rather than style."
        ),
        "forbidden": (
            "inventing course rules or learning constraints on the fly, accepting a tutorial in place of a "
            "patch, or using your own level of understanding as the success condition"
        ),
    },
}

COMMUNICATION_STYLE = {
    "verbosity": "terse",
    "explanation": "summary_only",
    "ordering": "resource feasibility -> result check -> re-runnable handoff",
    "length": "at most 6 short sentences, no code blocks unless quoting a command",
}


@dataclass
class TurnRecord:
    turn: int
    user_message_id: str
    user_message: str
    decision: Dict[str, Any]
    rejections: List[List[str]]
    agent: Dict[str, Any]
    snapshot: Dict[str, Any]
    events: List[Dict[str, Any]]
    checks: List[Dict[str, Any]]
    observation: Dict[str, Any]

    def to_json(self) -> Dict[str, Any]:
        return dict(self.__dict__)


# Phrases that assert an outcome the harness has ALREADY verified. Deliberately
# narrow: a disclosure has to quote its own requirement text ("give me the
# measured peak", "peak RSS <= 128 MiB"), and quoting a requirement is not an
# assertion about a result.
VERIFICATION_ASSERTION_RE = re.compile(
    r"(within budget|over budget|still fail(s|ing)|acceptance failed|checks? (confirm|show|say)"
    r"|independent check|verified gap|passes exactly|already verified|did not pass|came back)",
    re.IGNORECASE,
)
SENTENCE_RE = re.compile(r"[.!?](?:\s|$)")


def distinctive(marker: str) -> bool:
    """Can this bound string be used to detect a leaked requirement?

    Only hard identifiers may: a path, a flag, a file name, a date/number, or a long
    phrase. Soft phrases are rejected because some personas' requirements ARE their
    speaking style ("no code", "three points") -- blocking those would stop the persona
    from stating its own opening preference, which layer 2 explicitly allows.
    """
    if any(tok in marker for tok in ("/", ".py", "--", ".sh", ".md")):
        return True
    if any(ch.isdigit() for ch in marker):
        return True
    return len(marker.split()) >= 4


@dataclass
class Controller:
    manifest: TaskManifest
    ledger: Ledger
    checkers: CheckerRegistry
    candidates: List[Candidate]
    selected: List[Candidate]
    run_dir: Path
    max_turns: int = 8
    entrypoint_path: str = "/app/summarize.py"
    snapshot_tag_prefix: str = "tb2pilot-snap"

    events: List[Dict[str, Any]] = field(default_factory=list)
    fired: Dict[str, int] = field(default_factory=dict)
    initial_digest: Optional[str] = None
    current_digest: Optional[str] = None
    current_image: Optional[str] = None
    disclosure_turns: Dict[str, int] = field(default_factory=dict)
    revisions_done: Dict[str, int] = field(default_factory=dict)
    invalid_reasons: List[str] = field(default_factory=list)
    not_applicable: List[Dict[str, str]] = field(default_factory=list)
    latest_probe: Dict[str, Any] = field(default_factory=dict)
    infra_failures: List[str] = field(default_factory=list)
    end_state: Optional[str] = None
    last_transcript: List[Dict[str, Any]] = field(default_factory=list)
    _obs_counter: int = 0

    # ---- registration ---------------------------------------------------
    def register_candidates(self, validation_report: Optional[Dict[str, Any]] = None) -> None:
        proof = self._achievability_proof(validation_report)
        for cand in self.candidates:
            entry = self.ledger.add(cand.to_entry())
            if not cand.applicable:
                self.ledger.set_activation(entry.ref, "not_applicable", reason=cand.skip_reason)
                self.not_applicable.append({"requirement": entry.ref, "reason": cand.skip_reason})
                continue
            self.ledger.set_activation(entry.ref, "pending_validation", reason="offline_validation_started")
            problems = self._validate_candidate(cand, proof)
            if problems:
                self.ledger.set_activation(entry.ref, "not_applicable", reason="; ".join(problems))
                self.invalid_reasons.append(f"binding_failed:{entry.ref}:{problems[0]}")
                continue
            entry.source["achievability_proof"] = proof
            self.ledger.set_activation(entry.ref, "validated_undisclosed", reason="offline_validated")
            if cand not in self.selected:
                self.ledger.set_activation(entry.ref, "skipped", reason="evolved_requirement_budget")

    def _validate_candidate(self, cand: Candidate, proof: Dict[str, Any]) -> List[str]:
        problems: List[str] = []
        if not self.checkers.registered(cand.checker["id"], cand.checker["version"]):
            problems.append(f"checker_not_registered:{cand.checker['id']}")
        try:
            cand.statement()
        except KeyError as exc:
            problems.append(f"unbound_slot:{exc}")
        if not cand.evidence_refs:
            problems.append("no_source_evidence")
        if cand in self.selected and proof.get(cand.checker["id"]) != "pass":
            problems.append(f"not_proven_achievable:{cand.checker['id']}")
        return problems

    @staticmethod
    def _achievability_proof(validation_report: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Reference arms of the two offline gates are the achievability proof.

        `validate_bindings.py::reference_streaming` covers the deterministic checkers;
        `validate_rubric.py::complete_handoff` covers `rubric_judge`.
        """
        proof: Dict[str, Any] = {}
        sources: List[str] = []
        if validation_report:
            arm = validation_report.get("arms", {}).get("reference_streaming", {})
            proof.update({cid: res["status"] for cid, res in arm.get("checks", {}).items()})
            sources.append("validate_bindings.py::reference_streaming")
        rubric_path = Path(__file__).resolve().parent / "runs" / "validation" / "rubric_report.json"
        if rubric_path.is_file():
            report = json.loads(rubric_path.read_text(encoding="utf-8"))
            positive = report.get("arms", {}).get("complete_handoff", {})
            if positive.get("status") == "pass":
                proof["rubric_judge"] = "pass"
                sources.append("validate_rubric.py::complete_handoff")
        proof["source"] = " + ".join(sources)
        return proof

    # ---- snapshots, probes, verification --------------------------------
    def snapshot(self, container: Container, turn: int) -> Tuple[str, str, bool]:
        digest = container.snapshot_digest()
        tag = f"{self.snapshot_tag_prefix}-t{turn}:latest"
        container.commit(tag)
        changed = self.current_digest is not None and digest != self.current_digest
        if self.initial_digest is None:
            self.initial_digest = digest
        self.current_digest, self.current_image = digest, tag
        if changed:
            staled = self.ledger.mark_stale_for_snapshot(digest, turn)
            if staled:
                self.add_event("evidence_stale", turn, {"requirements": staled, "snapshot": digest})
        return tag, digest, changed

    def add_event(self, event_type: str, turn: int, detail: Dict[str, Any]) -> Dict[str, Any]:
        ev = {"id": f"ev-{len(self.events) + 1:03d}", "type": event_type, "turn": turn, "detail": detail}
        self.events.append(ev)
        self.fired.setdefault(event_type, turn)
        return ev

    def run_probes(self, turn: int) -> List[CheckResult]:
        """Cheap, deterministic probes on the immutable snapshot."""
        out: List[CheckResult] = []
        shape = self.checkers.run("deliverable_shape", "1", {"deliverable": self.manifest.deliverables[0]},
                                  self.current_image, self.current_digest, turn)
        out.append(shape)
        self.latest_probe["deliverable_shape"] = shape.status
        if shape.status == "pass" and "deliverable_present" not in self.fired:
            self.add_event("deliverable_present", turn, {"path": self.manifest.deliverables[0]})
        elif shape.reason_code == "shape_violation" and "deliverable_shape_violation" not in self.fired:
            self.add_event("deliverable_shape_violation", turn, {"evidence_ref": shape.evidence_ref})
        ep = self.checkers.run("entrypoint_present", "1", {"entrypoint_path": self.entrypoint_path},
                               self.current_image, self.current_digest, turn)
        out.append(ep)
        self.latest_probe["entrypoint_present"] = ep.status
        if ep.status == "pass":
            if "entrypoint_present" not in self.fired:
                self.add_event("entrypoint_present", turn, {"path": self.entrypoint_path})
            probe = self.checkers.run(
                "probe_reference_date", "1", {"reference_date": "2025-08-13", "alt_reference_date": "2025-08-06"},
                self.current_image, self.current_digest, turn,
            )
            out.append(probe)
            ignored = bool(probe.evidence.get("reference_date_ignored"))
            self.latest_probe["reference_date_ignored"] = ignored
            if ignored and "reference_date_ignored" not in self.fired:
                self.add_event("reference_date_ignored", turn, {"evidence_ref": probe.evidence_ref})
        else:
            self.latest_probe["reference_date_ignored"] = None
        return out

    def verify_active(self, turn: int, transcript: Optional[List[Dict[str, Any]]] = None) -> List[CheckResult]:
        """Trusted evidence for every active requirement lacking fresh proof.

        A `rubric_judge` requirement is graded on the artifacts AND the messages, so it is
        re-submitted every turn; the checker cache keys on the transcript digest, so an
        unchanged conversation costs nothing.
        """
        results: List[CheckResult] = []
        for entry in self.ledger.active():
            is_rubric = entry.checker["id"] == "rubric_judge"
            fresh = entry.satisfaction_status in ("met", "not_met") and entry.verified_snapshot == self.current_digest
            if fresh and not is_rubric:
                continue
            res = self.checkers.run(
                entry.checker["id"], entry.checker["version"], entry.checker["parameters"],
                self.current_image, self.current_digest, turn,
                context=self.checker_context(entry, transcript),
            )
            results.append(res)
            if res.status == "unknown" and res.reason_code in ("driver_no_verdict", "checker_error"):
                self.infra_failures.append(f"{entry.ref}:{res.reason_code}")
            try:
                self.ledger.record_check(entry.ref, res.satisfaction(), res.evidence_ref, self.current_digest, turn)
            except LedgerError as exc:
                self.infra_failures.append(f"ledger:{exc}")
            self.add_event(
                "requirement_verified", turn,
                {"requirement": entry.ref, "status": res.satisfaction(), "evidence_ref": res.evidence_ref,
                 "reason_code": res.reason_code},
            )
        return results

    def checker_context(self, entry, transcript: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
        """What an LLM-judged checker is allowed to see: the rubric, the statement, the messages."""
        return {
            "statement": entry.statement,
            "satisfaction": (entry.source or {}).get("satisfaction_policy", {}),
            "transcript": [
                {k: v for k, v in m.items() if k in ("role", "message_id", "turn", "status", "text")}
                for m in (transcript or [])
            ],
        }

    # ---- schedule -------------------------------------------------------
    def _candidate_for(self, requirement_id: str) -> Optional[Candidate]:
        for cand in self.candidates:
            if cand.requirement_id == requirement_id:
                return cand
        return None

    def due_item(self, turn: int) -> Optional[Dict[str, Any]]:
        """At most one disclosure per turn: a revision first, then the next requirement."""
        for cand in self.selected:
            if not cand.revision:
                continue
            base_ref = f"{cand.requirement_id}@1"
            if base_ref not in self.ledger.entries:
                continue
            entry = self.ledger.entries[base_ref]
            rev = cand.revision
            already = any(e.supersedes == [base_ref] for e in self.ledger.entries.values())
            triggered = rev["trigger_event"] in self.fired and (
                self.latest_probe.get("reference_date_ignored") is not False
            )
            if (
                entry.activation_status == "active"
                and not already
                and triggered
                and rev["earliest_turn"] <= turn <= rev["deadline_turn"]
            ):
                return {
                    "operation": "revise",
                    "requirement_ref": f"{cand.requirement_id}@{entry.version + 1}",
                    "replaces": [base_ref],
                    "statement": entry.statement + "\n" + rev["addendum"],
                    "must_include_verbatim": rev.get("markers", []),
                    "reason_code": rev["reason_code"],
                    "trigger_event": rev["trigger_event"],
                    "candidate_id": cand.candidate_id,
                }
        for cand in self.selected:
            ref = f"{cand.requirement_id}@1"
            entry = self.ledger.entries.get(ref)
            if entry is None or entry.activation_status != "validated_undisclosed":
                continue
            sched = cand.schedule
            if turn < sched["earliest_turn"]:
                continue
            if any(self.ledger.entries[d].activation_status not in ("active", "superseded") for d in entry.depends_on):
                continue
            triggered = sched["trigger_event"] in self.fired
            forced = sched["required"] and turn >= sched["deadline_turn"]
            if triggered or forced:
                return {
                    "operation": "add",
                    "requirement_ref": ref,
                    "replaces": [],
                    "statement": entry.statement,
                    "must_include_verbatim": cand.disclosure_markers,
                    "reason_code": "event_trigger_matched" if triggered else "deadline_forced",
                    "trigger_event": sched["trigger_event"],
                    "candidate_id": cand.candidate_id,
                }
        return None

    def missed_deadlines(self, turn: int) -> List[str]:
        missed = []
        for cand in self.selected:
            ref = f"{cand.requirement_id}@1"
            entry = self.ledger.entries.get(ref)
            if entry is None:
                continue
            if entry.activation_status == "validated_undisclosed" and cand.schedule["required"] and turn > cand.schedule["deadline_turn"]:
                missed.append(ref)
        return missed

    # ---- observation ----------------------------------------------------
    def end_blockers(self, turn: int) -> List[str]:
        blockers: List[str] = []
        if self.due_item(turn):
            blockers.append("a requirement disclosure is due this turn")
        for cand in self.selected:
            entry = self.ledger.entries.get(f"{cand.requirement_id}@1")
            if entry is not None and entry.activation_status == "validated_undisclosed" and cand.schedule["required"]:
                blockers.append(f"required requirement {entry.ref} not disclosed yet")
        for entry in self.ledger.active():
            if entry.satisfaction_status != "met":
                blockers.append(f"{entry.ref} is {entry.satisfaction_status}, not met")
        if "deliverable_present" not in self.fired:
            blockers.append(f"{self.manifest.deliverables[0]} does not exist yet")
        return blockers

    def build_observation(
        self,
        turn: int,
        agent_turn: Optional[Dict[str, Any]],
        snapshot_files: List[Dict[str, str]],
        snapshot_changed: bool,
        probe_results: List[CheckResult],
        check_results: List[CheckResult],
    ) -> Dict[str, Any]:
        self._obs_counter += 1
        due = self.due_item(turn)
        blockers = self.end_blockers(turn)
        allowed = ["answer", "feedback", "request_verification", "wait"]
        if due:
            allowed = ["disclose"]
        elif not blockers:
            allowed.append("end")
        public = self.manifest.public_view()
        public.pop("environment", None)
        obs = {
            "observation_id": f"obs-{self._obs_counter:03d}",
            "turn": turn,
            "max_turns": self.max_turns,
            "turns_left": self.max_turns - turn + 1,
            "public_contract": public,
            "agent_message": agent_turn,
            "artifact_snapshot": {
                "digest": self.current_digest,
                "changed_since_previous_turn": snapshot_changed,
                "files": snapshot_files,
            },
            "typed_events": self.events[-12:],
            "probe_results": [r.public_summary() for r in probe_results],
            "verification_results": [r.public_summary() for r in check_results],
            "ledger": self.ledger.simulator_view(),
            "validated_undisclosed_count": len([e for e in self.ledger.undisclosed()]),
            "schedule": {
                "requirement_due_this_turn": bool(due),
                "disclosure_budget_per_turn": 1,
                "last_disclosure_turn_allowed": max(
                    (c.schedule["deadline_turn"] for c in self.selected), default=self.max_turns
                ),
                "disclosures_so_far": self.disclosure_turns,
            },
            "requirement_to_disclose": due,
            "allowed_actions": allowed,
            "end_allowed": "end" in allowed,
            "end_blockers": blockers,
            "registered_checkers": sorted({f"{cid}@{ver}" for cid, ver in self.checkers.DRIVER_MODES}),
        }
        self.manifest.assert_no_leak(json.dumps(obs, ensure_ascii=False), "simulator observation")
        return obs

    # ---- decision validation & application ------------------------------
    def validate_decision(self, decision: Dict[str, Any], obs: Dict[str, Any]) -> List[str]:
        errs: List[str] = []
        if decision.get("turn_id") != obs["turn"]:
            errs.append(f"turn_id must be {obs['turn']}")
        if decision.get("observation_id") != obs["observation_id"]:
            errs.append(f"observation_id must be {obs['observation_id']}")
        action = decision.get("action")
        if action not in obs["allowed_actions"]:
            errs.append(f"action {action!r} is not allowed this turn; allowed: {obs['allowed_actions']}")
        msg = (decision.get("user_message") or "").strip()
        if not msg:
            errs.append("user_message must not be empty")
        if len(msg) > 2000:
            errs.append("user_message must be at most 2000 characters")

        due = obs["requirement_to_disclose"]
        discl = decision.get("disclosures") or []
        if due:
            if len(discl) != 1:
                errs.append("exactly one disclosure is required this turn")
            else:
                d = discl[0]
                if d.get("requirement_ref") != due["requirement_ref"]:
                    errs.append(f"disclosure must be {due['requirement_ref']}")
                if d.get("operation") != due["operation"]:
                    errs.append(f"disclosure operation must be {due['operation']}")
                if list(d.get("replaces") or []) != list(due["replaces"]):
                    errs.append(f"disclosure replaces must be {due['replaces']}")
            for marker in due["must_include_verbatim"]:
                if marker not in msg:
                    errs.append(f"user_message must contain the bound string {marker!r} verbatim")
        elif discl:
            errs.append("no requirement is due this turn; disclosures must be empty")

        for req in decision.get("verification_requests") or []:
            if not self.checkers.registered(req.get("checker_id", ""), req.get("version", "")):
                errs.append(f"checker {req.get('checker_id')}@{req.get('version')} is not registered")
            ref = req.get("requirement_ref")
            if ref not in self.ledger.entries or self.ledger.entries[ref].activation_status != "active":
                errs.append(f"verification_requests may only target an active requirement, got {ref!r}")
            try:
                params = json.loads(req.get("parameters") or "{}")
                if not isinstance(params, dict):
                    raise ValueError
            except (json.JSONDecodeError, ValueError):
                errs.append("parameters must be a JSON object")

        for upd in decision.get("ledger_updates") or []:
            ref = upd.get("requirement_ref")
            if ref not in self.ledger.entries or self.ledger.entries[ref].activation_status != "active":
                errs.append(f"ledger_updates may only mark claims on an active requirement, got {ref!r}")
            if upd.get("field") != "claimed":
                errs.append("the only ledger field you may set is `claimed`")

        # a message that asserts a verification outcome must cite the evidence it came from
        cited = [r for r in (decision.get("evidence_refs") or []) if str(r).startswith("chk-")]
        citable = {r.get("evidence_ref") for r in obs["verification_results"] + obs["probe_results"]}
        if citable and VERIFICATION_ASSERTION_RE.search(msg) and not cited:
            errs.append(
                "this user_message asserts a verification outcome, so evidence_refs must cite the chk-### "
                "result(s) it comes from"
            )

        # style budget from the persona's communication_style
        sentences = len([m for m in SENTENCE_RE.finditer(msg)])
        cap = 12 if due else 6
        if sentences > cap:
            errs.append(f"communication_style allows at most {cap} sentences this turn, got {sentences}")

        # never leak a requirement that is not due: no undisclosed candidate's bound markers.
        # Markers shared by several candidates are ambiguous and cannot block either.
        shared = {
            mk for i, a in enumerate(self.candidates) for b in self.candidates[i + 1:]
            for mk in set(a.disclosure_markers) & set(b.disclosure_markers)
        }
        for cand in self.candidates:
            ref = f"{cand.requirement_id}@1"
            entry = self.ledger.entries.get(ref)
            if entry is None or entry.activation_status in ("active", "superseded", "not_applicable"):
                continue
            if due and due["requirement_ref"].startswith(cand.requirement_id):
                continue
            hits = [
                mk for mk in cand.disclosure_markers
                if mk.lower() in msg.lower() and distinctive(mk) and mk not in shared
            ]
            if hits:
                errs.append(
                    f"requirement {ref} is {entry.activation_status}, not disclosed: drop {hits} from user_message"
                )

        known_refs = {r["evidence_ref"] for r in obs["probe_results"] + obs["verification_results"] if r.get("evidence_ref")}
        known_refs |= {e["id"] for e in obs["typed_events"]}
        for ref in decision.get("evidence_refs") or []:
            if ref not in known_refs:
                errs.append(f"evidence_ref {ref!r} is not in this observation")

        if action == "end":
            if not obs["end_allowed"]:
                errs.append(f"ending is blocked: {obs['end_blockers']}")
            if decision.get("end_state") != "completed_pending_final":
                errs.append("end_state must be completed_pending_final when action is end")
        elif decision.get("end_state") not in (None, "null"):
            errs.append("end_state must be null unless action is end")
        return errs

    def apply_decision(self, decision: Dict[str, Any], obs: Dict[str, Any], turn: int) -> List[CheckResult]:
        due = obs["requirement_to_disclose"]
        if due:
            if due["operation"] == "add":
                self.ledger.disclose(due["requirement_ref"], turn=turn, reason=due["reason_code"])
            else:
                cand = self._candidate_for(due["requirement_ref"].split("@")[0])
                self.ledger.revise(
                    due["replaces"][0], new_statement=due["statement"], turn=turn,
                    reason=due["reason_code"], evidence_refs=list(decision.get("evidence_refs") or []),
                )
                self.revisions_done[due["requirement_ref"]] = turn
            self.disclosure_turns[due["requirement_ref"]] = turn
            self.add_event("requirement_disclosed", turn,
                           {"requirement": due["requirement_ref"], "operation": due["operation"],
                            "reason_code": due["reason_code"]})
        for upd in decision.get("ledger_updates") or []:
            if str(upd.get("value")).lower() in ("true", "1", "yes"):
                self.ledger.record_claim(upd["requirement_ref"], turn, message_id=f"a-{turn - 1}")
        extra: List[CheckResult] = []
        for req in decision.get("verification_requests") or []:
            entry = self.ledger.entries[req["requirement_ref"]]
            params = dict(entry.checker["parameters"])
            params.update(json.loads(req.get("parameters") or "{}"))
            res = self.checkers.run(req["checker_id"], req["version"], params,
                                    self.current_image, self.current_digest, turn,
                                    context=self.checker_context(entry, self.last_transcript))
            extra.append(res)
            if req["checker_id"] == entry.checker["id"]:
                try:
                    self.ledger.record_check(entry.ref, res.satisfaction(), res.evidence_ref, self.current_digest, turn)
                except LedgerError as exc:
                    self.infra_failures.append(f"ledger:{exc}")
        if decision.get("action") == "end":
            self.end_state = decision.get("end_state")
        return extra
