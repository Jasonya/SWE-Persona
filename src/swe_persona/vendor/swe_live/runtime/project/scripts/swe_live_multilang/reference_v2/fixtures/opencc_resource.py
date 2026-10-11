"""Public amino-acid workload plus measured resource and full-output controls."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

if __package__:
    from .linux_measure import measure
else:
    from linux_measure import measure

REPEATS = 100000
CAP_KIB = 131072
CAP_SECONDS = 30
INPUT = "\u80fa\u57fa\u9178\n\u7518\u80fa\u9178\n"
EXPECTED = "\u6c28\u57fa\u9178\n\u7518\u6c28\u9178\n"


def verdict(record):
    if any(k not in record for k in ("measurement", "output_sha256", "semantic_sha256", "expected_sha256", "input_unchanged")):
        return "unknown", "resource_evidence_missing"
    measured = record["measurement"]
    if measured["timeout"] or measured["wall_seconds"] > CAP_SECONDS:
        return "fail", "resource_wall_limit_exceeded"
    if measured["peak_tree_rss_kib_sampled"] > CAP_KIB:
        return "fail", "sampled_process_tree_memory_limit_exceeded"
    if measured["exit_code"] or measured["background_processes_after_parent_exit"]:
        return "fail", "workload_did_not_complete"
    if not record["input_unchanged"] or record.get("semantic_error") or record["semantic_sha256"] != record["expected_sha256"]:
        return "fail", "workload_output_or_input_coverage_mismatch"
    return "pass", "complete_workload_within_registered_limits"


def run_workload(command, directory, name):
    source = directory / "input.txt"
    output = directory / (name + ".txt")
    expected = (EXPECTED * REPEATS).encode()
    original = source.read_bytes()
    before = hashlib.sha256(original).hexdigest()
    measured = measure(command, output, cwd="/testbed", seconds=CAP_SECONDS)
    actual = output.read_bytes()
    # Inspect is an existing public line-wise entry point. Ordinary file-mode
    # streaming has an unrelated baseline chunk-boundary defect in this image
    # (retained calibration receipts locate it near byte 1048533). Do not make
    # repairing that unrelated C++ defect a new demand in a dictionary task.
    semantic = hashlib.sha256()
    semantic_error, count = None, 0
    try:
        lines = actual.decode("utf-8").split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        inputs = INPUT.splitlines()
        for index, line in enumerate(lines):
            row = json.loads(line)
            if row.get("input") != inputs[index % 2] or not isinstance(row.get("output"), str):
                raise ValueError("Input identity or output type mismatch")
            semantic.update((row["output"] + "\n").encode())
            count += 1
        if count != REPEATS * 2:
            semantic_error = "incomplete_or_extra_records"
    except (ValueError, TypeError, AttributeError, UnicodeError):
        semantic_error = "invalid_inspection_output"
    unchanged = source.is_file() and hashlib.sha256(source.read_bytes()).hexdigest() == before
    record = {"name": name, "command": command, "input_sha256": before, "input_bytes": len(original),
              "input_lines": REPEATS * 2, "input_unchanged": unchanged,
              "output_sha256": hashlib.sha256(actual).hexdigest(),
              "output_bytes": output.stat().st_size,
              "output_preview": actual[:200].decode("utf-8", "replace"),
              "semantic_sha256": semantic.hexdigest(), "semantic_error": semantic_error, "records_checked": count,
              "expected_sha256": hashlib.sha256(expected).hexdigest(), "measurement": measured}
    status, reason = verdict(record)
    return record | {"status": status, "reason": reason}


def main():
    root = Path("/testbed")
    calibration = "--calibration" in sys.argv
    # Force rebuilding candidate code instead of trusting shipped binaries.
    build = subprocess.run(["cmake", "--build", "build", "--clean-first", "-j2"], cwd=root,
                           capture_output=True, text=True, timeout=600)
    if build.returncode:
        print(json.dumps({"base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
                          "candidate_build_failed": True, "records": [],
                          "build": {"exit_code": build.returncode, "output_tail": (build.stdout + build.stderr)[-4000:]}}))
        return
    with tempfile.TemporaryDirectory(prefix="persona-opencc-resource-") as temporary:
        folder = Path(temporary)
        source = folder / "input.txt"
        source.write_bytes((INPUT * REPEATS).encode())
        source.chmod(0o444)
        command = [str(root / "build/src/tools/opencc"), "--path", str(root / "build/data"),
                   "-c", str(root / "data/config/tw2sp.json"), "--inspect", "-i", str(source)]
        records = []
        if calibration:
            records.append(run_workload(command, folder, "baseline_public_gap"))
            # An independently authored calibration supplement for these two
            # PUBLIC inputs only; never applied to an agent or official task.
            dictionary = root / "data/dictionary/TWPhrasesRev.txt"
            entries = dict(line.split("\t", 1) for line in dictionary.read_text().splitlines()
                           if line and not line.startswith("#"))
            entries.update({"\u80fa\u57fa\u9178": "\u6c28\u57fa\u9178", "\u7518\u80fa\u9178": "\u7518\u6c28\u9178"})
            controlled = folder / "TWPhrasesRev.txt"
            controlled.write_text("\n".join(k + "\t" + v for k, v in sorted(entries.items())) + "\n")
            subprocess.run([str(root / "build/src/tools/opencc_dict"), "-i", str(controlled),
                            "-o", str(root / "build/data/TWPhrasesRev.ocd2"), "-f", "text", "-t", "ocd2"], check=True,
                           capture_output=True)
        # Distinct fresh processes; the second retains OS file cache only.
        for name in ("cold_process", "warm_filesystem_cache"):
            records.append(run_workload(command, folder, name))
        if calibration:
            two_rows = "\n".join(json.dumps({"input": i, "output": o}, ensure_ascii=False)
                                  for i, o in zip(INPUT.splitlines(), EXPECTED.splitlines())) + "\n"
            prefix = "import sys;sys.stdout.write(" + repr(two_rows) + ")"
            records.append(run_workload(["python3", "-c", prefix], folder, "truncated_output"))
            # Child memory must count, even though the parent is tiny.
            hog = "import time;x=bytearray(160*1024*1024);time.sleep(.3)"
            parent = ("import subprocess,sys;child=subprocess.Popen([sys.executable,'-c'," + repr(hog) + "]);"
                      "sys.stdout.write(" + repr(two_rows) + "*" + str(REPEATS) + ");child.wait()")
            records.append(run_workload(["python3", "-c", parent], folder, "child_memory_exceeded"))
        print(json.dumps({"base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
                          "calibration_only_supplement": calibration, "records": records,
                          "limits": {"sampled_tree_rss_kib": CAP_KIB, "wall_seconds_per_run": CAP_SECONDS},
                          "build": {"exit_code": build.returncode, "output_tail": (build.stdout + build.stderr)[-2000:]}}))


if __name__ == "__main__":
    main()
