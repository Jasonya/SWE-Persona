#!/usr/bin/env python3
"""Checker-only driver for generic patch evidence and official tests."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import tempfile
import subprocess
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parent
APP_DIR = Path("/app")
MAX_ARTIFACT_TEXT = 16000
MAX_CHANGED_FILES = 8
MAX_DIFF_TEXT = 80000


def emit(status: str, reason_code: str, **evidence) -> int:
    print("CHECK " + json.dumps({"status": status, "reason_code": reason_code, "evidence": evidence}, ensure_ascii=False))
    return 0


def run(args, *, input_text=None, timeout=1800, env=None):
    return subprocess.run(
        args, cwd=APP_DIR, text=True, encoding="utf-8", errors="replace", input=input_text, capture_output=True,
        timeout=timeout, env=env,
    )


def changed_paths(base_commit: str):
    diff = run(["git", "diff", "--name-only", base_commit, "--"], timeout=120)
    status = run(["git", "status", "--porcelain=v1", "--untracked-files=all"], timeout=120)
    paths = {line.strip() for line in diff.stdout.splitlines() if line.strip()}
    for line in status.stdout.splitlines():
        if len(line) >= 4:
            paths.add(line[3:].split(" -> ")[-1])
    return sorted(paths), status.stdout.splitlines()


def check_task_patch_manifest(args) -> int:
    paths, status_lines = changed_paths(args.base_commit)
    head = run(["git", "rev-parse", "HEAD"], timeout=60).stdout.strip()
    common = {
        "head": head,
        "base_commit": args.base_commit,
        "changed_paths": paths,
        "initial_snapshot": {"base_commit": args.base_commit, "git_status": []},
        "delivery_manifest": {"existing_changes": [], "new_changes": paths},
    }
    if head != args.base_commit:
        return emit("fail", "head_moved", **common)
    if not paths:
        return emit("fail", "no_task_patch", **common)
    return emit("pass", "task_patch_recorded", status_lines=status_lines[:50], **common)


def check_isolated_patch(args) -> int:
    allowed = set(json.loads(args.allowed_paths))
    paths, status_lines = changed_paths(args.base_commit)
    head = run(["git", "rev-parse", "HEAD"], timeout=60).stdout.strip()
    out_of_scope = [path for path in paths if path not in allowed]
    common = {
        "head": head,
        "base_commit": args.base_commit,
        "changed_paths": paths,
        "out_of_scope": out_of_scope,
        "initial_snapshot": {"base_commit": args.base_commit, "git_status": []},
        "delivery_manifest": {"existing_changes": [], "new_changes": paths},
    }
    if head != args.base_commit:
        return emit("fail", "head_moved", **common)
    if not paths:
        return emit("fail", "no_task_patch", **common)
    if out_of_scope:
        return emit("fail", "out_of_scope_changes", allowed_paths=sorted(allowed), **common)
    return emit("pass", "patch_isolated", status_lines=status_lines[:50], **common)


def _artifact(path: str, raw: bytes, limit: int = MAX_ARTIFACT_TEXT):
    text = raw.decode("utf-8", errors="replace")
    return {
        "path": path,
        "sha256": hashlib.sha256(raw).hexdigest()[:16],
        "size_bytes": len(raw),
        "truncated": len(text) > limit,
        "text": text[:limit],
    }


def check_evidence_bundle(args) -> int:
    spec = json.loads((HARNESS / "task_spec.json").read_text(encoding="utf-8"))
    requested = [item for item in args.paths.split(",") if item]
    paths, _status = changed_paths(args.base_commit)
    for rel in paths[:MAX_CHANGED_FILES]:
        absolute = str(APP_DIR / rel)
        if absolute not in requested:
            requested.append(absolute)

    files = []
    listing = []
    missing = []
    for key in ("problem_statement", "requirements", "interface"):
        raw = str(spec.get(key) or "").encode()
        virtual = f"contract:{key}"
        files.append(_artifact(virtual, raw))
        listing.append({"path": virtual, "size_bytes": len(raw)})

    # Resolve every path before reading any candidate content. The checker has
    # private mounts that must not become rubric evidence through a symlink.
    resolved = []
    unsafe = []
    root = APP_DIR.resolve()
    for path in requested:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = APP_DIR / candidate
        try:
            relative = candidate.relative_to(APP_DIR)
            try:
                target = candidate.resolve(strict=True)
            except FileNotFoundError:
                target = candidate.resolve()
            target_relative = target.relative_to(root)
            if ".." in relative.parts or ".git" in relative.parts or ".git" in target_relative.parts:
                raise ValueError("non-public repository path")
        except (ValueError, OSError, RuntimeError):
            unsafe.append(hashlib.sha256(path.encode()).hexdigest())
            continue
        resolved.append((path, candidate, relative.as_posix()))
    if unsafe:
        return emit("unknown", "unsafe_evidence_path", rejected_count=len(unsafe),
                    rejected_path_sha256=unsafe)

    for path, candidate, rel in resolved:
        if not candidate.is_file():
            missing.append(path)
            continue
        raw = candidate.read_bytes()
        files.append(_artifact(path, raw))
        listing.append({"path": path, "size_bytes": len(raw)})

        old = run(["git", "show", f"{args.base_commit}:{rel}"], timeout=120)
        if old.returncode == 0:
            virtual = f"git:{args.base_commit}:{rel}"
            old_raw = old.stdout.encode()
            files.append(_artifact(virtual, old_raw))
            listing.append({"path": virtual, "size_bytes": len(old_raw)})

    diff = run(["git", "diff", "--no-ext-diff", "--binary", args.base_commit, "--"], timeout=180)
    diff_path = f"git:{args.base_commit}:task.diff"
    raw_diff = diff.stdout.encode()
    files.append(_artifact(diff_path, raw_diff, MAX_DIFF_TEXT))
    listing.append({"path": diff_path, "size_bytes": len(raw_diff)})
    return emit(
        "pass", "evidence_collected", listing=listing, files=files,
        missing_requested_paths=missing, file_count=len(listing), changed_paths=paths,
        changed_files_included=min(len(paths), MAX_CHANGED_FILES),
    )


def _safe_repo_path(raw: str) -> Path:
    path = Path(raw)
    if not raw or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe oracle test path: {raw!r}")
    return path


def _oracle_test_paths() -> list[str]:
    """Paths the evaluator owns, read from the hidden test patch's own headers."""
    paths: list[str] = []
    for line in (HARNESS / "test.patch").read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith(("+++ ", "--- ")):
            continue
        target = line[4:].strip()
        if target in ("/dev/null", ""):
            continue
        if target.startswith(("a/", "b/")):
            target = target[2:]
        try:
            paths.append(str(_safe_repo_path(target)))
        except ValueError:
            continue
    return sorted(set(paths))


def _apply_hidden_tests(base_commit: str) -> tuple[bool, str, dict]:
    """Apply the hidden test patch to the candidate worktree with ``git apply``.

    The evaluator's tests are applied, never checked out over the candidate: a
    submission that edited an evaluator-owned test path makes the patch fail to
    apply, and that is reported instead of being silently overwritten.  The
    conflict is therefore a real signal about the submission, and the evidence
    names the paths that caused it.
    """
    patch_path = HARNESS / "test.patch"
    oracle_paths = _oracle_test_paths()
    # Read the candidate's changed paths BEFORE applying: a successful apply itself
    # shows up in `git diff`, which would otherwise be reported as the candidate
    # having touched the evaluator's tests.
    candidate_paths, _status = changed_paths(base_commit)
    touched = sorted(set(oracle_paths) & set(candidate_paths))
    applied = run(
        ["git", "apply", "--binary", "--whitespace=nowarn", str(patch_path)],
        timeout=300,
    )
    overlay = {
        "strategy": "git_apply_test_patch",
        "oracle_paths": oracle_paths,
        "candidate_touched_oracle_paths": touched,
    }
    if applied.returncode != 0:
        overlay["apply_error"] = applied.stderr[-2000:] or applied.stdout[-2000:]
        return False, "hidden_test_patch_conflict", overlay
    return True, "hidden_test_patch_applied", overlay


def compilation_diagnostics(text: str) -> bool:
    """Only compiler diagnostics, never a missing executable/dependency signal."""
    environment_patterns=(r'command not found',r'no required module provides package',
        r'cannot find (?:module|package)',r'Could not resolve',r'Could not find or load main class',
        r'error while loading shared libraries',r'INTERNALERROR',r'cannot send \(already closed',
        r'(?m)^Killed$',r'Cannot allocate memory',r'No space left on device')
    if any(re.search(pattern,text,re.I) for pattern in environment_patterns):return False
    return bool(re.search(r'(?m)^\S+\.(?:go|java|rs|ts|tsx|c|cpp):\d+(?::\d+)?:.*'
                          r'(?:undefined:|multiple-value|too many arguments|not enough arguments|'
                          r'cannot use|error:|error TS\d+)',text)
                or re.search(r'error\[E\d+\]:',text))


def gold_execution_control(args, selected, required):
    """Run the same frozen tests on base+gold in an isolated worktree.

    A candidate compiler error is a functional failure only if the reference
    implementation passes the same required tests in this environment. No
    candidate file is reset, no test is weakened, and gold stays evaluator-only.
    """
    record={'policy':'reference-execution-control-v1','passed':False}
    with tempfile.TemporaryDirectory(prefix='swepro-gold-control-') as directory:
        root=Path(directory)/'repo'
        added=run(['git','worktree','add','--detach',str(root),args.base_commit],timeout=60)
        if added.returncode:
            record.update(reason='control_worktree_unavailable',error=added.stderr[-1000:]);return record
        try:
            for name in ('gold.patch','test.patch'):
                applied=subprocess.run(['git','apply','--binary','--whitespace=nowarn',str(HARNESS/name)],
                    cwd=root,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=60)
                if applied.returncode:
                    record.update(reason='control_patch_conflict',patch=name,error=applied.stderr[-1000:]);return record
            # An absolute repository jump would leave the isolated control.
            script=(HARNESS/'run_script.sh').read_text()
            if re.search(r'(?:cd|--directory|--cwd)\s+[\"\']?/app(?:/|\b)',script):
                record.update(reason='control_runner_requires_original_path');return record
            command=['bash',str(HARNESS/'run_script.sh')]
            if selected:command.append(','.join(selected))
            stdout=Path(directory)/'stdout';stderr=Path(directory)/'stderr'
            with stdout.open('w') as out,stderr.open('w') as err:
                proc=subprocess.run(command,cwd=root,stdout=out,stderr=err,timeout=300)
            record.update(runner_exit=proc.returncode,
                          stdout_tail=stdout.read_text(errors='replace')[-6000:],
                          stderr_tail=stderr.read_text(errors='replace')[-6000:])
            output=Path(directory)/'tests.json'
            parsed=subprocess.run([sys.executable,str(HARNESS/'parser.py'),str(stdout),str(stderr),str(output)],
                capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=60)
            if parsed.returncode or not output.is_file():
                record.update(reason='control_parser_failed');return record
            tests=json.loads(output.read_text())['tests']
            passed={str(t['name']) for t in tests if str(t.get('status','')).upper()=='PASSED'}
            record.update(passed=bool(required) and required<=passed,
                          required_count=len(required),passed_required_count=len(required & passed),
                          reason='control_tests_completed',
                          stdout_sha256=hashlib.sha256(stdout.read_bytes()).hexdigest(),
                          stderr_sha256=hashlib.sha256(stderr.read_bytes()).hexdigest())
            return record
        except (OSError,ValueError,KeyError,subprocess.TimeoutExpired) as exc:
            record.update(reason='control_execution_unavailable',error=type(exc).__name__);return record
        finally:
            run(['git','worktree','remove','--force',str(root)],timeout=60)


def check_official_tests(args) -> int:
    spec = json.loads((HARNESS / "task_spec.json").read_text(encoding="utf-8"))
    applied, apply_reason, test_overlay = _apply_hidden_tests(args.base_commit)
    if not applied:
        # `unknown`, not `fail`: no required test ran, so the functional outcome is
        # undetermined.  Reporting it as a failure would put a harness-level conflict
        # into the same bucket as a genuinely failing patch.
        return emit("unknown", apply_reason, test_overlay=test_overlay)

    selected = list(ast.literal_eval(spec["selected_test_files_to_run"]))
    runner_args = ["bash", str(HARNESS / "run_script.sh")]
    if selected:
        runner_args.append(",".join(selected))
    # Some official runners start Xvfb in the background.  If the runner exits
    # early under ``set -e`` (or simply omits cleanup), that orphan can retain
    # inherited stdout/stderr pipe descriptors forever.  Capturing with PIPE
    # would then make subprocess.run wait for EOF until the checker timeout even
    # though the test process already finished.  Files are immune to that pipe
    # lifetime issue and preserve the same parser inputs.
    runner_stdout_path = Path("/tmp/swepro_runner_stdout.log")
    runner_stderr_path = Path("/tmp/swepro_runner_stderr.log")
    with runner_stdout_path.open("w", encoding="utf-8") as stdout_handle, runner_stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr_handle:
        proc = subprocess.run(
            runner_args,
            cwd=APP_DIR,
            text=True,
            stdout=stdout_handle,
            stderr=stderr_handle,
            timeout=3600,
        )
    runner_stdout = runner_stdout_path.read_text(encoding="utf-8", errors="replace")
    runner_stderr = runner_stderr_path.read_text(encoding="utf-8", errors="replace")
    stdout_path = Path("/tmp/swepro_stdout.log")
    stderr_path = Path("/tmp/swepro_stderr.log")
    output_path = Path("/tmp/swepro_output.json")
    stdout_path.write_text(runner_stdout, encoding="utf-8")
    stderr_path.write_text(runner_stderr, encoding="utf-8")
    parsed = subprocess.run(
        [sys.executable, str(HARNESS / "parser.py"), str(stdout_path), str(stderr_path), str(output_path)],
        capture_output=True, text=True, timeout=180,
    )
    if parsed.returncode != 0 or not output_path.is_file():
        return emit(
            "unknown", "official_parser_failed", parser_error=parsed.stderr[-2000:],
            runner_exit=proc.returncode, runner_tail="\n".join(runner_stdout.splitlines()[-30:]),
            test_overlay=test_overlay,
        )
    try:
        output = json.loads(output_path.read_text(encoding="utf-8"))
        results = output["tests"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return emit(
            "unknown", "official_parser_invalid_output",
            error=f"{type(exc).__name__}: {exc}"[:500], test_overlay=test_overlay,
        )
    observed = {str(item["name"]) for item in results if item.get("name") is not None}
    passed = {str(item["name"]) for item in results if str(item.get("status", "")).upper() == "PASSED"}
    fail_to_pass = set(ast.literal_eval(spec["fail_to_pass"]))
    pass_to_pass = set(ast.literal_eval(spec["pass_to_pass"]))
    required = fail_to_pass | pass_to_pass
    observed_required = required & observed
    if required and not observed_required:
        combined=runner_stdout+'\n'+runner_stderr
        control=None
        if compilation_diagnostics(combined):
            control=gold_execution_control(args,selected,required)
            if control['passed']:
                return emit('fail','official_candidate_compilation_failed',reward=0,
                    failure_category='candidate_code',required_count=len(required),
                    observed_required_count=0,parsed_test_count=len(results),
                    runner_exit=proc.returncode,runner_tail=combined.splitlines()[-30:],
                    reference_control=control,test_overlay=test_overlay)
        # No executed required test and no passing reference control: keep the
        # functional verdict unknown. Signals remain explicit for diagnosis.
        return emit(
            "unknown", "official_required_tests_not_observed",
            failure_category="test_environment_unavailable",reference_control=control,
            summary_lines=[f"0/{len(required)} required tests were observed by the parser"],
            required_count=len(required), observed_required_count=0,
            parsed_test_count=len(results), runner_exit=proc.returncode,
            runner_tail="\n".join(runner_stdout.splitlines()[-30:]),
            test_overlay=test_overlay,
        )
    missing = sorted(required - passed)
    passed_count = len(required & passed)
    return emit(
        "pass" if not missing else "fail",
        "official_tests_passed" if not missing else "official_tests_failed",
        reward=1 if not missing else 0,
        summary_lines=[f"{passed_count}/{len(required)} required tests passed"],
        required_count=len(required), passed_required_count=passed_count,
        failed_required_count=len(missing), failed_tests=missing,
        parsed_test_count=len(results), runner_exit=proc.returncode,
        runner_tail="\n".join(runner_stdout.splitlines()[-30:]),
        test_overlay=test_overlay,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", required=True,
        choices=["task_patch_manifest", "isolated_patch", "evidence_bundle", "official_tests"],
    )
    parser.add_argument("--base-commit", required=True)
    parser.add_argument("--allowed-paths", default="[]")
    parser.add_argument("--paths", default="")
    args = parser.parse_args()
    try:
        return {
            "task_patch_manifest": check_task_patch_manifest,
            "isolated_patch": check_isolated_patch,
            "evidence_bundle": check_evidence_bundle,
            "official_tests": check_official_tests,
        }[args.mode](args)
    except subprocess.TimeoutExpired as exc:
        return emit("unknown", "checker_timeout", error=str(exc)[:500])
    except Exception as exc:  # noqa: BLE001
        return emit("unknown", "checker_error", error=f"{type(exc).__name__}: {exc}"[:500])


if __name__ == "__main__":
    raise SystemExit(main())
