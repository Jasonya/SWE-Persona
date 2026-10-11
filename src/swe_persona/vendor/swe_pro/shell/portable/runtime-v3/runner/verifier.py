"""Final-review metrics for this pilot, on top of the reused TB2 split.

The TB2 outcome verifier already produces the four metric groups (base task,
evolved requirements, interaction delivery, evaluation validity) and is reused
unchanged.  One field it does not report is the official runner's `reason_code`,
and `batch_report` needs it: an `unknown` base verdict has to be attributed to
the parser or the runner (`official_parser_failed`,
`official_parser_invalid_output`, `checker_timeout`, ...) before the batch can
tell an infrastructure failure apart from a real functional failure.  Rather
than change the shared TB2 module, the reason code is recovered here from the
checker registry's own result list, which is where the official verdict was
recorded in the first place. Local metric aggregation also propagates the
interaction judge's deterministic audit failures into evaluation validity;
style scores still do not determine task success.
"""

from __future__ import annotations

import sys
import copy
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tb2_persona_pilot.verifier import compute_metrics as _base_compute_metrics  # noqa: E402
from tb2_persona_pilot.verifier import verify_outcome as _base_verify_outcome  # noqa: E402


def compute_metrics(arm, outcome, ledger, controller, transcript, judge, max_turns, agent_turns_used):
    metrics = copy.deepcopy(_base_compute_metrics(
        arm, outcome, ledger, controller, transcript, judge, max_turns, agent_turns_used))
    audit_reasons = list((judge or {}).get("episode_invalid_reasons") or [])
    if audit_reasons:
        coverage = metrics["valid_evaluation_coverage"]
        coverage["invalid_reasons"] = sorted(set(coverage.get("invalid_reasons", []) + audit_reasons))
        coverage["episode_valid"] = False
        metrics["joint_success"] = 0
    base_task = (outcome or {}).get("base_task") or {}
    evaluator_errors = [
        f"requirement_evaluator_error:{item.get('requirement')}:{item.get('reason_code')}"
        for item in (outcome or {}).get("evolved_requirements", [])
        if (item.get("evidence") or {}).get("error_category")
    ]
    if evaluator_errors:
        coverage = metrics["valid_evaluation_coverage"]
        coverage["invalid_reasons"] = sorted(set(coverage.get("invalid_reasons", []) + evaluator_errors))
        coverage["episode_valid"] = False
        metrics["joint_success"] = 0
    if base_task.get("status") == "unknown":
        reason = base_task.get("reason_code") or "unknown"
        coverage = metrics["valid_evaluation_coverage"]
        invalid_reason = f"base_evaluation_unknown:{reason}"
        coverage["invalid_reasons"] = sorted(
            set(coverage.get("invalid_reasons", []) + [invalid_reason])
        )
        coverage["episode_valid"] = False
        metrics["joint_success"] = 0
    return metrics


def _official_reason_code(checkers: Any, evidence_ref: Optional[str]) -> Optional[str]:
    """The reason code of the official_tests result the outcome was built from."""
    results = [
        result for result in getattr(checkers, "results", [])
        if getattr(result, "checker_id", None) == "official_tests"
    ]
    if not results:
        return None
    if evidence_ref is not None:
        for result in results:
            if getattr(result, "evidence_ref", None) == evidence_ref:
                return result.reason_code
    return results[-1].reason_code


def verify_outcome(ledger, checkers, final_image, final_digest, turn, reference_checkers=None,
                   transcript=None) -> Dict[str, Any]:
    outcome = _base_verify_outcome(
        ledger, checkers, final_image, final_digest, turn,
        reference_checkers=reference_checkers, transcript=transcript,
    )
    base_task = outcome.get("base_task")
    if isinstance(base_task, dict) and "reason_code" not in base_task:
        reason_code = _official_reason_code(checkers, base_task.get("evidence_ref"))
        if reason_code is not None:
            base_task["reason_code"] = reason_code
    return outcome


__all__ = ["compute_metrics", "verify_outcome"]
