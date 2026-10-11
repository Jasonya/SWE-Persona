#!/usr/bin/env python3
"""Model-specific discrimination gate for the generic public-data rubrics."""

from __future__ import annotations

import argparse
import copy
import json
import re
import uuid
from pathlib import Path

import dockerx
from checkers import CheckerRegistry
from dockerx import Container, Mount
from llm import CallLog, Llm
from registry import build_candidates
from task_adapter import TaskManifest
from rubric_checker import GROUNDING_PROTOCOL, grading_identity

HERE = Path(__file__).resolve().parent


def unique_name(prefix):
    return prefix + '-' + uuid.uuid4().hex[:12]


def calibration_parameters(candidate, samples, base_commit):
    """Calibrate public reasoning, never expose official-test results to it."""
    params = copy.deepcopy(candidate.checker["parameters"])
    params["samples"] = samples
    if (params.get("subcheck") or {}).get("id") == "official_tests":
        params["subcheck"] = {
            "id": "task_patch_manifest", "version": "1",
            "parameters": {"base_commit": base_commit},
        }
    return params


def calibration_result_ok(result, expected):
    """A collection/judge malfunction is not a successful negative control."""
    return not result.evidence.get("error_category") and result.status in expected


def make_snapshot(manifest: TaskManifest, prepared: str, assets: Path, name: str, unrelated: bool = False):
    container = Container(
        name=unique_name(f"swepro-rub-{re.sub(r'[^a-z0-9]+', '-', name.lower())[:28]}"),
        image=prepared,
        mounts=[Mount(str(assets.resolve()), "/harness_full", read_only=True)],
    )
    container.start()
    try:
        setup = "git apply --binary /harness_full/gold.patch"
        if unrelated:
            setup += " && printf unrelated > /app/UNRELATED.txt"
        rc, out, err = container.sh(setup, timeout=600)
        if rc != 0:
            raise RuntimeError((err or out)[-3000:])
        rc, changed, err = container.sh("git diff --name-only HEAD; git ls-files --others --exclude-standard")
        if rc != 0:
            raise RuntimeError(err)
        digest = container.snapshot_digest(manifest.base_commit)
        tag = unique_name(f"swepro-rub-{manifest.row_index_zero + 1:03d}-{name}") + ':latest'
        container.commit(tag)
        return tag, digest, sorted(set(changed.splitlines()))
    finally:
        container.remove()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--judge-model", default="gpt-5.6-luna")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument(
        "--case",
        choices=("scope_good", "scope_omits_unrelated", "explanation_good", "explanation_generic"),
        help="rerun one gate case and merge it into the existing model report",
    )
    parser.add_argument("--dataset-index", type=int, default=44)
    parser.add_argument("--index-base", type=int, choices=[0, 1], default=1)
    parser.add_argument("--out", type=Path, default=HERE / "runs" / "validation")
    args = parser.parse_args()
    # Docker bind mounts require absolute host paths, including public contracts.
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    manifest = TaskManifest.from_dataset(dataset_index=args.dataset_index, index_base=args.index_base)
    run_dir = args.out / f"rubric-row-{manifest.row_index_zero + 1:03d}"
    run_dir.mkdir(parents=True, exist_ok=True)
    assets = manifest.materialize_checker_assets(run_dir / ".checker_assets", HERE / "fixtures")
    pulled = not dockerx.image_exists(manifest.docker_image)
    if pulled:
        dockerx.pull_image(manifest.docker_image)
    prepared = unique_name(f"swepro-rub-base-{manifest.row_index_zero + 1:03d}") + ':latest'
    prep = Container(name=unique_name(f"swepro-rub-prep-{manifest.row_index_zero + 1:03d}"), image=manifest.docker_image)
    images = []
    failures = []
    try:
        prep.start()
        guard = prep.prepare_clean_base(manifest.base_commit, manifest.source_commit)
        prep.commit(prepared)
        images.append(prepared)
        good_image, good_digest, changed = make_snapshot(manifest, prepared, assets, "good")
        bad_image, bad_digest, _bad_changed = make_snapshot(manifest, prepared, assets, "unrelated", unrelated=True)
        images.extend((good_image, bad_image))

        contract = manifest.instruction[:1800]
        changed_text = ", ".join(changed)
        good_scope = [
            {"role": "user", "message_id": "u-1", "text": "Keep this patch limited to the public task."},
            {"role": "agent", "message_id": "a-1", "status": "done", "text": (
                f"The clean-base net change is limited to {changed_text}. These files implement the behavior "
                "described by the public task; there are no unrelated, generated, configuration, or test edits."
            )},
        ]
        good_explanation = [
            {"role": "user", "message_id": "u-1", "text": "Explain the old behavior and why this diff fixes it."},
            {"role": "agent", "message_id": "a-1", "status": "done", "text": (
                "Before this change, consts/mime_types.go owned a hardcoded extension map and lossless-format "
                "list, and server/serve_index.go rendered the UI value from consts.LosslessFormats. That meant an "
                "operator could not add a format without a code release. The new resources/mime_types.yaml is the "
                "data source; conf/mime/mime_types.go loads its types and lossless entries during the conf.AddHook "
                "startup path, registers extension mappings (including the explicit .js and .css fallbacks), strips "
                "the leading dots into mime.LosslessFormats, and server/serve_index.go now renders that loaded list. "
                "So startup configuration now causally supplies both runtime MIME lookup and the uppercase, "
                "comma-separated UI setting. The boundary is exactly the configuration resource, its loader, the "
                f"removed hardcoded definitions, and the one server consumer ({changed_text}); it does not change "
                "unrelated media processing or introduce a new interface. Public contract: " + contract
            )},
        ]
        generic = [
            {"role": "user", "message_id": "u-1", "text": "Explain the task patch."},
            {"role": "agent", "message_id": "a-1", "status": "done", "text": "I changed the relevant code and tests pass."},
        ]
        hidden_unrelated = [
            {"role": "user", "message_id": "u-1", "text": "List every net change and justify its task scope."},
            {"role": "agent", "message_id": "a-1", "status": "done", "text": (
                f"The only net changes are {changed_text}; all are required by the public task."
            )},
        ]

        llm = Llm(args.judge_model, CallLog(run_dir / "rubric_gate_calls.jsonl"))
        checkers = CheckerRegistry(assets, run_dir, base_commit=manifest.base_commit, judge_llm=llm)
        candidates = {item.requirement_id: item for item in build_candidates(manifest)}
        scope = candidates["req.first_pr.scope.01"]
        explanation = candidates["req.first_pr.explanation.01"]
        cases = [
            ("scope_good", good_image, good_digest, scope, good_scope, {"pass"}),
            ("scope_omits_unrelated", bad_image, bad_digest, scope, hidden_unrelated, {"fail", "unknown"}),
            ("explanation_good", good_image, good_digest, explanation, good_explanation, {"pass"}),
            ("explanation_generic", good_image, good_digest, explanation, generic, {"fail", "unknown"}),
        ]
        output = args.out / f"rubric_report.{args.judge_model}.{GROUNDING_PROTOCOL}.json"
        report = {
            "grading_identity": grading_identity(),
            "calibration_protocol": "public-subchecks-v2",
            "task": manifest.selection_record(),
            "history_guard": guard,
            "judge_model": args.judge_model,
            "samples": args.samples,
            "arms": {},
        }
        if args.case and output.is_file():
            previous = json.loads(output.read_text(encoding="utf-8"))
            if (
                (previous.get("task") or {}).get("instance_id") == manifest.task_id
                and previous.get("judge_model") == args.judge_model
                and previous.get("samples") == args.samples
                and previous.get("grading_identity") == grading_identity()
                and previous.get("calibration_protocol") == "public-subchecks-v2"
            ):
                report["arms"] = dict(previous.get("arms") or {})
        for name, image, digest, candidate, transcript, expected in cases:
            if args.case and name != args.case:
                continue
            params = calibration_parameters(candidate, args.samples, manifest.base_commit)
            result = checkers.run(
                "rubric_judge", "1", params, image, f"{name}:{digest}",
                context={
                    "statement": candidate.statement(),
                    "satisfaction": candidate.template["satisfaction"],
                    "transcript": transcript,
                },
            )
            ok = calibration_result_ok(result, expected)
            report["arms"][name] = {
                "status": result.status,
                "reason_code": result.reason_code,
                "expected": sorted(expected),
                "ok": ok,
                "agreement": result.evidence.get("judge_agreement_mean"),
                "error_category": result.evidence.get("error_category"),
            }
            print(f"{name:25s} {result.status:8s} {'ok' if ok else 'FAIL'}", flush=True)
        expected_names = {name for name, *_rest in cases}
        failures = [
            f"{name}: missing or failed gate case"
            for name in sorted(expected_names)
            if not (report["arms"].get(name) or {}).get("ok")
        ]
        report["gate_passed"] = not failures
        report["failures"] = failures
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"gate_passed={not failures} -> {output}")
        return 1 if failures else 0
    finally:
        prep.remove()
        for image in images:
            dockerx.remove_image(image)
        # Registry images are shared Docker cache, not owned snapshots.
        # Retain them: another calibration may have checked this tag and be
        # starting its container concurrently.


if __name__ == "__main__":
    raise SystemExit(main())
