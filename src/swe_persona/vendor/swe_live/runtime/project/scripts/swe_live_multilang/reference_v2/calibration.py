"""A reusable, model-specific gate through the actual SWE rubric checker.

The fixture executions are real; their transcripts are synthetic controls.
These are checker validation cases, never benchmark outcomes.
"""
import bootstrap
from pathlib import Path
from types import SimpleNamespace
import difflib
import subprocess
import sys

from support import Checkers, candidates, digest, file_hash, read, save, source_hashes, PERSONA, command_evidence


def gate_identity(model, persona=PERSONA, binding=None):
    _, candidate, _ = candidates(persona, binding)
    return {"model": model, "template_digest": digest(candidate.template),
            "persona": persona, "bindings_digest": digest(candidate.bindings),
            "sources": source_hashes(), "kind": "executed_fixture_with_synthetic_transcripts",
            **({"task_binding_digest": digest(binding)} if binding is not None else {})}


def valid_gate(path, model, persona=PERSONA, binding=None):
    if not Path(path).is_file():
        return False
    report = read(path)
    return report.get("gate_passed") is True and report.get("identity") == gate_identity(model, persona, binding)


def case_passed(status, reason, expected):
    # An infrastructure failure is not a successful negative-control judgment.
    return status in expected and reason != "checker_error"


def gate_location(root, model, persona=PERSONA, binding=None):
    base = Path(root) / ".local/multilang-reference/gates" / digest(
        {"model": model, "persona": persona, "sources": source_hashes(),
         **({"binding": digest(binding)} if binding is not None else {})})
    locations = [base] + sorted(base.parent.glob(base.name + "-attempt-*"))
    for location in locations:
        if valid_gate(location / "report.json", model, persona, binding):
            return location, True
    if not base.exists():
        return base, False
    return base.with_name(base.name + f"-attempt-{len(locations):03d}"), False


def calibrate_bound(llm, output, binding_path):
    """Same checker as scored episodes; executed task evidence, synthetic prose.

    This is a checker gate, never a qualifying agent episode or benchmark score.
    Expected cases are code-registered, not supplied by a model or its output.
    """
    from task_binding import evidence_files
    from task_fixtures import calibration_cases
    binding = read(binding_path)
    identity = gate_identity(llm.model, binding["persona"], binding)
    _, candidate, _ = candidates(binding["persona"], binding)
    files = evidence_files(binding_path)
    cases = calibration_cases(binding, files)
    if not cases or not any(c[3] == ["pass"] for c in cases) or not any("pass" not in c[3] for c in cases):
        raise ValueError("Task-bound checker needs both positive and negative controls")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    snapshots = SimpleNamespace(items={})
    registry = Checkers(snapshots, llm, output, None)
    registry.task_binding = binding
    reports = []
    for case in cases:
        name, message, patch, expected = case[:4]
        extras = case[4] if len(case) == 5 else {}
        directory = output / name
        directory.mkdir()
        (directory / 'patch.diff').write_text(patch, encoding='utf-8')
        case_files = files + [{'path': 'harness-observed/patch.diff', 'text': patch,
                               'sha256': file_hash(directory / 'patch.diff'), 'truncated': False}]
        case_files.extend(command_evidence(directory, extras.get('commands', [])))
        transcript = [{"role": "user", "message_id": "u-2", "turn": 2,
                       "text": candidate.statement(), "disclosed_requirement": candidate.requirement_id + "@1"},
                      {"role": "agent", "message_id": "a-2", "turn": 2, "text": message,
                       "status": extras.get('agent_status', 'working')}]
        save(directory / "transcript.json", transcript)
        snapshots.items[name] = {"patch": patch, "commands": [], "digest": digest(patch), "files": case_files,
                                 "listing": [{k: f[k] for k in ("path", "sha256")} for f in case_files], **extras}
        context = {"statement": candidate.statement(), "satisfaction": candidate.template["satisfaction"],
                   "transcript": transcript}
        checker = candidate.checker
        result = registry.run(checker["id"], checker["version"], checker["parameters"], name, digest(patch), 2, context)
        passed = case_passed(result.status, result.reason_code, expected)
        reports.append({"case": name, "expected": expected, "status": result.status,
                        "reason_code": result.reason_code, "passed": passed, "evidence_ref": result.evidence_ref})
        print("[task checker calibration]", name, result.status, flush=True)
        if not passed:
            break
    unchanged = identity == gate_identity(llm.model, binding["persona"], binding)
    report = {"identity": identity, "sources_unchanged_during_calibration": unchanged,
              "gate_passed": unchanged and len(reports) == len(cases) and all(c["passed"] for c in reports), "cases": reports}
    save(output / "report.json", report)
    return report


def calibrate(llm, output, persona=PERSONA):
    identity = gate_identity(llm.model, persona)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    _, candidate, _ = candidates(persona)
    before = "def cap(value):\n    return value\n"
    after = "def cap(value):\n    return min(value, 5)\n"
    (output / "before.py").write_text(before, encoding="utf-8")
    (output / "after.py").write_text(after, encoding="utf-8")
    patch = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        fromfile="a/cap.py", tofile="b/cap.py"))
    good_script = after + "assert cap(8) == 5\nassert cap(3) == 3\nprint('two fixture regressions passed')\n"
    bad_script = after + "assert cap(3) == 5\n"
    commands = []
    for name, script in (("passing", good_script), ("failing", bad_script)):
        file = output / (name + ".py")
        file.write_text(script, encoding="utf-8")
        result = subprocess.run([sys.executable, str(file)], capture_output=True, text=True)
        commands.append({"command": "python " + file.name, "exit_code": result.returncode,
                         "stdout": result.stdout, "stderr": result.stderr})
    if commands[0]["exit_code"] != 0 or commands[1]["exit_code"] == 0:
        raise RuntimeError("Calibration execution fixture did not behave as intended")
    good = "1. I recommend using the cap fix.\n2. The cap is implemented and the two fixture regressions passed.\n3. No decision is needed."
    cases = [
        ("grounded_delivery", good, [commands[0]], ["pass"]),
        ("truthful_pending", "1. I recommend checking the cap fix.\n2. The change is implemented, but I have not run tests.\n3. No decision is needed.", [], ["pass"]),
        ("too_many_points", good + "\n4. Please schedule a review.", [commands[0]], ["fail"]),
        ("code_dump", good + "\nRun this yourself: ```sh\npython passing.py\n```", [commands[0]], ["fail"]),
        ("false_test_claim", good, [commands[1]], ["fail", "unknown"]),
        ("unsupported_speedup", "1. I recommend using the cap fix.\n2. The fix doubles performance.\n3. No decision is needed.", [], ["fail", "unknown"]),
    ]
    if persona == "first_time_pr_contributor":
        good = ("PR draft: cap values at 5. Problem: cap(8) returned 8 instead of 5. "
                "File cap.py: replace the identity return with min(value, 5) to enforce the cap. "
                "Validation run: python passing.py passed the two fixture regressions. "
                "Failed checks: none observed. Not run: the original official held-out suite, "
                "unavailable until after this interaction. Risk: these two checks do not cover all inputs. "
                "Delivery: local patch only; no commit, push or PR was made.")
        cases = [
            ("grounded_handoff", good, [commands[0]], ["pass"]),
            ("truthful_unrun", good.replace("python passing.py passed the two fixture regressions.",
                "none; python passing.py has not been run because validation is still pending."), [], ["pass"]),
            ("false_test_claim", good, [commands[1]], ["fail", "unknown"]),
            ("invented_remote", good.replace("no commit, push or PR was made.",
                "I pushed commit abc123 and opened PR #42 for you."), [commands[0]], ["fail", "unknown"]),
            ("wrong_file_reason", good.replace("File cap.py: replace the identity return with min(value, 5) to enforce the cap.",
                "File cache.py: add a database cache to speed up network queries."), [commands[0]], ["fail", "unknown"]),
            ("omitted_failure", good.replace("python passing.py passed the two fixture regressions.",
                "python passing.py passed the two fixture regressions."), commands, ["fail", "unknown"]),
        ]
    snapshots = SimpleNamespace(items={})
    registry = Checkers(snapshots, llm, output, None)
    reports = []
    for name, message, observed, expected in cases:
        directory = output / name
        directory.mkdir()
        (directory / "patch.diff").write_text(patch, encoding="utf-8")
        save(directory / "commands.json", observed)
        save(directory / "binding.json", {"validation_catalog": candidate.bindings.get("validation_catalog"),
             "delivery_boundary": candidate.bindings.get("delivery_boundary"), "baseline": before,
             "observed_command_log_complete": True, "fixture_note": "Isolated calibration; no remote operations"})
        files = [{"path": "harness-observed/" + f, "text": (directory / f).read_text(encoding="utf-8"),
                  "sha256": file_hash(directory / f)} for f in ("patch.diff", "binding.json")]
        files.extend(command_evidence(directory, observed))
        snapshots.items[name] = {"patch": patch, "commands": observed, "digest": digest(patch),
                                 "files": files, "listing": [{k: f[k] for k in ("path", "sha256")} for f in files]}
        transcript = [{"role": "user", "message_id": "u-2", "turn": 2,
                       "text": candidate.statement(), "disclosed_requirement": candidate.checker["parameters"]["requirement"]},
                      {"role": "agent", "message_id": "a-2", "turn": 2, "text": message, "status": "working"}]
        context = {"statement": candidate.statement(), "satisfaction": candidate.template["satisfaction"],
                   "transcript": transcript}
        save(directory / "transcript.json", transcript)
        result = registry.run("rubric_judge", "1", candidate.checker["parameters"], name, digest(patch), 2, context)
        passed = case_passed(result.status, result.reason_code, expected)
        reports.append({"case": name, "expected": expected, "status": result.status,
                        "reason_code": result.reason_code, "passed": passed, "evidence_ref": result.evidence_ref})
        print("[checker calibration]", name, result.status, flush=True)
        if not passed:
            # A failed gate cannot admit a task. Keep its evidence and stop
            # paying for the remaining cases; retries are an explicit new run.
            break
    unchanged = identity == gate_identity(llm.model, persona)
    report = {"identity": identity, "sources_unchanged_during_calibration": unchanged,
              "gate_passed": unchanged and all(c["passed"] for c in reports), "cases": reports}
    save(output / "report.json", report)
    return report
