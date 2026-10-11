#!/usr/bin/env python3
"""Container-side checker driver (mounted at /harness_full, checkers only).

One place where every registered checker's verdict is computed, so the verdict
logic is auditable and identical whether it runs mid-episode as a probe or in
the final outcome verifier.  Prints exactly one line:

    CHECK {"status": "pass|fail|unknown", "reason_code": ..., "evidence": {...}}

The agent's container never mounts this file (nor reference_impl.py), so the
oracle is not readable from inside the episode.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reference_impl as ref  # noqa: E402

HARNESS = Path(__file__).resolve().parent


def emit(status: str, reason_code: str, **evidence) -> int:
    print("CHECK " + json.dumps({"status": status, "reason_code": reason_code, "evidence": evidence}, ensure_ascii=False))
    return 0


def oracle_csv(log_dir: str, reference_date: str) -> str:
    return ref.rows_to_csv(ref.compute_rows(Path(log_dir), ref.parse_date(reference_date)))


def diff_rows(expected_csv: str, actual_text: str):
    exp = [line.split(",") for line in expected_csv.strip().splitlines()]
    act = [line.split(",") for line in actual_text.strip().splitlines()]
    diffs = []
    if not act:
        return [{"row": 0, "expected": exp[0], "actual": None, "note": "empty output"}]
    if act[0] != exp[0]:
        diffs.append({"row": 0, "expected": exp[0], "actual": act[0], "note": "header"})
    for i in range(1, max(len(exp), len(act))):
        e = exp[i] if i < len(exp) else None
        a = act[i] if i < len(act) else None
        if e != a:
            diffs.append({"row": i, "expected": e, "actual": a})
    return diffs


def measured_run(cmd, timeout, json_path="/tmp/measure.json"):
    full = [sys.executable, str(HARNESS / "measure.py"), "--json", json_path, "--timeout", str(timeout), "--"] + cmd
    proc = subprocess.run(full, capture_output=True, text=True, timeout=timeout + 120)
    report = {}
    try:
        report = json.loads(Path(json_path).read_text())
    except (OSError, json.JSONDecodeError):
        for line in (proc.stdout or "").splitlines():
            if line.startswith("MEASUREMENT "):
                report = json.loads(line[len("MEASUREMENT ") :])
    return proc, report


def entrypoint_argv(entrypoint_path: str, logs: str, reference_date: str, out: str):
    return [sys.executable, entrypoint_path, "--logs", logs, "--reference-date", reference_date, "--out", out]


def check_entrypoint_present(args) -> int:
    path = Path(args.entrypoint_path)
    if not path.is_file():
        return emit("fail", "no_runnable_entrypoint", entrypoint=str(path), exists=False)
    probe_dir = "/tmp/ep_probe_logs"
    Path(probe_dir).mkdir(parents=True, exist_ok=True)
    Path(probe_dir, "2025-08-12_db.log").write_text(
        "2025-08-12 00:00:00 [ERROR] x\n2025-08-12 00:00:01 [INFO] y\n", encoding="utf-8"
    )
    proc = subprocess.run(
        entrypoint_argv(str(path), probe_dir, "2025-08-12", "/tmp/ep_probe.csv"),
        capture_output=True, text=True, timeout=300,
    )
    text = Path("/tmp/ep_probe.csv").read_text(encoding="utf-8") if Path("/tmp/ep_probe.csv").is_file() else ""
    ok = proc.returncode == 0 and text.strip().splitlines()[:1] == ["period,severity,count"]
    return emit(
        "pass" if ok else "fail",
        "entrypoint_ok" if ok else "entrypoint_smoke_failed",
        entrypoint=str(path),
        exit_code=proc.returncode,
        stdout=(proc.stdout or "")[-800:],
        stderr=(proc.stderr or "")[-800:],
        first_line=text.strip().splitlines()[:1],
    )


def check_resource_budget(args) -> int:
    path = Path(args.entrypoint_path)
    if not path.is_file():
        return emit("fail", "no_runnable_entrypoint", entrypoint=str(path))
    out_csv = "/tmp/agent_bulk.csv"
    proc, report = measured_run(
        entrypoint_argv(str(path), args.workload, args.reference_date, out_csv), timeout=args.run_timeout
    )
    if not report:
        return emit("unknown", "measurement_unavailable", stdout=(proc.stdout or "")[-800:], stderr=(proc.stderr or "")[-800:])
    if report.get("timed_out") or report.get("exit_code") != 0:
        return emit("fail", "entrypoint_failed", measurement=report, stderr=(proc.stderr or "")[-1500:])
    if not Path(out_csv).is_file():
        return emit("fail", "out_path_not_written", measurement=report, expected_out=out_csv,
                    stdout=(proc.stdout or "")[-800:])
    actual = Path(out_csv).read_text(encoding="utf-8")
    diffs = diff_rows(oracle_csv(args.workload, args.reference_date), actual)
    over_rss = report["peak_rss_kib"] > args.cap_peak_rss_kib
    over_wall = report["wall_sec"] > args.cap_wall_sec
    if diffs:
        return emit("fail", "workload_output_mismatch", measurement=report, diffs=diffs[:6], diff_count=len(diffs))
    if over_rss or over_wall:
        return emit(
            "fail",
            "over_budget",
            measurement=report,
            cap_peak_rss_kib=args.cap_peak_rss_kib,
            cap_wall_sec=args.cap_wall_sec,
            over_rss=over_rss,
            over_wall=over_wall,
        )
    return emit(
        "pass",
        "within_budget_and_exact",
        measurement=report,
        cap_peak_rss_kib=args.cap_peak_rss_kib,
        cap_wall_sec=args.cap_wall_sec,
    )


def _build_semantic(dir_path: str) -> None:
    subprocess.run([sys.executable, str(HARNESS / "gen_semantic_logs.py"), "--out", dir_path], check=True,
                   capture_output=True, text=True, timeout=300)


def check_semantic_matrix(args) -> int:
    path = Path(args.entrypoint_path)
    if not path.is_file():
        return emit("fail", "no_runnable_entrypoint", entrypoint=str(path))
    sem_dir = "/tmp/logs_semantic"
    _build_semantic(sem_dir)
    out_csv = "/tmp/agent_semantic.csv"
    proc = subprocess.run(
        entrypoint_argv(str(path), sem_dir, args.reference_date, out_csv), capture_output=True, text=True, timeout=600
    )
    if proc.returncode != 0:
        return emit("fail", "entrypoint_failed", exit_code=proc.returncode, stderr=(proc.stderr or "")[-1500:])
    if not Path(out_csv).is_file():
        return emit("fail", "out_path_not_written", expected_out=out_csv, stdout=(proc.stdout or "")[-800:])
    actual = Path(out_csv).read_text(encoding="utf-8")
    expected = oracle_csv(sem_dir, args.reference_date)
    diffs = diff_rows(expected, actual)
    if diffs:
        return emit("fail", "semantic_mismatch", diffs=diffs[:8], diff_count=len(diffs),
                    reference_date=args.reference_date, fixture=sem_dir)
    return emit("pass", "semantic_exact", reference_date=args.reference_date, fixture=sem_dir, rows_compared=15)


def check_probe_reference_date(args) -> int:
    """Typed-event probe: does --reference-date actually drive the windows?"""
    path = Path(args.entrypoint_path)
    if not path.is_file():
        return emit("unknown", "no_runnable_entrypoint", entrypoint=str(path))
    sem_dir = "/tmp/logs_semantic_probe"
    _build_semantic(sem_dir)
    outs = {}
    for ref_date in (args.reference_date, args.alt_reference_date):
        out = f"/tmp/probe_{ref_date}.csv"
        proc = subprocess.run(
            entrypoint_argv(str(path), sem_dir, ref_date, out), capture_output=True, text=True, timeout=600
        )
        if proc.returncode != 0:
            return emit("unknown", "entrypoint_failed", exit_code=proc.returncode, stderr=(proc.stderr or "")[-800:])
        outs[ref_date] = Path(out).read_text(encoding="utf-8") if Path(out).is_file() else ""
    a, b = outs[args.reference_date], outs[args.alt_reference_date]
    oracle_a = oracle_csv(sem_dir, args.reference_date)
    oracle_b = oracle_csv(sem_dir, args.alt_reference_date)
    ignored = (a == b) and (oracle_a != oracle_b)
    return emit(
        "pass" if not ignored else "fail",
        "reference_date_ignored" if ignored else "reference_date_honoured",
        reference_date_ignored=ignored,
        outputs={args.reference_date: a.strip().splitlines()[1:4], args.alt_reference_date: b.strip().splitlines()[1:4]},
    )


def check_clean_room_repro(args) -> int:
    path = Path(args.entrypoint_path)
    if not path.is_file():
        return emit("fail", "no_runnable_entrypoint", entrypoint=str(path))
    out_csv = "/tmp/repro.csv"
    proc = subprocess.run(
        entrypoint_argv(str(path), args.workload, args.reference_date, out_csv), capture_output=True, text=True, timeout=600
    )
    if proc.returncode != 0:
        return emit("fail", "entrypoint_failed", exit_code=proc.returncode, stderr=(proc.stderr or "")[-1500:])
    actual = Path(out_csv).read_text(encoding="utf-8") if Path(out_csv).is_file() else ""
    diffs = diff_rows(oracle_csv(args.workload, args.reference_date), actual)
    delivered = Path("/app/summary.csv").read_text(encoding="utf-8") if Path("/app/summary.csv").is_file() else ""
    same_as_delivered = delivered.strip() == actual.strip()
    if diffs:
        return emit("fail", "repro_mismatch", diffs=diffs[:6], same_as_delivered=same_as_delivered)
    return emit("pass", "repro_exact", same_as_delivered=same_as_delivered)


TEXT_SUFFIXES = (".py", ".sh", ".md", ".txt", ".csv", ".json", ".cfg", ".ini", ".toml", ".yaml", ".yml", "")
# The authoritative stdlib set for the interpreter that actually runs in the image.
STDLIB_MODULES = set(getattr(sys, "stdlib_module_names", ()))
NETWORK_IMPORT_RE = re.compile(
    r"^\s*(?:import|from)\s+(urllib|requests|socket|httpx|aiohttp|ftplib|telnetlib|smtplib)\b", re.MULTILINE
)


def _iter_workspace(root: Path, exclude_names=("logs", "logs_bulk")):
    for entry in sorted(root.rglob("*")):
        if any(part in exclude_names for part in entry.relative_to(root).parts[:-1] + (entry.name,)):
            continue
        if entry.is_file():
            yield entry


def check_evidence_bundle(args) -> int:
    """Deterministic grounding for the rubric judge: what is actually in the snapshot.

    No verdict of its own -- it only reports the workspace listing and the text of
    small files, each with a sha256, so an LLM grader can be asked to cite artifacts
    instead of trusting the agent's prose.
    """
    root = Path(args.workspace)
    listing, files, budget = [], [], args.max_total_bytes
    for entry in _iter_workspace(root):
        try:
            size = entry.stat().st_size
        except OSError:
            continue
        listing.append({"path": str(entry), "size_bytes": size})
        if len(files) >= args.max_files or budget <= 0:
            continue
        if entry.suffix.lower() not in TEXT_SUFFIXES or size > args.max_file_bytes:
            continue
        try:
            raw = entry.read_bytes()
        except OSError:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        take = min(len(text), budget, args.max_file_bytes)
        files.append({
            "path": str(entry),
            "sha256": hashlib.sha256(raw).hexdigest()[:16],
            "size_bytes": size,
            "truncated": take < len(text),
            "text": text[:take],
        })
        budget -= take
    requested = [p for p in (args.paths or "").split(",") if p.strip()]
    missing = [p for p in requested if not Path(p.strip()).exists()]
    return emit("pass", "evidence_collected", workspace=str(root), listing=listing[:200],
                files=files, missing_requested_paths=missing, file_count=len(listing))


def check_constraint_scan(args) -> int:
    """Deterministic half of a learning/runtime constraint requirement."""
    target = Path(args.entrypoint_path)
    if not target.is_file():
        return emit("fail", "no_runnable_entrypoint", entrypoint=str(target))
    findings, imported = [], []
    for entry in [target] + [e for e in _iter_workspace(Path(args.workspace)) if e.suffix == ".py" and e != target]:
        try:
            text = entry.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", text, re.MULTILINE):
            mod = match.group(1)
            imported.append({"file": str(entry), "module": mod})
            if mod not in STDLIB_MODULES and not Path(args.workspace, mod + ".py").exists():
                findings.append({"file": str(entry), "module": mod, "rule": "third_party_or_unknown_import"})
        if NETWORK_IMPORT_RE.search(text):
            findings.append({"file": str(entry), "rule": "network_access"})
        if re.search(r"pip\s+install", text):
            findings.append({"file": str(entry), "rule": "dependency_install"})
    ok = not findings
    return emit("pass" if ok else "fail", "constraints_respected" if ok else "constraint_violation",
                entrypoint=str(target), findings=findings[:10], modules_seen=sorted({i["module"] for i in imported}))


def check_deliverable_shape(args) -> int:
    """Public-contract SHAPE only (never values): what the simulator may see."""
    p = Path(args.deliverable)
    if not p.is_file():
        return emit("fail", "deliverable_missing", path=str(p))
    lines = [line for line in p.read_text(encoding="utf-8").strip().splitlines()]
    header_ok = lines[:1] == ["period,severity,count"]
    labels = [line.split(",")[0:2] for line in lines[1:]]
    expected_labels = [[p_, s] for p_ in ref.PERIODS for s in ref.SEVERITIES]
    order_ok = labels == expected_labels
    ints_ok = all(len(line.split(",")) == 3 and line.split(",")[2].strip().isdigit() for line in lines[1:])
    ok = header_ok and order_ok and ints_ok and len(lines) == 16
    return emit(
        "pass" if ok else "fail",
        "shape_ok" if ok else "shape_violation",
        path=str(p),
        header_ok=header_ok,
        row_order_ok=order_ok,
        counts_are_integers=ints_ok,
        line_count=len(lines),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True)
    ap.add_argument("--entrypoint-path", default="/app/summarize.py")
    ap.add_argument("--workload", default="/app/logs")
    ap.add_argument("--reference-date", default="2025-08-12")
    ap.add_argument("--alt-reference-date", default="2025-08-06")
    ap.add_argument("--deliverable", default="/app/summary.csv")
    ap.add_argument("--cap-peak-rss-kib", type=int, default=131072)
    ap.add_argument("--cap-wall-sec", type=float, default=120.0)
    ap.add_argument("--run-timeout", type=float, default=600.0)
    ap.add_argument("--workspace", default="/app")
    ap.add_argument("--paths", default="")
    ap.add_argument("--max-files", type=int, default=12)
    ap.add_argument("--max-file-bytes", type=int, default=20000)
    ap.add_argument("--max-total-bytes", type=int, default=40000)
    args = ap.parse_args()
    modes = {
        "entrypoint_present": check_entrypoint_present,
        "resource_budget": check_resource_budget,
        "semantic_matrix": check_semantic_matrix,
        "probe_reference_date": check_probe_reference_date,
        "clean_room_repro": check_clean_room_repro,
        "deliverable_shape": check_deliverable_shape,
        "evidence_bundle": check_evidence_bundle,
        "constraint_scan": check_constraint_scan,
    }
    if args.mode not in modes:
        print(f"unknown mode {args.mode}", file=sys.stderr)
        return 2
    try:
        return modes[args.mode](args)
    except Exception as exc:  # noqa: BLE001 - a checker crash is `unknown`, never `met`
        return emit("unknown", "checker_error", error=f"{type(exc).__name__}: {exc}"[:500])


if __name__ == "__main__":
    sys.exit(main())
