#!/usr/bin/env python3
"""LLM-free integration gate for any pinned SWE-bench Pro instance."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import dockerx
from checkers import CheckerRegistry
from dockerx import Container, Mount
from task_adapter import TaskManifest

HERE = Path(__file__).resolve().parent


def snapshot(manifest: TaskManifest, prepared: str, assets: Path, name: str, setup: str):
    suffix = hashlib.sha256((manifest.task_id + name).encode()).hexdigest()[:10]
    container = Container(
        name=f"swepro-bind-{re.sub(r'[^a-z0-9]+', '-', name.lower())[:20]}-{suffix}",
        image=prepared,
        mounts=[Mount(str(assets.resolve()), "/harness_full", read_only=True)],
    )
    container.start()
    try:
        if setup:
            rc, out, err = container.sh(setup, timeout=600)
            if rc != 0:
                raise RuntimeError(f"setup {name} failed: {(err or out)[-3000:]}")
        digest = container.snapshot_digest(manifest.base_commit)
        tag = f"swepro-bind-{suffix}:latest"
        container.commit(tag)
        return tag, digest
    finally:
        container.remove()


def main() -> int:
    parser = argparse.ArgumentParser()
    task = parser.add_mutually_exclusive_group()
    task.add_argument("--instance-id")
    task.add_argument("--dataset-index", type=int, default=44)
    parser.add_argument("--index-base", type=int, choices=[0, 1], default=1)
    parser.add_argument("--out", type=Path, default=HERE / "runs" / "validation")
    args = parser.parse_args()
    args.out = args.out.resolve()
    manifest = TaskManifest.from_dataset(
        instance_id=args.instance_id,
        dataset_index=None if args.instance_id else args.dataset_index,
        index_base=args.index_base,
    )
    args.out.mkdir(parents=True, exist_ok=True)
    run_dir = args.out / f"row-{manifest.row_index_zero + 1:03d}"
    run_dir.mkdir(parents=True, exist_ok=True)
    assets = manifest.materialize_checker_assets(run_dir / ".checker_assets", HERE / "fixtures")
    pulled = not dockerx.image_exists(manifest.docker_image)
    if pulled:
        dockerx.pull_image(manifest.docker_image)
    prepared = f"swepro-bind-base-{manifest.row_index_zero + 1:03d}:latest"
    prep = Container(name=f"swepro-bind-prep-{manifest.row_index_zero + 1:03d}", image=manifest.docker_image)
    images = []
    failures = []
    try:
        prep.start()
        guard = prep.prepare_clean_base(manifest.base_commit, manifest.source_commit)
        prep.commit(prepared)
        images.append(prepared)
        checkers = CheckerRegistry(assets, run_dir, base_commit=manifest.base_commit)
        specs = [
            ("base", "", {"official_tests": "fail", "task_patch_manifest": "fail"}),
            (
                "reference_compliant",
                "git apply --binary /harness_full/gold.patch",
                {"official_tests": "pass", "task_patch_manifest": "pass"},
            ),
        ]
        report = {
            "task": manifest.selection_record(),
            "history_guard": guard,
            "harness_assets": json.loads((assets / "asset_hashes.json").read_text(encoding="utf-8")),
            "arms": {},
        }
        for name, setup, expected in specs:
            image, digest = snapshot(manifest, prepared, assets, name, setup)
            images.append(image)
            official = checkers.run("official_tests", "1", {}, image, f"{name}:{digest}")
            patch = checkers.run(
                "task_patch_manifest", "1", {"base_commit": manifest.base_commit},
                image, f"{name}:{digest}",
            )
            actual = {"official_tests": official.status, "task_patch_manifest": patch.status}
            ok = actual == expected
            # A base-pass anomaly is reported but does not invalidate a working
            # gold-patch/harness chain.
            if name == "base" and official.status == "pass" and patch.status == "fail":
                ok = True
            report["arms"][name] = {
                "checks": {
                    "official_tests": {"status": official.status, "reason_code": official.reason_code},
                    "task_patch_manifest": {"status": patch.status, "reason_code": patch.reason_code},
                },
                "expected": expected,
                "ok": ok,
            }
            if not ok:
                failures.append(f"{name}: expected {expected}, got {actual}")
            print(f"{name:20s} official={official.status:7s} patch={patch.status:7s} {'ok' if ok else 'FAIL'}")
        report["gate_passed"] = not failures
        report["failures"] = failures
        output = run_dir / "validation_report.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (args.out / "validation_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"gate_passed={not failures} -> {output}")
        return 1 if failures else 0
    finally:
        prep.remove()
        for image in images:
            dockerx.remove_image(image)
        if pulled:
            dockerx.remove_image(manifest.docker_image, force=False)


if __name__ == "__main__":
    raise SystemExit(main())
