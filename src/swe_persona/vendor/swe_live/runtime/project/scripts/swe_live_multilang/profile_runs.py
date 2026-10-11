"""Summarize existing run timing without Docker, API calls, or result mutation."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re


def summarize(batch):
    batch = Path(batch)
    rows = []
    for path in sorted((batch / "episodes").glob("*/*/attempt-*/summary.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        api, counts = Counter(), Counter()
        errors = 0
        calls = path.parent / "llm_calls.jsonl"
        if calls.is_file():
            for line in calls.open(encoding="utf-8"):
                call = json.loads(line)
                if "schema_repair_round" in call:
                    continue
                role = call.get("role", "unknown").split(".")[0]
                api[role] += call.get("latency_sec", 0)
                counts[role] += 1
                errors += "error" in call
        info = path.stat()
        birth = getattr(info, "st_birthtime", None)
        evaluation = path.parent / "evaluation.log"
        eval_birth = getattr(evaluation.stat(), "st_birthtime", None) if evaluation.is_file() else None
        rows.append({"language": path.parts[-4], "arm": path.parts[-3], "attempt": path.parent.name,
                     "status": data.get("status"), "evaluation_valid": data.get("evaluation_valid"),
                     "wall_seconds_estimate": round(info.st_mtime - birth, 2) if birth else None,
                     "official_seconds_estimate": round(info.st_mtime - eval_birth, 2) if eval_birth else None,
                     "api_seconds_by_role": {k: round(v, 2) for k, v in api.items()},
                     "api_calls_by_role": dict(counts), "api_errors": errors})
    gold = []
    for path in sorted((batch / "gold").glob("*/attempt-*/evaluation.log")):
        info = path.stat()
        birth = getattr(info, "st_birthtime", None)
        logs = list(path.parent.glob("evaluation/*/post_patch_log.txt"))
        durations = []
        for log in logs:
            # Fixed-prefix bounded numeric matches: no unanchored .* scan over
            # megabyte XML lines, which would itself introduce quadratic work.
            text = log.read_text(encoding="utf-8", errors="replace")
            durations.extend(float(v) for v in re.findall(r'durationInSeconds="([0-9.]+)"', text))
        gold.append({"language": path.parts[-3], "attempt": path.parent.name,
                     "wall_seconds_estimate": round(info.st_mtime - birth, 2) if birth else None,
                     "test_output_bytes": sum(p.stat().st_size for p in logs),
                     "catch_duration_entries": len(durations),
                     "catch_duration_sum_seconds": round(sum(durations), 2) if durations else None})
    return {"timing_source": "Pilot wall estimates use local file creation/modification times; copying files invalidates these estimates. API latency is recorded directly. Catch durations can include nested sections and are not wall time.",
            "episodes": rows, "gold": gold,
            "note": "Gold runs, image pulls and episodes are separate work. No scores or attempts were rewritten."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, default=Path(".local/multilang-batch/pilot-v1"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = summarize(args.batch)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for row in report["episodes"]:
        total, official = row["wall_seconds_estimate"], row["official_seconds_estimate"]
        print(row["language"], row["arm"], "wall_min=", round(total / 60, 1) if total else None,
              "official_min=", round(official / 60, 1) if official else None)
    print("Report:", args.output)


if __name__ == "__main__":
    main()
