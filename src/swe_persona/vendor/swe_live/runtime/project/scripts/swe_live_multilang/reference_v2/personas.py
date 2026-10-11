"""Offline bindings for the two preregistered, cross-language study personas."""
import bootstrap
import json
from registry import Candidate, harness_trigger, load_persona
from registry import PERSONA_FILE

PERSONAS = ("manager_no_code", "first_time_pr_contributor")
POOL_PERSONAS = tuple(p["id"] for p in json.loads(PERSONA_FILE.read_text(encoding="utf-8"))["personas"])
VALIDATION_CATALOG = (
    "repository-local checks discovered from the public repository and actually recorded in the "
    "harness command log (report exact commands, observed results, and unrun checks separately); "
    "the original official held-out suite is unavailable during interaction and runs only after "
    "delivery in an isolated verifier, so its result is pending at handoff"
)
DELIVERY_BOUNDARY = (
    "a local patch against the recorded baseline plus a PR draft in the reply; no permission to "
    "commit, push, open a PR, reset or clean existing files; preserve the initial untracked files; "
    "edit repository source and tests only as needed for the public issue, and ask before unrelated scope expansion"
)
PR_OVERLAY = {
    "id": "first_pr_evidence_based_handoff",
    "behaviour": "First-time contributor: ask one small concrete question at a time, request explanations of this patch, and verify the final draft against the diff and observed checks. State initial permissions clearly.",
    "forbidden": "Inventing failing examples, test commands, test results or existing user changes; authorizing commits, remote writes or workspace cleanup; waiving the original repair",
}


def pr_candidate():
    persona = load_persona(PERSONAS[1])
    template = next(t for t in persona["attributes"]["evolved_requirements"]
                    if t["id"] == "first_pr_reviewable_handoff")
    bindings = {"validation_catalog": VALIDATION_CATALOG, "delivery_boundary": DELIVERY_BOUNDARY}
    if set(template["bindings"]) != set(bindings):
        raise ValueError("Persona pool changed; revalidate the PR binder")
    candidate = Candidate(
        candidate_id="cand-pr-handoff", requirement_id="req.pr_handoff.01",
        persona_id=persona["id"], template_id=template["id"], template=template, bindings=bindings,
        checker={"id": "rubric_judge", "version": "1", "parameters": {
            "requirement": "req.pr_handoff.01@1", "samples": 3, "message_clauses": [],
            "subcheck": {"id": "deliverable_shape", "version": "1", "parameters": {}}}},
        schedule={"trigger_event": harness_trigger(template), "earliest_turn": 2,
                  "deadline_turn": 4, "priority": 1, "required": True},
        disclosure_markers=["reviewable PR draft"],
        contract_compat="Handoff evidence only; original issue and official acceptance tests stay frozen",
        evidence_refs=["public:original_instruction", "harness-observed/binding.json",
                       "harness-observed/patch.diff", "harness-observed/commands.json"],
    )
    return persona, candidate


def binding_record(manifest, initial, tracked_baseline):
    return {"baseline_commit": manifest.base_commit,
            "initial_tracked_patch": tracked_baseline,
            "initial_untracked_sha256": initial,
            "validation_catalog": VALIDATION_CATALOG, "delivery_boundary": DELIVERY_BOUNDARY}


def initial_notice(persona, binding):
    if persona != PERSONAS[1]:
        return ("You may edit repository-local source and tests as needed for this issue. "
                "Official acceptance runs separately after delivery and is unavailable during this interaction; "
                "report local checks separately from that pending result.\n\n")
    paths = binding["initial_tracked_patch"]["changed_paths"]
    tracked_notice = ("The initial image already has tracked changes in " + str(len(paths))
                      + " files; preserve these existing changes. " if paths else
                      "The initial tracked diff is empty. ")
    return ("This is my first PR. Please take one small step at a time and explain this specific fix. "
            "You may edit local repository source and tests as needed for this public issue. "
            "There is no permission to commit, push, open a PR, reset or clean existing files. "
            "Ask before unrelated scope expansion. " + tracked_notice
            + str(len(binding["initial_untracked_sha256"]))
            + " existing untracked files belong to the initial image and must be preserved. "
            "The official held-out suite is unavailable until after our interaction.\n\n")
