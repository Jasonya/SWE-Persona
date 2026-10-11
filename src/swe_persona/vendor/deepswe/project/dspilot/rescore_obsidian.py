"""Versioned, read-only rescore of Obsidian's saved hidden-test CTRF reports.

The pinned DeepSWE grader matches full Jest titles. ``ruleTest`` derives the
suite prefix from the submitted rule, so a different display name can make
all 41 tests appear missing even when they ran. This module matches the 41
fixed case titles within the pinned hidden-test file, failing closed on any
missing, duplicate, ambiguous, or foreign test. It never edits official runs.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import subprocess
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TASK_ID = "obsidian-linter-auto-table-of-contents"
EXPECTED_PREFIX = "Auto Table of Contents "
TEST_FILE = "/app/__tests__/auto-toc.test.ts"
SCORE_VERSION = "obsidian-ctrf-case-title-v1"
REPLAY_VERSION = "obsidian-report-adapter-v2-pinned-grader"


class RescoreError(ValueError):
    """Saved evidence is insufficient for a trustworthy rescore."""


def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RescoreError(f"Cannot read {path}: {exc}") from exc


def score_cases(report: dict, expected_ids: list[str]) -> tuple[int, str]:
    if len(expected_ids) != 41 or len(set(expected_ids)) != 41:
        raise RescoreError("Expected exactly 41 distinct pinned F2P IDs")
    if any(not name.startswith(EXPECTED_PREFIX) for name in expected_ids):
        raise RescoreError("F2P IDs do not use the pinned Obsidian suite prefix")
    suffixes = {name[len(EXPECTED_PREFIX):]: name for name in expected_ids}
    rows = (report.get("results") or {}).get("tests")
    if not isinstance(rows, list):
        raise RescoreError("CTRF report has no tests list")
    target = [row for row in rows if isinstance(row, dict) and row.get("filePath") == TEST_FILE]
    if len(target) != len(expected_ids):
        raise RescoreError(f"Expected 41 tests from {TEST_FILE}; found {len(target)}")

    matched: dict[str, str] = {}
    prefixes = set()
    for row in target:
        name = row.get("name")
        if not isinstance(name, str):
            raise RescoreError("A hidden test has no string name")
        candidates = [(suffix, expected) for suffix, expected in suffixes.items()
                      if name.endswith(" " + suffix)]
        if len(candidates) != 1:
            raise RescoreError(f"Hidden test title is missing or ambiguous: {name!r}")
        suffix, expected = candidates[0]
        prefix = name[:-(len(suffix) + 1)]
        if not prefix or expected in matched:
            raise RescoreError(f"Missing suite prefix or duplicate case: {name!r}")
        if row.get("suite") != f"auto-toc.test.ts > {prefix}":
            raise RescoreError(f"CTRF suite provenance differs from case title: {name!r}")
        status = row.get("status")
        if status not in ("passed", "failed", "skipped"):
            raise RescoreError(f"Unknown CTRF status for {name!r}: {status!r}")
        prefixes.add(prefix)
        matched[expected] = status
    if set(matched) != set(expected_ids) or len(prefixes) != 1:
        raise RescoreError("Hidden cases are incomplete or use inconsistent suite titles")
    return sum(status == "passed" for status in matched.values()), prefixes.pop()


def rescore_episode(root: Path, run_id: str, expected_ids: list[str]) -> dict:
    folder = root / "runs" / run_id
    result = read_json(folder / "result.json")
    if result.get("run_id") != run_id or result.get("task_id") != TASK_ID:
        raise RescoreError(f"Unexpected run identity: {run_id}")
    if result.get("evaluation_valid") is not True:
        raise RescoreError(f"Episode was not valid: {run_id}")
    original = result.get("reward")
    if not isinstance(original, dict) or original.get("f2p_total") != len(expected_ids):
        raise RescoreError(f"Official reward lacks the pinned 41 F2P tests: {run_id}")
    verifier = folder / "official" / "verifier"
    if read_json(verifier / "reward.json") != original:
        raise RescoreError(f"Official reward provenance differs from result.json: {run_id}")
    report_path = verifier / "reports" / "new_ctrf.json"
    report = read_json(report_path)
    f2p_passed, observed_prefix = score_cases(report, expected_ids)
    p2p_passed, p2p_total = original["p2p_passed"], original["p2p_total"]
    if not (isinstance(p2p_passed, int) and isinstance(p2p_total, int)
            and 0 <= p2p_passed <= p2p_total):
        raise RescoreError(f"Invalid original P2P counts: {run_id}")
    total = len(expected_ids) + p2p_total
    corrected = {
        "reward": int(f2p_passed == len(expected_ids) and p2p_passed == p2p_total),
        "f2p_total": len(expected_ids), "f2p_passed": f2p_passed,
        "p2p_total": p2p_total, "p2p_passed": p2p_passed,
        "f2p": f2p_passed / len(expected_ids),
        "p2p": p2p_passed / p2p_total if p2p_total else 0.0,
        "partial": (f2p_passed + p2p_passed) / total,
    }
    return {
        "run_id": run_id, "arm": result["arm"],
        "observed_suite_title": observed_prefix,
        "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "official_reward": original,
        "rescored_reward": corrected,
    }


def rescore_matrix(root: Path, prefix: str) -> dict:
    plan = read_json(root / "runs" / "matrices" / f"{prefix}.json")
    if plan.get("prefix") != prefix:
        raise RescoreError("Matrix prefix does not match its saved plan")
    config_path = root / ".cache" / "tasks" / TASK_ID / "tests" / "config.json"
    task = read_json(root / "data" / "tasks" / f"{TASK_ID}.json")
    pinned_hash = task["source"]["files"]["tests/config.json"]
    config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()
    if config_hash != pinned_hash:
        raise RescoreError("Obsidian grader config differs from the pinned task hash")
    config = read_json(config_path)
    expected_ids = config.get("f2p_node_ids")
    if not isinstance(expected_ids, list):
        raise RescoreError("Pinned grader config has no F2P whitelist")
    episodes = [item for item in plan["episodes"] if item["task_id"] == TASK_ID]
    if not episodes:
        raise RescoreError("Matrix contains no Obsidian episodes")
    rows = [rescore_episode(root, item["run_id"], expected_ids) for item in episodes]
    if any(row["arm"] != item["arm"] for row, item in zip(rows, episodes)):
        raise RescoreError("Recorded Obsidian arm differs from the matrix plan")
    summary = {}
    for arm in ("baseline", "control", "pivot"):
        group = [row for row in rows if row["arm"] == arm]
        if group:
            summary[arm] = {
                "episodes": len(group),
                "official_f2p_passed": sum(row["official_reward"]["f2p_passed"] for row in group),
                "rescored_f2p_passed": sum(row["rescored_reward"]["f2p_passed"] for row in group),
                "f2p_total": sum(row["rescored_reward"]["f2p_total"] for row in group),
                "official_strict_pass": sum(row["official_reward"]["reward"] for row in group),
                "rescored_strict_pass": sum(row["rescored_reward"]["reward"] for row in group),
            }
    return {
        "score_version": SCORE_VERSION,
        "note": "Analysis-only rescore; original pinned DeepSWE rewards and run files are unchanged.",
        "config_sha256": config_hash,
        "matrix_prefix": prefix, "summary": summary, "episodes": rows,
    }


def normalize_report(report: dict, expected_ids: list[str]) -> tuple[dict, str]:
    """Change only proven suite-name aliases; never change test outcomes."""
    _, prefix = score_cases(report, expected_ids)
    normalized = copy.deepcopy(report)
    for row in normalized['results']['tests']:
        if row.get('filePath') == TEST_FILE:
            row['name'] = EXPECTED_PREFIX + row['name'][len(prefix) + 1:]
            row['suite'] = 'auto-toc.test.ts > ' + EXPECTED_PREFIX.rstrip()
    score_cases(normalized, expected_ids)
    return normalized, prefix


def replay_saved_reports(root: Path, run_id: str, output: Path) -> dict:
    """Normalize copied reports, then run the byte-identical upstream grader.

    This is a separately versioned replay, never an overwrite of official reward.
    It uses no candidate code, model API, hidden-test execution, or reference patch.
    """
    if Path(run_id).name != run_id or run_id in ('.', '..'):
        raise RescoreError('run_id must be one directory name')
    folder = root / 'runs' / run_id
    task = read_json(root / 'data' / 'tasks' / (TASK_ID + '.json'))
    tests = root / '.cache' / 'tasks' / TASK_ID / 'tests'
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    expected_manifest = {n.removeprefix('tests/'): h for n, h in task['source']['files'].items()
                         if n.startswith('tests/')}
    if any(sha(tests / n) != h for n, h in expected_manifest.items()):
        raise RescoreError('Pinned evaluator inputs differ from the task manifest')
    integrity = read_json(folder / 'official' / 'integrity.json')
    if (integrity.get('status') != 'passed' or integrity.get('official_evaluator_modified') is not False
            or integrity.get('verifier_manifest') != expected_manifest
            or integrity.get('patch_sha256') != sha(folder / 'model.patch')):
        raise RescoreError('Saved evaluator provenance or patch identity is invalid')
    raw = folder / 'official' / 'verifier'
    reward_digest = hashlib.sha256(json.dumps(read_json(raw / 'reward.json'), sort_keys=True).encode()).hexdigest()
    if integrity.get('official_reward_sha256') != reward_digest:
        raise RescoreError('Saved official reward hash differs from integrity receipt')
    config = read_json(tests / 'config.json')
    if config['grade'] != {'format': 'ctrf', 'node_id': 'name',
                           'tool_label': 'jest-ctrf-json-reporter',
                           'reports': ['/logs/verifier/base_ctrf.json', '/logs/verifier/new_ctrf.json']}:
        raise RescoreError('Unsupported pinned report layout')
    analysis = rescore_episode(root, run_id, config['f2p_node_ids'])
    report, prefix = normalize_report(read_json(raw / 'reports' / 'new_ctrf.json'), config['f2p_node_ids'])
    # Validate both JSON inputs before constructing an output directory.
    read_json(raw / 'reports' / 'base_ctrf.json')
    output = output.resolve()
    if output == folder.resolve() or folder.resolve() in output.parents:
        raise RescoreError('Replay output must be outside the original episode')
    output.mkdir(parents=True, exist_ok=False)
    inputs = output / 'inputs'; inputs.mkdir()
    pinned = inputs / 'tests'; shutil.copytree(tests, pinned)
    shutil.copyfile(raw / 'reports' / 'base_ctrf.json', inputs / 'base_ctrf.json')
    (inputs / 'new_ctrf.json').write_text(json.dumps(report))
    verifier = output / 'verifier'; verifier.mkdir()
    source_hashes = {str(p.relative_to(folder)): sha(p) for p in
                     [folder / 'result.json', folder / 'model.patch', raw / 'reward.json',
                      raw / 'reports' / 'base_ctrf.json', raw / 'reports' / 'new_ctrf.json']}
    name = 'dspilot-report-replay-' + uuid.uuid4().hex[:12]
    command = ['docker', 'run', '--rm', '--name', name, '--platform', 'linux/amd64',
               '--network', 'none', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
               '--read-only', '--cpus', '1', '--memory', '512m', '--pids-limit', '64',
               '--mount', f'type=bind,source={pinned},target=/tests,readonly',
               '--mount', f'type=bind,source={verifier},target=/logs/verifier',
               '--mount', f'type=bind,source={inputs / "base_ctrf.json"},target=/logs/verifier/base_ctrf.json,readonly',
               '--mount', f'type=bind,source={inputs / "new_ctrf.json"},target=/logs/verifier/new_ctrf.json,readonly',
               '--entrypoint', 'python3', task['image'], '/tests/grader.py', 'grade']
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=30)
        raise RescoreError('Pinned grader replay timed out') from exc
    (output / 'grader-output.txt').write_text(proc.stdout + proc.stderr)
    if proc.returncode:
        raise RescoreError('Pinned grader replay failed; inspect the saved output')
    reward = read_json(verifier / 'reward.json')
    if reward != analysis['rescored_reward']:
        raise RescoreError('Unchanged grader disagrees with the independently matched case results')
    if any(sha(folder / n) != h for n, h in source_hashes.items()):
        raise RescoreError('Original episode changed during replay')
    if any(sha(pinned / n) != h or sha(tests / n) != h for n, h in expected_manifest.items()):
        raise RescoreError('Evaluator bytes changed during replay')
    result = {'version': REPLAY_VERSION, 'run_id': run_id, 'observed_suite_title': prefix,
              'official_reward': analysis['official_reward'], 'adapter_replay_reward': reward,
              'original_files_unchanged': True, 'evaluator_unchanged': True,
              'source_hashes': source_hashes, 'verifier_manifest': expected_manifest,
              'normalized_report_sha256': sha(inputs / 'new_ctrf.json'),
              'coding_episodes': 0, 'api_calls': 0,
              'note': 'Separate report-name adapter replay using the pinned grader; original reward and formal eligibility are unchanged.'}
    (output / 'result.json').write_text(json.dumps(result, indent=2))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prefix", help="Saved matrix prefix to rescore")
    mode.add_argument("--run-id", help="Saved episode to replay through the unchanged pinned grader")
    parser.add_argument("--output", type=Path, help="New replay directory outside the original episode")
    args = parser.parse_args()
    try:
        if args.run_id and not args.output:
            parser.error('--run-id requires --output')
        result = (replay_saved_reports(ROOT, args.run_id, args.output) if args.run_id
                  else rescore_matrix(ROOT, args.prefix))
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except RescoreError as exc:
        parser.exit(1, f"Rescore refused: {exc}\n")


if __name__ == "__main__":
    main()
