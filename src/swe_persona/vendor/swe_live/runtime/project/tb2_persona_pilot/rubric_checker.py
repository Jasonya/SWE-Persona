#!/usr/bin/env python3
"""LLM-as-judge checker for persona requirements that no deterministic oracle can decide.

Design constraints, all of them reactions to how an LLM grader normally fails:

* **The rubric is not written by the grader.** The clauses are the template's own
  `satisfaction.met_if / not_met_if / unknown_if`, verbatim from the persona pool.
* **The LLM only grades one clause at a time against grounded facts.** It returns
  `satisfied / violated / unsupported` per clause plus the grounding it used. The
  met/not_met/unknown aggregation is done here, deterministically, so the verdict
  rule is auditable and identical across requirements.
* **Prose is not evidence.** Grounding must point at a real artifact in the
  snapshot bundle (path + sha256 collected by `checker_driver --mode
  evidence_bundle`) or at a deterministic sub-check result. A clause supported
  only by the agent's own message is downgraded to `unsupported`, which is how
  "a verbal completion statement is at most `claimed`" is enforced for
  LLM-judged requirements too.
* **Fabricated references cannot pass.** Every `ref` is checked against the
  bundle listing / sub-check id / transcript message ids before it counts.
* **Disagreement is not a verdict.** k independent samples; a clause without a
  strict majority becomes `unsupported`, and the agreement rate is reported so
  judge reliability is measurable instead of assumed.
* **A failing deterministic half wins.** In hybrid mode the sub-check can only
  lower the verdict, never raise it.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

CLAUSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["clauses", "overall_note"],
    "properties": {
        "clauses": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["clause_id", "verdict", "grounding", "reason"],
                "properties": {
                    "clause_id": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["satisfied", "violated", "unsupported"]},
                    "grounding": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["kind", "ref", "quote"],
                            "properties": {
                                "kind": {"type": "string", "enum": ["artifact", "subcheck", "agent_message"]},
                                "ref": {"type": "string"},
                                "quote": {"type": "string"},
                            },
                        },
                    },
                    "reason": {"type": "string"},
                },
            },
        },
        "overall_note": {"type": "string"},
    },
}

SYSTEM = """You are grading ONE requirement of a coding session against a fixed rubric. You are not the user, you are not the agent, and you do not decide the overall outcome: you grade each clause separately and the harness aggregates.

For every clause return exactly one verdict:
  satisfied   - the clause is true, and you can point at grounding that shows it
  violated    - the clause is false, and you can point at grounding that shows it
  unsupported - you cannot tell from the given material

Grounding rules, which override everything else:
- `artifact` grounding must use a path that appears verbatim in the snapshot listing, and your quote must come from that file's text as provided.
- `subcheck` grounding must use the sub-check id given to you, and only for what that sub-check actually reports.
- `agent_message` grounding is a CLAIM, not evidence. A clause about what the code, files or runs actually do may NOT be called satisfied on the strength of agent messages alone: mark it `unsupported` instead. The exception is a clause whose subject is the conversation itself (how a reply is written, what it contains) -- there the messages are what is being judged, and the harness knows which clauses those are.
- Never invent a path, a file, a command output or a number. If the material does not contain it, the clause is `unsupported`.
- Fluent, confident or well-organised prose is not evidence of anything.
- **Existence is not content.** Citing a file proves only that the file exists. If the clause requires that
  instructions, documentation, parameters or answers EXIST somewhere, you must quote the text that actually
  contains them; an implementation file that merely does the work does not satisfy a clause about documented
  instructions, and a listing entry never does.
- **A confident restatement is still a claim.** If the clause requires that something was run, measured,
  recounted or verified, you need a sub-check result or an artifact that records the run. An agent asserting it --
  however precisely, however many times -- is `unsupported`.
- Your quote must be sufficient on its own. If a reader who sees only your quote could not agree the clause holds,
  the verdict is `unsupported`, not `satisfied`.

Judge only the clause in front of you, against this task's real artifacts. Do not import your own standards of code quality, and do not reward or punish style."""


@dataclass
class RubricSpec:
    """The bound rubric for one requirement."""

    met_if: List[str]
    not_met_if: List[str] = field(default_factory=list)
    unknown_if: List[str] = field(default_factory=list)
    evidence_required: List[str] = field(default_factory=list)
    evidence_paths: List[str] = field(default_factory=list)
    subcheck: Optional[Dict[str, Any]] = None
    samples: int = 3
    # Clauses whose subject IS the conversation (e.g. "each manager-facing reply has at
    # most three points, no code"). For those the messages are the artifact under
    # judgement, so agent_message grounding is legitimate. Declared at binding time by
    # the author, never chosen by the grader.
    message_grounded_clauses: List[str] = field(default_factory=list)

    @classmethod
    def from_template(
        cls,
        satisfaction: Dict[str, Any],
        evidence_paths: Optional[List[str]] = None,
        subcheck: Optional[Dict[str, Any]] = None,
        samples: int = 3,
        message_grounded_clauses: Optional[List[str]] = None,
    ) -> "RubricSpec":
        return cls(
            met_if=list(satisfaction.get("met_if") or []),
            not_met_if=list(satisfaction.get("not_met_if") or []),
            unknown_if=list(satisfaction.get("unknown_if") or []),
            evidence_required=list(satisfaction.get("evidence_required") or []),
            evidence_paths=list(evidence_paths or []),
            subcheck=subcheck,
            samples=samples,
            message_grounded_clauses=list(message_grounded_clauses or []),
        )

    def clause_index(self) -> List[Tuple[str, str, str]]:
        out = []
        for kind, clauses in (("met_if", self.met_if), ("not_met_if", self.not_met_if), ("unknown_if", self.unknown_if)):
            for i, text in enumerate(clauses):
                out.append((f"{kind}[{i}]", kind, text))
        return out


def _valid_refs(bundle: Dict[str, Any], transcript: List[Dict[str, Any]], subcheck_id: Optional[str]) -> Dict[str, set]:
    return {
        "artifact": {f["path"] for f in bundle.get("files", [])} | {f["path"] for f in bundle.get("listing", [])},
        "subcheck": {subcheck_id} if subcheck_id else set(),
        "agent_message": {m.get("message_id") for m in transcript if m.get("role") == "agent"},
    }


def _prompt(statement: str, spec: RubricSpec, bundle: Dict[str, Any], transcript: List[Dict[str, Any]],
            subcheck: Optional[Dict[str, Any]]) -> str:
    clauses = [{"clause_id": cid, "group": grp, "clause": text} for cid, grp, text in spec.clause_index()]
    payload = {
        "requirement_statement": statement,
        "clauses_to_grade": clauses,
        "evidence_required_by_the_rubric": spec.evidence_required,
        "snapshot_listing": bundle.get("listing", []),
        "snapshot_files": bundle.get("files", []),
        "paths_the_requirement_named_that_are_missing": bundle.get("missing_requested_paths", []),
        "deterministic_subcheck": subcheck,
        "agent_messages": [
            {"message_id": m.get("message_id"), "status": m.get("status"), "text": m.get("text")}
            for m in transcript if m.get("role") == "agent"
        ],
        "user_messages": [
            {"message_id": m.get("message_id"), "text": m.get("text")}
            for m in transcript if m.get("role") == "user"
        ],
        "what_you_do_not_have": [
            "the hidden tests and their expected values",
            "any reference solution",
            "anything not listed above",
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=1)


def grade(
    judge_llm,
    requirement_ref: str,
    statement: str,
    spec: RubricSpec,
    bundle: Dict[str, Any],
    transcript: List[Dict[str, Any]],
    subcheck: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str, Dict[str, Any]]:
    """Return (status, reason_code, evidence) where status is pass|fail|unknown."""
    clause_ids = [cid for cid, _grp, _t in spec.clause_index()]
    if not spec.met_if:
        return "unknown", "rubric_has_no_met_clause", {"requirement": requirement_ref}

    subcheck_id = f"subcheck:{subcheck['checker']}" if subcheck else None
    allowed = _valid_refs(bundle, transcript, subcheck_id)
    prompt = _prompt(statement, spec, bundle, transcript,
                     dict(subcheck, id=subcheck_id) if subcheck else None)

    samples: List[Dict[str, Any]] = []
    for i in range(max(1, spec.samples)):
        out = judge_llm.json_obj(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            CLAUSE_SCHEMA, schema_name="rubric_grading",
            role=f"rubric.{requirement_ref}.sample{i + 1}", max_output_tokens=4000,
        )
        samples.append(out)

    # ---- per-clause majority, with fabricated refs dropped -----------------
    invalid_refs: List[Dict[str, str]] = []
    per_clause: Dict[str, Dict[str, Any]] = {}
    for cid, grp, text in spec.clause_index():
        votes: List[str] = []
        grounding: List[Dict[str, str]] = []
        reasons: List[str] = []
        for out in samples:
            item = next((c for c in out.get("clauses", []) if c.get("clause_id") == cid), None)
            if item is None:
                votes.append("unsupported")
                continue
            kept = []
            for g in item.get("grounding", []):
                if g.get("ref") in allowed.get(g.get("kind"), set()):
                    kept.append(g)
                else:
                    invalid_refs.append({"clause_id": cid, "kind": g.get("kind", "?"), "ref": g.get("ref", "")})
            verdict = item.get("verdict", "unsupported")
            # prose alone cannot establish a met_if clause
            if (
                grp == "met_if"
                and verdict == "satisfied"
                and cid not in spec.message_grounded_clauses
                and not any(g["kind"] in ("artifact", "subcheck") for g in kept)
            ):
                verdict = "unsupported"
            votes.append(verdict)
            grounding.extend(kept)
            if item.get("reason"):
                reasons.append(item["reason"])
        counts = Counter(votes)
        top, n = counts.most_common(1)[0]
        verdict = top if n > len(votes) / 2 else "unsupported"
        per_clause[cid] = {
            "group": grp, "clause": text, "verdict": verdict, "votes": dict(counts),
            "agreement": round(n / max(1, len(votes)), 2),
            "grounding": grounding[:6], "reasons": reasons[:3],
        }

    # ---- deterministic aggregation ----------------------------------------
    def verdicts(group: str) -> List[str]:
        return [c["verdict"] for c in per_clause.values() if c["group"] == group]

    status, reason = "unknown", "insufficient_evidence"
    if "satisfied" in verdicts("not_met_if"):
        status, reason = "fail", "rubric_violation_clause_satisfied"
    elif "violated" in verdicts("met_if"):
        status, reason = "fail", "rubric_met_clause_violated"
    elif "satisfied" in verdicts("unknown_if"):
        status, reason = "unknown", "rubric_unknown_clause_satisfied"
    elif verdicts("met_if") and all(v == "satisfied" for v in verdicts("met_if")):
        status, reason = "pass", "rubric_all_met_clauses_grounded"

    # the deterministic half can only lower the verdict
    if subcheck:
        if subcheck.get("status") == "fail":
            status, reason = "fail", f"subcheck_failed:{subcheck.get('reason_code')}"
        elif subcheck.get("status") == "unknown" and status == "pass":
            status, reason = "unknown", f"subcheck_unknown:{subcheck.get('reason_code')}"

    agreements = [c["agreement"] for c in per_clause.values()]
    evidence = {
        "requirement": requirement_ref,
        "clauses": per_clause,
        "samples": len(samples),
        "judge_agreement_mean": round(sum(agreements) / max(1, len(agreements)), 2),
        "clauses_without_majority": [cid for cid, c in per_clause.items() if c["agreement"] <= 0.5],
        "invalid_grounding_refs": invalid_refs[:10],
        "subcheck": subcheck,
        "snapshot_files_seen": [f["path"] for f in bundle.get("files", [])],
        "snapshot_file_count": bundle.get("file_count"),
        "bundle_digest": hashlib.sha256(
            json.dumps(bundle.get("listing", []), sort_keys=True).encode()
        ).hexdigest()[:12],
        "notes": [out.get("overall_note", "")[:300] for out in samples][:3],
        "clause_ids": clause_ids,
    }
    return status, reason, evidence
