"""Formal reporting eligibility, independent of agent outcomes and runtime gates.

This is an analysis policy, NOT a corrected or recalibrated task evaluator.
Changing eligibility requires a new policy version and documented evidence.
"""

VERSION = 'original-task-report-policy-v1'
EXCLUDED_TASKS = {
    'obsidian-linter-auto-table-of-contents': (
        'Pinned Jest full-title matching can mark executed F2P cases missing. '
        'Exclude original-task strict, F2P/P2P and joint metrics from formal '
        'analysis pending a versioned corrected evaluator and fresh calibration. '
        'Raw scores are audit-only; independent persona evaluation is retained.'
    ),
}


def exclusion_reason(task_id):
    return EXCLUDED_TASKS.get(task_id)
