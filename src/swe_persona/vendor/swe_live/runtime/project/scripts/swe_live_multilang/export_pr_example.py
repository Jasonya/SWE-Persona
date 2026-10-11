"""Package a finished study for review without running models or changing scores."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[2]


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def within(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Artifact path escapes its source directory")
    return path


def export(study, output):
    study, output = study.resolve(), output.resolve()
    if (study / "study.lock").exists():
        raise ValueError("Study is still running")
    if output.exists() or output == study or study.is_relative_to(output):
        raise ValueError("Choose a new output directory outside the source ancestors")
    source = Path(read(study / "latest-review-bundle.json")["path"]).resolve()
    if output.is_relative_to(source):
        raise ValueError("Do not modify an existing immutable bundle")
    if not read(source / "export_check.json")["passed"]:
        raise ValueError("Source export check failed")
    source_hashes = read(source / "files.sha256.json")
    for name, expected in source_hashes.items():
        path = within(source, name)
        if not path.is_file() or sha(path) != expected:
            raise ValueError("Source bundle hash mismatch: " + name)
    rows, manifest = read(source / "results.json"), read(source / "manifest.json")
    cells = [c for row in rows for c in row["cells"]]
    if any(c["status"] not in ("valid", "invalid", "gold_preflight_failed") for c in cells):
        raise ValueError("Study still contains pending conditions")
    for name, expected in manifest["sources"].items():
        if sha(within(ROOT, name)) != expected:
            raise ValueError("Frozen source changed before packaging: " + name)
    shutil.copytree(source, output)
    save(output / "provenance/source-bundle-files.sha256.json", source_hashes)
    public_tasks, forbidden = [], []
    for task in manifest["tasks"]:
        private = json.loads(within(study, task["path"]).read_text(encoding="utf-8").splitlines()[0])
        public_tasks.append({"key": task["key"], **{k: private[k] for k in
            ("instance_id", "repo", "base_commit", "problem_statement", "docker_image") if k in private}})
        for key in ("patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS"):
            value = private.get(key)
            if value and len(str(value)) > 80:
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                forbidden.extend((text.encode(), json.dumps(text, ensure_ascii=False)[1:-1].encode()))
    (output / "tasks.public.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False) + "\n"
                                                    for x in public_tasks), encoding="utf-8")
    pool = read(ROOT / "human-interactive-coding-personas.v3.json")
    save(output / "personas.json", {"active_templates": ["manager_conclusion_only_delivery",
        "first_pr_reviewable_handoff"], "personas": [p for p in pool["personas"]
        if p["id"] in ("manager_no_code", "first_time_pr_contributor")]})
    review = []
    for row in rows:
        for cell in row["cells"]:
            run = within(study, cell["run"]) if cell["run"] else None
            episode = read(run / "episode.json") if run and (run / "episode.json").is_file() else {}
            for relative in cell["attempts"]:
                for name in ("simulator_prompt.txt", "first_message.json", "task.public.json"):
                    src, dst = within(study, relative) / name, within(output, relative) / name
                    if src.is_file() and not dst.exists():
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(src, dst)
            review.append({"task_key": row["key"], "task": row["task"], "arm": cell["arm"],
                "persona": cell["persona"], "status": cell["status"], "automatic_valid": cell["valid"],
                "official_success": cell["base_success"], "persona_status": cell["persona_status"],
                "joint_success_recorded": cell["joint_success"],
                "joint_success_for_valid_analysis": cell["joint_success"] if cell["valid"] else None,
                "interaction_seconds": cell["timing"].get("interaction_seconds"),
                "final_review_seconds": cell["timing"].get("final_review_seconds"),
                "total_episode_seconds": cell["seconds"], "gold_seconds": row.get("gold_seconds"),
                "termination": episode.get("metrics", {}).get("interaction_delivery", {}).get("end_state"),
                "error": episode.get("error"), "audit_problems": "; ".join(cell["problems"]),
                "attempts": "; ".join(cell["attempts"]),
                "transcript": f"reports/trajectories/{row['key']}.html",
                "trajectory": f"reports/trajectories/{row['key']}.png"})
    with (output / "review.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(review[0]))
        writer.writeheader()
        writer.writerows(review)
    summary = ["# SWE-bench-Live MultiLang study results", "",
        "Queue completed; semantic transcript review is not complete. Scores below are raw counts, not paper-finalized rates.", "",
        "| Arm | Planned | Attempted | Automatically valid | Invalid/incomplete | Gold blocked | Official pass / known | Joint pass / valid |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for arm in ("original", "persona0", "persona1"):
        group = [c for c in cells if c["arm"] == arm]
        valid = sum(c["valid"] for c in group)
        summary.append(f"| {arm} | {len(group)} | {sum(bool(c['attempts']) for c in group)} | {valid} | "
            f"{sum(c['status']=='invalid' for c in group)} | {sum(c['status']=='gold_preflight_failed' for c in group)} | "
            f"{sum(c['base_success']==1 for c in group)} / {sum(c['base_success'] is not None for c in group)} | "
            f"{sum(c['valid'] and c['joint_success']==1 for c in group)} / {valid} |")
    valid_cells = [c for c in cells if c["valid"]]
    attempted = sum(bool(c["attempts"]) for c in cells)
    blocked = sum(c["status"] == "gold_preflight_failed" for c in cells)
    invalid = sum(c["status"] == "invalid" for c in cells)
    attempts = sum(len(c["attempts"]) for c in cells)
    known = sum(c["base_success"] is not None for c in cells)
    passed = sum(c["base_success"] == 1 for c in cells)
    valid_passed = sum(c["base_success"] == 1 for c in valid_cells)
    summary += ["", "Official pass / known includes known functional outcomes from otherwise invalid episodes; "
        "it is not the all-valid cohort. Unknown is never counted as zero. Joint comparisons use only "
        "automatically valid episodes and are not paired comparisons across the same task set.", "",
        f"The {len(cells)} planned conditions produced {attempted} attempted conditions and {blocked} blocked conditions. "
        f"There are {len(valid_cells)} automatically valid results ({valid_passed} functional successes and "
        f"{len(valid_cells)-valid_passed} functional failures), {invalid} invalid/incomplete results, and {attempts} "
        f"preserved attempt records. The known official outcomes include {passed} passes and {known-passed} failures; "
        f"{attempted-known} attempted conditions have unknown functional outcomes.", "",
        "Some raw records may contain a joint score despite failing the final audit. Raw values are retained "
        "but excluded from valid joint analysis; see both CSV columns.", "",
        "Original uses the full issue; persona arms use adapted openings and evolving disclosure. "
        "This study does not isolate the effect of persona alone. Review the frozen protocol, "
        "source amendments, timeout policy, message-screen receipts (where enabled), and full transcripts. "
        "An automated semantic screen does not replace human review.", "",
        "[Full result table](reports/index.html) · [Review CSV](review.csv) · [Summary graphic](reports/summary.png)"]
    (output / "SUMMARY.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    (output / "README.md").write_text(
        f"# SWE-bench-Live MultiLang: {len(rows)}-task study\n\n"
        f"This is a completed queue and review example, not {len(cells)} valid finalized samples. "
        "Start with [SUMMARY.md](SUMMARY.md), [review.csv](review.csv), or the "
        "[interactive result table](reports/index.html). Task links open full conversations and figures.\n\n"
        "Public inputs are in `tasks.public.jsonl`; persona definitions are in `personas.json`. "
        "Effective openings and simulator prompts accompany preserved attempts in `episodes/`. "
        "Private evaluator rows, oracle patches, credentials, provider logs, and agent thought history are excluded. "
        "Public inputs alone cannot run official grading: obtain the pinned dataset and images using "
        "[the study instructions](../../../docs/swe-live-multilang-study-20.md). "
        "Original execution records may contain local paths; use documented commands for reproduction.\n\n"
        "All available attempts are retained, including invalid and interrupted attempts. "
        "Raw scores and transcripts are unchanged. Automatic validity still requires semantic review. "
        "The frozen manifest records the protocol, source hashes, selection and budgets.\n\n"
        "`provenance/source-bundle-files.sha256.json` records the original checked export. "
        "The root `files.sha256.json` covers this augmented example. See `pr_export_check.json` "
        "for the final literal secret/private-payload scan and preservation checks. "
        "This scan does not substitute for semantic review.\n", encoding="utf-8")
    env = ROOT / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            value = value.strip().strip("\"'")
            if sep and len(value) >= 8 and any(x in key.upper() for x in ("KEY", "SECRET", "TOKEN", "PASSWORD")):
                forbidden.append(value.encode())
    preserved = [name for name, expected in source_hashes.items() if name != "README.md"
                 and sha(within(output, name)) != expected]
    flagged = []
    for path in output.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            if any(marker in data for marker in forbidden):
                flagged.append(path.relative_to(output).as_posix())
    missing_links = [r[field] for r in review for field in ("transcript", "trajectory")
                     if not within(output, r[field]).is_file()]
    check = {"passed": not (flagged or preserved or missing_links), "flagged_files": flagged,
        "changed_original_artifacts": preserved, "missing_links": missing_links,
        "planned_rows": len(review), "source_files_verified": len(source_hashes),
        "scope": "Known literal secrets/private payloads, original artifact preservation and report links; semantic review pending"}
    save(output / "pr_export_check.json", check)
    save(output / "provenance/packaging.json", {"created_utc": datetime.now(timezone.utc).isoformat(),
        "source_bundle": source.name, "packager": "scripts/swe_live_multilang/export_pr_example.py",
        "packager_sha256": sha(Path(__file__))})
    save(output / "files.sha256.json", {p.relative_to(output).as_posix(): sha(p)
        for p in output.rglob("*") if p.is_file() and p != output / "files.sha256.json"})
    if not check["passed"]:
        raise ValueError("PR export check failed; inspect pr_export_check.json")
    return check


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(export(args.study, args.output), indent=2))
