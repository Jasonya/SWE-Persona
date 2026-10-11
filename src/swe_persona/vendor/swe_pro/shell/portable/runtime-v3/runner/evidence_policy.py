"""Explicit evidence-metadata repair; frozen rubric text remains unchanged."""
from copy import deepcopy

EVIDENCE_POLICY = "persona-v4-full-command-bounded-stdout-v3"
CONVERSATIONAL_CLAUSES = {
    "first_pr_patch_explanation":
        "Provides a specific causal chain and scope boundaries, without presenting a local fix as having resolved all similar problems.",
    "student_patch_and_boundary_reasoning":
        "The boundary-case walkthrough gives the correct path and output, without treating a finite set of cases as an unconditional, complete proof.",
    "compat_focused_handoff":
        "Preserved, intentionally changed, and unverified behavior are clearly separated without unsupported external claims.",
}


def corrected_parameters(template_id, satisfaction, parameters):
    """Authorize the delivered explanation, never execution claims as proof."""
    result = deepcopy(parameters)
    expected = CONVERSATIONAL_CLAUSES.get(template_id)
    if expected is None:
        return result
    clauses = satisfaction.get("met_if", [])
    if len(clauses) < 2 or clauses[1] != expected:
        raise ValueError("Evidence repair does not match the frozen rubric clause")
    result["message_clauses"] = list(dict.fromkeys(
        result.get("message_clauses", []) + ["met_if[1]"]))
    # The companion clause explicitly requires consistency with code/diff/checks.
    # Existing message eligibility must not let prose alone prove that fact.
    factual = ["met_if[0]"]
    if template_id == "student_patch_and_boundary_reasoning":
        factual.append("met_if[1]")
    result["message_clause_requires_artifact"] = list(dict.fromkeys(
        result.get("message_clause_requires_artifact", []) + factual))
    result["evidence_policy"] = EVIDENCE_POLICY
    return result


ACTIVATION_POLICY = "public-disclosure-and-recovery-v2"


def activation_context(introduced_turn, transcript):
    """Public activation time and message chronology, never hidden simulator state."""
    messages = [{k: m[k] for k in ("message_id", "role", "turn") if k in m}
                for m in transcript if m.get("role") in ("user", "agent")]
    return {"policy": ACTIVATION_POLICY, "introduced_turn": introduced_turn,
            "disclosure_message_ids": [m["message_id"] for m in messages
                if m.get("role") == "user" and m.get("turn") == introduced_turn
                and introduced_turn is not None], "message_timeline": messages}
