#!/usr/bin/env python3
"""Deterministic test of the machinery a lucky episode never exercises.

No LLM: it drives the controller and the ledger by hand over two real snapshots
(a hard-coded-reference-date entry point, then a fixed one) and asserts

  1. the event probe fires `reference_date_ignored` on the broken snapshot;
  2. the controller then schedules a REVISION (not a new requirement);
  3. `Ledger.revise` supersedes v1, marks its evidence stale, activates v2, and
     refuses a second revision of the same requirement;
  4. after the fix, the new snapshot re-verifies to `met` and the revision is no
     longer offered, because the event evidence is no longer current.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import dockerx
from checkers import CheckerRegistry
from controller import Controller
from dockerx import Container, Mount
from ledger import Ledger, LedgerError
from registry import build_candidates
from task_adapter import TaskManifest, build_environment_overlay
from validate_bindings import HARDCODED_ENTRYPOINT

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
BULK_VOLUME = "tb2pilot-bulk-v1"
CHECKS = []


def expect(cond: bool, label: str) -> None:
    CHECKS.append((label, bool(cond)))
    print(("  ok   " if cond else "  FAIL ") + label, flush=True)


def main() -> int:
    run_dir = HERE / "runs" / "revision-path-test"
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = TaskManifest.from_harbor_task()
    manifest.environment = build_environment_overlay(manifest, "/app/logs_bulk", "/harness")
    checkers = CheckerRegistry(FIXTURES, BULK_VOLUME, Path(manifest.hidden["tests_dir"]), run_dir)
    ledger = Ledger()
    candidates = build_candidates("laptop_data_scientist", max_evolved=2)
    controller = Controller(
        manifest=manifest, ledger=ledger, checkers=checkers, candidates=candidates,
        selected=candidates[:2], run_dir=run_dir, max_turns=8,
        snapshot_tag_prefix="tb2pilot-snap-revtest",
    )
    report = json.loads((HERE / "runs" / "validation" / "validation_report.json").read_text())
    controller.register_candidates(report)
    expect(all(ledger.entries[f"{c.requirement_id}@1"].activation_status == "validated_undisclosed"
               for c in candidates[:2]), "both selected candidates validated offline")

    container = Container(
        name="tb2pilot-revtest",
        image=manifest.docker_image,
        mounts=[Mount(str(FIXTURES), "/harness_full", read_only=True),
                Mount(BULK_VOLUME, "/app/logs_bulk", read_only=True)],
        network="none",
    )
    container.start()
    try:
        controller.snapshot(container, 0)
        # snapshot A: correct deliverable, entry point that ignores --reference-date
        container.sh("cat > /app/summarize.py <<'EOF'\n" + HARDCODED_ENTRYPOINT + "EOF\n"
                     "python3 /app/summarize.py --logs /app/logs --reference-date 2025-08-12 --out /app/summary.csv")
        controller.snapshot(container, 1)
        controller.add_event("approach_visible", 1, {"via": "artifact"})
        controller.add_event("first_patch", 1, {"via": "artifact"})
        controller.run_probes(1)
        expect("reference_date_ignored" in controller.fired, "probe fired reference_date_ignored")

        due2 = controller.due_item(2)
        expect(due2 is not None and due2["requirement_ref"] == "req.resource_budget.01@1" and due2["operation"] == "add",
               "turn 2 schedules the resource-budget disclosure first")
        ledger.disclose("req.resource_budget.01@1", turn=2, reason=due2["reason_code"])
        controller.disclosure_turns["req.resource_budget.01@1"] = 2
        controller.verify_active(2)

        due3 = controller.due_item(3)
        expect(due3 is not None and due3["requirement_ref"] == "req.data_semantics.01@1",
               "turn 3 schedules the data-semantics disclosure")
        ledger.disclose("req.data_semantics.01@1", turn=3, reason=due3["reason_code"])
        controller.disclosure_turns["req.data_semantics.01@1"] = 3
        controller.verify_active(3)
        expect(ledger.entries["req.data_semantics.01@1"].satisfaction_status == "not_met",
               "semantic checker reports not_met on the hard-coded snapshot")

        due4 = controller.due_item(4)
        expect(due4 is not None and due4["operation"] == "revise"
               and due4["requirement_ref"] == "req.data_semantics.01@2"
               and due4["replaces"] == ["req.data_semantics.01@1"],
               "turn 4 schedules a REVISION of the disclosed requirement")
        expect("--reference-date" in due4["must_include_verbatim"], "revision carries its bound markers")

        ledger.revise("req.data_semantics.01@1", new_statement=due4["statement"], turn=4,
                      reason=due4["reason_code"], evidence_refs=["chk-003"])
        old, new = ledger.entries["req.data_semantics.01@1"], ledger.entries["req.data_semantics.01@2"]
        expect(old.activation_status == "superseded" and old.satisfaction_status == "stale",
               "v1 is superseded and its evidence stale")
        expect(new.activation_status == "active" and new.supersedes == ["req.data_semantics.01@1"],
               "v2 is active and records what it replaces")
        try:
            ledger.revise("req.data_semantics.01@1", new_statement="again", turn=5, reason="x", evidence_refs=[])
            expect(False, "a second revision of the same requirement is refused")
        except LedgerError:
            expect(True, "a second revision of the same requirement is refused")
        expect(controller.due_item(5) is None, "no further disclosure is due after the revision")

        # snapshot B: the agent fixes the entry point
        container.sh("cp /harness_full/reference_impl.py /app/summarize.py && "
                     "python3 /app/summarize.py --logs /app/logs --reference-date 2025-08-12 --out /app/summary.csv")
        _img, digest, changed = controller.snapshot(container, 5)
        expect(changed, "the fix is seen as a new snapshot")
        controller.run_probes(5)
        expect(controller.latest_probe["reference_date_ignored"] is False, "live probe now says the date is honoured")
        controller.verify_active(5)
        expect(ledger.entries["req.data_semantics.01@2"].satisfaction_status == "met",
               "the revised requirement verifies as met on the fixed snapshot")
        expect(ledger.entries["req.resource_budget.01@1"].satisfaction_status == "met",
               "the resource requirement re-verifies as met after the change")
        expect(controller.due_item(6) is None, "a stale event no longer schedules anything")
    finally:
        container.remove()
        for turn in (0, 1, 5):
            dockerx.remove_image(f"tb2pilot-snap-revtest-t{turn}:latest")

    ledger.dump(run_dir / "ledger.json")
    failed = [label for label, ok in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} assertions passed")
    (run_dir / "result.json").write_text(json.dumps(
        {"assertions": [{"label": l, "ok": o} for l, o in CHECKS], "failed": failed}, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
