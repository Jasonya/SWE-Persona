"""Pure selection/accounting for the full ten-persona inventory; no model calls."""
import math

from support import ROOT, digest, read
from personas import PERSONAS

POOL = ROOT / "human-interactive-coding-personas.v3.json"
# These are missing registered *task fixtures*, not claims about task semantics.
MISSING_FIXTURES = {
    "terse_lead_with_flaky_test": "controlled intermittent failure, repetition protocol and stability oracle",
    "laptop_data_scientist": "representative public workload, process-tree measurement and output-semantics oracle",
    "student_needing_explanation": "bound public counterexample, baseline execution-path anchors and checked boundary cases",
    "analyst_without_index_permission": "database dialect/schema, restricted role and isolated query-semantics cases",
    "designer_after_hard_reset": "authorized controlled post-reset state, recovery evidence and content ownership",
    "founder_leaked_production_key": "isolated credential service, fictitious rotation event and revocation verifier",
    "shop_owner_cannot_install": "verified user-operable delivery entry point in the registered no-install tool environment",
    "senior_race_review_no_tutorial": "bound concurrent entry points, read-only write audit and explicit implementation authorization branch",
}


def persona_pool():
    personas = read(POOL)["personas"]
    ids = [p["id"] for p in personas]
    if len(ids) != 10 or len(set(ids)) != 10 or set(ids) != set(PERSONAS) | set(MISSING_FIXTURES):
        raise ValueError("Persona pool changed; revalidate the full-set inventory")
    return personas


def preliminary_inventory(task):
    """Never conflate an unimplemented fixture with intrinsic incompatibility."""
    from task_fixtures import SPECS
    result = [{"task_key": task["key"], "instance_id": task["instance_id"], "persona": p["id"],
             "status": "pending_public_fit_check" if p["id"] in PERSONAS else "binding_required",
             "reason": "Public task fit must be checked before observing agent outcomes" if p["id"] in PERSONAS else
                       "No registered per-task fixture for: " + MISSING_FIXTURES[p["id"]],
             "pool_required_fixtures": p["attributes"]["episode_binding"]["required_fixtures"],
             "pool_missing_inputs": p["attributes"]["episode_binding"]["missing_inputs"]}
            for p in persona_pool()]
    for pair in result:
        fixtures = [kind for kind, spec in SPECS.items() if spec["persona"] == pair["persona"]
                    and task["instance_id"] in spec["instance_ids"]]
        if fixtures:
            pair.update(status="pending_fixture_validation", fixture_candidates=fixtures,
                        reason="Registered task adapter exists; executed controls and a valid agent episode are still required")
    return result


def p20(records):
    """Nearest-rank P20; completed failures count, censored samples stay in N.

    Infrastructure-invalid records have no known completion time. Exclude them
    explicitly, retain their counts and require reported calibration coverage.
    Never impute their error exit time as a completion, or use gold duration.
    A censoring bound below the candidate quantile makes it unidentified.
    """
    completed, censored, excluded = [], [], []
    for record in records:
        status = record["status"]
        if status == "completed":
            value = record["seconds"]
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("Invalid reference duration")
            completed.append(value)
        elif status == "timeout":
            value = record["cap_seconds"]
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("Invalid censoring bound")
            censored.append(value)
        else:
            excluded.append(status)
    population = len(completed) + len(censored)
    if not population:
        raise ValueError("No evaluable timing references; a threshold cannot be calculated")
    rank = math.ceil(population * .2)
    completed.sort()
    if len(completed) < rank:
        raise ValueError("P20 is right-censored; extend the reference ceiling in a new frozen study")
    threshold = completed[rank - 1]
    if any(bound < threshold for bound in censored):
        raise ValueError("P20 is not identified by the available censored references")
    return {"seconds": threshold, "quantile": .2, "method": "nearest_rank_ceil_p_times_n",
            "rank": rank, "population": population, "completed": len(completed),
            "right_censored": len(censored), "excluded_invalid": len(excluded),
            "total_references": len(records), "coverage": population / len(records),
            "reference": "Original full-issue single-turn; whole supervised process through final evaluation",
            "records_digest": digest(records)}


def task_time_status(reference, threshold, prefilter=True):
    if reference["status"] not in ("completed", "timeout"):
        return "reference_invalid"
    if prefilter and (reference["status"] == "timeout" or reference["seconds"] > threshold):
        return "excluded_time_screen"
    return "selected"


def fastest_selection(records, threshold, prefilter=True):
    """Keep exactly ceil(.2*N) evaluable references, with public-ID tie breaking.

    Infrastructure-invalid observations cannot establish a completion rank.
    The quantile includes right-censored records; they cannot be selected when
    the quantile is identified. Success/failure is deliberately not consulted.
    """
    keys = [r['task_key'] for r in records]
    if len(keys) != len(set(keys)):
        raise ValueError('Duplicate timing reference task keys')
    eligible = sorted((r for r in records if r['status'] == 'completed'),
                      key=lambda r: (r['seconds'], r['task_key']))
    selected = {r['task_key'] for r in eligible[:threshold['rank']]}
    rows = []
    for reference in records:
        status = task_time_status(reference, threshold['seconds'], prefilter)
        if prefilter and status == 'selected' and reference['task_key'] not in selected:
            status = 'excluded_time_screen'
        rows.append({'task_key': reference['task_key'], 'status': status,
                     'reference_status': reference['status'], 'reference_seconds': reference.get('seconds'),
                     'tie_break': 'task_key_ascending'})
    return rows


def metric_summary(results):
    from collections import Counter
    counts = Counter(r["status"] for r in results)
    executed = [r for r in results if r["status"] in ("completed", "timeout")]
    screened = [r for r in results if r["status"] == "excluded_time_screen"]
    # User's preregistered policy assigns zero to the slower reference cohort.
    # Keep these unexecuted cells distinct from measured persona timeouts.
    scored = executed + screened
    passes = sum(r.get("joint_success") == 1 for r in executed)
    return {"counts": dict(counts), "scored_including_timeouts": len(scored), "joint_passes": passes,
            "joint_rate_including_timeouts": passes / len(scored) if scored else None,
            "executed_scored": len(executed), "time_screen_policy_failures": len(screened),
            "joint_rate_among_executed": passes / len(executed) if executed else None,
            "candidate_count": len(results),
            "note": "Reference-time-screen exclusions count as policy failures (zero), not measured persona timeouts. "
                    "N/A, unbound and invalid are reported separately and excluded from scored denominators."}
