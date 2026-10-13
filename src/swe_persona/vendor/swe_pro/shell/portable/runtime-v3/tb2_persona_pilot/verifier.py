#!/usr/bin/env python3
"""Isolated outcome verifier + metrics (plan section 6).

Runs on the FINAL snapshot only, in fresh containers:
  * the original Terminal-Bench `tests/test.sh`, unmodified -- the base task
    verdict, which no persona or requirement can relax;
  * the checker of every disclosed, non-superseded evolved requirement;
  * for reporting only, the same evolved checkers on arms where the requirement
    was never disclosed (so a single-turn baseline can be compared without
    counting undisclosed requirements against it).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from checkers import CheckerRegistry, CheckResult
from ledger import Ledger


def verify_outcome(
    ledger: Ledger,
    checkers: CheckerRegistry,
    final_image: str,
    final_digest: str,
    turn: int,
    reference_checkers: Optional[List[Dict[str, Any]]] = None,
    transcript: Optional[List[Dict[str, Any]]] = None,
    skip_rubric: bool = False,
) -> Dict[str, Any]:
    official = checkers.run("official_tests", "1", {}, final_image, f"final:{final_digest}", turn)
    per_requirement: List[Dict[str, Any]] = []
    for entry in ledger.entries.values():
        if entry.activation_status != "active":
            continue
        if skip_rubric and entry.checker.get("id") == "rubric_judge":
            continue
        res = checkers.run(
            entry.checker["id"], entry.checker["version"], entry.checker["parameters"],
            final_image, f"final:{final_digest}", turn,
            context={
                "statement": entry.statement,
                "introduced_turn": entry.introduced_turn,
                "satisfaction": (entry.source or {}).get("satisfaction_policy", {}),
                "transcript": transcript or [],
            },
        )
        ledger.record_check(entry.ref, res.satisfaction(), res.evidence_ref, f"final:{final_digest}", turn)
        per_requirement.append(
            {
                "requirement": entry.ref,
                "statement": entry.statement,
                "introduced_turn": entry.introduced_turn,
                "claimed_by_agent": entry.claimed,
                "checker": res.ref,
                "judge_tier": (entry.source or {}).get("judge_tier"),
                "status": res.satisfaction(),
                "reason_code": res.reason_code,
                "evidence_ref": res.evidence_ref,
                "evidence": res.evidence,
            }
        )
    reference: List[Dict[str, Any]] = []
    for spec in reference_checkers or []:
        res = checkers.run(spec["id"], spec["version"], spec["parameters"], final_image, f"final:{final_digest}", turn)
        reference.append(
            {"checker": res.ref, "parameters": spec["parameters"], "status": res.status,
             "reason_code": res.reason_code, "evidence_ref": res.evidence_ref, "evidence": res.evidence,
             "note": "not disclosed in this arm; reported for comparison only"}
        )
    return {
        "base_task": {
            "status": official.status,
            "reward": official.evidence.get("reward"),
            "evidence_ref": official.evidence_ref,
            "summary_lines": official.evidence.get("summary_lines"),
        },
        "evolved_requirements": per_requirement,
        "undisclosed_reference_checks": reference,
    }


def compute_metrics(
    arm: str,
    outcome: Dict[str, Any],
    ledger: Ledger,
    controller: Any,
    transcript: List[Dict[str, Any]],
    judge: Optional[Dict[str, Any]],
    max_turns: int,
    agent_turns_used: int,
) -> Dict[str, Any]:
    base_ok = outcome["base_task"]["status"] == "pass"
    evolved = outcome["evolved_requirements"]
    met = [e for e in evolved if e["status"] == "met"]
    unknown = [e for e in evolved if e["status"] == "unknown"]
    not_met = [e for e in evolved if e["status"] == "not_met"]

    # opportunity budget: a disclosure needs at least one agent turn after it
    no_opportunity = [
        e["requirement"] for e in evolved
        if e["introduced_turn"] is not None and e["introduced_turn"] >= max_turns
    ]
    claimed_but_not_met = [e["requirement"] for e in evolved if e["claimed_by_agent"] and e["status"] != "met"]
    missing_evidence = [e["requirement"] for e in evolved if e["status"] == "unknown"]

    user_turns = [t for t in transcript if t["role"] == "user"]
    # Verbatim markers catch a bound requirement quoted out of turn; a PARAPHRASED one only shows
    # up in the judge's `discloses_only_activated` item, so it is audited here rather than hidden.
    policy_audit = []
    for item in (judge or {}).get("scores", []) or []:
        if item.get("item") == "discloses_only_activated" and item.get("score", 2) < 2:
            policy_audit.append({"item": item["item"], "score": item["score"], "reason": item.get("reason")})
    invalid_reasons = list(controller.invalid_reasons) + [f"missed_disclosure_deadline:{r}" for r in controller.missed_deadlines(max_turns + 1)]
    required_ids = {c.requirement_id for c in controller.selected if c.schedule.get('required', True)}
    required_ids.update(entry.id for entry in getattr(ledger, 'entries', {}).values()
                        if entry.activation_status == 'active')
    checked_ids = {e['requirement'].rsplit('@', 1)[0] for e in evolved}
    missing_checks = sorted(required_ids - checked_ids)
    if arm == 'progressive_persona':
        if not controller.selected or not evolved:
            invalid_reasons.append('persona_requirements_not_verified')
        invalid_reasons.extend('persona_requirement_check_missing:'+rid for rid in missing_checks)
    episode_valid = not invalid_reasons and not controller.infra_failures and not no_opportunity

    all_evolved_met = bool(evolved) and not missing_checks and len(met) == len(evolved)
    persona_pass = False if not_met else True if all_evolved_met else None
    persona_ok = all_evolved_met if arm == 'progressive_persona' or evolved else True
    joint = bool(base_ok and persona_ok and episode_valid)

    return {
        "arm": arm,
        "base_task_success": int(base_ok),
        "evolved_requirement_success": {
            "disclosed": len(evolved),
            "met": len(met),
            "not_met": len(not_met),
            "unknown": len(unknown),
            "all_met": all_evolved_met,
            "pass": persona_pass,
            "required_ids": sorted(required_ids),
            "missing_check_ids": missing_checks,
            "per_requirement": [
                {"requirement": e["requirement"], "status": e["status"], "reason_code": e["reason_code"],
                 "introduced_turn": e["introduced_turn"], "claimed_by_agent": e["claimed_by_agent"]}
                for e in evolved
            ],
        },
        "interaction_delivery": {
            "agent_turns_used": agent_turns_used,
            "user_turns": len(user_turns),
            "disclosures": len(controller.disclosure_turns),
            "revisions": len(controller.revisions_done),
            "verification_events": len([e for e in controller.events if e["type"] == "requirement_verified"]),
            "evidence_citations": sum(len(t.get("evidence_refs") or []) for t in user_turns),
            "claimed_but_not_met": claimed_but_not_met,
            "end_state": controller.end_state or "incomplete",
            "out_of_policy_disclosure_audit": policy_audit,
        },
        "valid_evaluation_coverage": {
            "episode_valid": episode_valid,
            "not_applicable_candidates": list(controller.not_applicable),
            "pair_applicable": bool(controller.selected),
            "invalid_reasons": invalid_reasons,
            "binding_failures": [r for r in controller.invalid_reasons if r.startswith("binding_failed")],
            "no_opportunity": no_opportunity,
            "infra_failures": list(controller.infra_failures),
            "missing_evidence": missing_evidence,
        },
        "joint_success": int(joint),
        "interaction_judge": ({"total": judge.get("total"), "max_total": judge.get("max_total"),
                               "scores": judge.get("scores"), "overall_note": judge.get("overall_note")}
                              if judge else None),
        "note": "style scores never offset a functional failure: joint_success ignores the judge",
    }
