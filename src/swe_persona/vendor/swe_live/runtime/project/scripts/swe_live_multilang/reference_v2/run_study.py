"""Frozen 20-task / 60-episode study. --run authorizes model calls.

Sequential Docker jobs fit this PC's 23 GiB Docker allocation. Successful and
valid failed outcomes are both retained; there is no replacement or score-based retry.
"""
import bootstrap
from bootstrap import ROOT, SCRIPTS
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

from personas import PERSONAS
from support import PROTOCOL, REVISION, Manifest, digest, file_hash, read, save, source_hashes
from run_reference import gold_proof

LANGUAGES = ("c", "cpp", "go", "js", "rust", "java", "ts", "cs")
ARMS = ("original", "persona0", "persona1")
HERE = Path(__file__).resolve().parent
DEFAULT_STUDY = ROOT / ".local/multilang-study/study-20-v5"


def select_tasks(splits, count, seed):
    """Balanced language allocation; within each language rank ONLY public IDs."""
    selected = []
    for i, language in enumerate(LANGUAGES):
        size = count // len(LANGUAGES) + int(i < count % len(LANGUAGES))
        ranked = sorted(splits[language], key=lambda row: hashlib.sha256(
            (seed + ":" + row["instance_id"]).encode()).hexdigest())
        if len(ranked) < size:
            raise ValueError("Insufficient tasks for " + language)
        selected.extend((language, row) for row in ranked[:size])
    return selected


def prepare(study, config):
    study = Path(study)
    path = study / "manifest.json"
    if path.exists():
        manifest = read(path)
        if manifest["config"] != config or manifest["protocol"] != PROTOCOL:
            raise ValueError("Frozen study configuration differs; use a new study directory")
        if manifest["sources"] != source_hashes():
            raise ValueError("Production source changed after freezing; do not mix protocols in this study")
        for task in manifest["tasks"]:
            if file_hash(study / task["path"]) != task["sha256"]:
                raise ValueError("Frozen task changed: " + task["instance_id"])
        return manifest
    selection = config.get("diagnostic_selection")
    if selection:
        source = Path(selection["source_study"])
        if file_hash(source / "manifest.json") != selection["manifest_sha256"]:
            raise ValueError("Source manifest changed")
        original = read(source / "manifest.json")
        by_key = {t["key"]: t for t in original["tasks"]}
        keys = selection["task_keys"]
        if len(set(keys)) != len(keys) or not set(keys) <= by_key.keys():
            raise ValueError("Diagnostic keys must be unique and present in the frozen source study")
        if len(keys) != config["tasks"] or original["dataset_revision"] != REVISION:
            raise ValueError("Diagnostic task count or dataset revision mismatch")
        # Copy exact frozen rows, retaining task keys. No network or outcome selection.
        tasks = []
        for key in keys:
            task = by_key[key]
            data = (source / task["path"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != task["sha256"]:
                raise ValueError("Source task changed: " + key)
            target = study / task["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            Manifest(target)
            tasks.append(dict(task))
        manifest = {"protocol": PROTOCOL, "dataset_revision": REVISION, "config": config,
                    "created_utc": datetime.now(timezone.utc).isoformat(), "sources": source_hashes(),
                    "tasks": tasks, "arms": list(ARMS), "personas": list(PERSONAS),
                    "selection": "Explicit diagnostic subset of an earlier frozen study; not a representative effectiveness estimate; no replacement on failure",
                    "comparison": original["comparison"]}
        save(path, manifest)
        return manifest
    from datasets import load_dataset
    splits = {}
    for language in LANGUAGES:
        print("[prepare]", language, flush=True)
        splits[language] = load_dataset("SWE-bench-Live/MultiLang", split=language,
            revision=REVISION, cache_dir=str(ROOT / ".local/hf-cache"))
    tasks = []
    for index, (language, row) in enumerate(select_tasks(splits, config["tasks"], config["seed"])):
        key = f"{index + 1:02d}-{language}"
        target = study / "tasks" / key / "task.private.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        Manifest(target)
        tasks.append({"key": key, "language": language, "instance_id": row["instance_id"],
                      "path": target.relative_to(study).as_posix(), "sha256": file_hash(target)})
    manifest = {"protocol": PROTOCOL, "dataset_revision": REVISION, "config": config,
                "created_utc": datetime.now(timezone.utc).isoformat(), "sources": source_hashes(),
                "tasks": tasks, "arms": list(ARMS), "personas": list(PERSONAS),
                "selection": "Balanced language counts; SHA256(seed:public instance_id) ranking; no replacement on failure",
                "comparison": "Original = single full-issue turn; personas = reference vague opening + evolving requirement. Differences include interaction and information disclosure, not a pure persona causal effect."}
    save(path, manifest)
    return manifest


def attempts(parent):
    return sorted(p for p in Path(parent).glob("attempt-*") if p.is_dir())


def can_skip(run):
    # Valid failures are completed data, not candidates for retry.
    from audit_sample import audit
    return audit(run)["ready_for_review"]


def execute(command, log):
    log.parent.mkdir(parents=True, exist_ok=True)
    print("[run]", log, flush=True)
    with log.open("w", encoding="utf-8") as out:
        return subprocess.run(command, stdout=out, stderr=subprocess.STDOUT).returncode


def verification_budget(gold_seconds, maximum):
    # Same value for all three arms, fixed BEFORE model outcomes are observed.
    return min(maximum, max(900, math.ceil(1.5 * gold_seconds + 180)))


def ensure_gold(study, task, image_id, config, retry):
    manifest = Manifest(study / task["path"])
    directory = study / "gold" / task["key"]
    directory.mkdir(parents=True, exist_ok=True)
    try:
        gold_proof(directory, manifest, image_id)
        return read(directory / "timing.json")["seconds"]
    except (ValueError, FileNotFoundError):
        pass
    old = attempts(directory)
    if old and not retry:
        return None
    run = directory / f"attempt-{len(old) + 1:03d}"
    run.mkdir()
    start = time.monotonic()
    rc = execute([sys.executable, "-u", str(SCRIPTS / "run_evaluation.py"),
        "--dataset", str(manifest.task_path), "--patch_dir", "gold", "--platform", "linux",
        "--workers", "1", "--overwrite", "0", "--wall-timeout-seconds", str(config["gold_seconds"]),
        "--output_dir", str(run / "evaluation")], run / "evaluation.log")
    seconds = time.monotonic() - start
    file = run / "evaluation/results.json"
    report = read(file) if file.exists() else {}
    save(run / "attempt.json", {"exit_code": rc, "seconds": seconds})
    if rc or any(report.get(k) != v for k, v in {"submitted": 1, "success": 1, "error": 0, "incomplete": 0}.items()):
        return None
    save(directory / "validated.json", {"image_id": image_id, "task_sha256": task["sha256"]})
    save(directory / "timing.json", {"seconds": seconds})
    return seconds


def ensure_gates(study, config, retry=False):
    from calibration import calibrate, gate_location
    from execution_policy import BudgetClient
    from llm import CallLog, Llm
    from run_episode import _assert_rubric_model_gated
    model = config["model"]
    _assert_rubric_model_gated(model, True)
    for persona in PERSONAS:
        cache, cached = gate_location(ROOT, model, persona)
        if cached:
            continue
        if "-attempt-" in cache.name and not retry:
            raise RuntimeError("Prior checker gate failed; inspect it before a new study: " + str(cache))
        log = CallLog(path=study / ("calibration-" + persona + ".jsonl"))
        report = calibrate(BudgetClient(Llm(model=model, call_log=log), config["request_seconds"]), cache, persona)
        save(study / ("gate-" + persona + ".json"), report)
        if not report["gate_passed"]:
            raise RuntimeError("Checker gate failed: " + persona)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--study-dir", type=Path, default=DEFAULT_STUDY)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--retry-episode", action="append", default=[], metavar="TASK_KEY/ARM",
                        help="Retry one invalid attempt without rerunning other failures; repeatable")
    parser.add_argument("--model", default="gpt-6-astra")
    parser.add_argument("--tasks", type=int, default=20)
    parser.add_argument("--seed", default="silvia-multilang-20-v1")
    parser.add_argument("--from-study", type=Path, help="Copy a diagnostic subset from exact frozen local task rows")
    parser.add_argument("--task-keys", nargs="+", help="Explicit diagnostic task keys; requires --from-study")
    parser.add_argument("--interaction-seconds", type=float, default=900)
    parser.add_argument("--request-seconds", type=float, default=90)
    parser.add_argument("--gold-seconds", type=float, default=5400)
    parser.add_argument("--max-verification-seconds", type=float, default=5400)
    args = parser.parse_args()
    if bool(args.from_study) != bool(args.task_keys):
        parser.error("Use --from-study and --task-keys together")
    if args.from_study and args.from_study.resolve() == args.study_dir.resolve():
        parser.error("Use a new study directory; historical evidence is immutable")
    if args.task_keys:
        args.tasks = len(args.task_keys)
    if args.tasks < 1 or min(args.interaction_seconds, args.request_seconds, args.gold_seconds, args.max_verification_seconds) <= 0:
        parser.error("Counts and budgets must be positive")
    config = {"tasks": args.tasks, "seed": args.seed, "model": args.model,
              "interaction_seconds": args.interaction_seconds, "request_seconds": args.request_seconds,
              "gold_seconds": args.gold_seconds, "max_verification_seconds": args.max_verification_seconds,
              "verification_policy": "min(maximum, max(900, ceil(1.5 * gold_wall_seconds + 180)))",
              "max_turns": 8, "max_steps": 14, "single_turn_steps": 40, "docker_workers": 1,
              "container_memory": "16g", "container_cpus": 4}
    if args.from_study:
        config["diagnostic_selection"] = {"source_study": str(args.from_study.resolve()),
            "manifest_sha256": file_hash(args.from_study / "manifest.json"), "task_keys": args.task_keys}
    os.environ.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    os.environ["PATH"] = str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Docker/Docker/resources/bin") + os.pathsep + os.environ["PATH"]
    study = args.study_dir.resolve()
    study.mkdir(parents=True, exist_ok=True)
    lock = study / "study.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise SystemExit("Study is locked; check its process before resuming: " + str(lock))
    with os.fdopen(fd, "w") as out:
        json.dump({"pid": os.getpid(), "started_utc": datetime.now(timezone.utc).isoformat()}, out)
    client = None
    manifest = None
    try:
        # Hold a power request only while running. Do not change system power settings.
        if args.run and os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
        manifest = prepare(study, config)
        allowed_retries = {t["key"] + "/" + arm for t in manifest["tasks"] for arm in ARMS}
        if not set(args.retry_episode) <= allowed_retries:
            raise ValueError("Unknown --retry-episode; use a task key / arm from the manifest")
        from report_study import report
        report(study)
        print(f"Frozen {len(manifest['tasks'])} tasks x 3 arms = {len(manifest['tasks']) * 3} episodes", flush=True)
        if not args.run:
            print("No Docker jobs or API calls. Prepared:", study, flush=True)
            return 0
        import docker
        client = docker.from_env(timeout=180)
        info = client.info()
        if info["OSType"] != "linux" or info["MemTotal"] < 18 * 1024**3:
            raise RuntimeError("Linux Docker with at least 18 GiB required")
        ensure_gates(study, config, args.retry_failed)
        for task in manifest["tasks"]:
            print("[task]", task["key"], task["instance_id"], flush=True)
            state_file = study / (task["key"] + "-state.json")
            state = {"task": task["instance_id"], "status": "preparing"}
            save(state_file, state)
            try:
                task_manifest = Manifest(study / task["path"])
                try:
                    image = client.images.get(task_manifest.docker_image)
                except docker.errors.ImageNotFound:
                    print("[image] pulling", task_manifest.docker_image, flush=True)
                    image = client.images.pull(task_manifest.docker_image)
                seconds = ensure_gold(study, task, image.id, config, args.retry_failed)
                if seconds is None:
                    state.update(status="gold_preflight_failed")
                    continue
                review_seconds = verification_budget(seconds, config["max_verification_seconds"])
                state.update(status="running", image_id=image.id, gold_seconds=round(seconds, 3),
                             verification_seconds=review_seconds)
                save(state_file, state)
                for arm in ARMS:
                    if source_hashes() != manifest["sources"]:
                        raise RuntimeError("Source changed during the frozen study; stop before further paid calls")
                    parent = study / "episodes" / task["key"] / arm
                    previous = attempts(parent)
                    retry_this = args.retry_failed or task["key"] + "/" + arm in args.retry_episode
                    if previous and (can_skip(previous[-1]) or not retry_this):
                        print("[skip existing]", task["key"], arm, flush=True)
                        continue
                    parent.mkdir(parents=True, exist_ok=True)
                    run = parent / f"attempt-{len(previous) + 1:03d}"
                    command = [sys.executable, "-u", str(HERE / "run_reference.py"), "--run",
                        "--task", str(study / task["path"]), "--gold-dir", str(study / "gold" / task["key"]),
                        "--output-dir", str(run), "--arm", "single_turn" if arm == "original" else "interactive",
                        "--persona", PERSONAS[1] if arm == "persona1" else PERSONAS[0],
                        "--agent-model", config["model"], "--interaction-seconds", str(config["interaction_seconds"]),
                        "--verification-seconds", str(review_seconds), "--request-seconds", str(config["request_seconds"])]
                    started = time.monotonic()
                    rc = execute(command, parent / (run.name + ".log"))
                    run.mkdir(exist_ok=True)
                    save(run / "attempt.json", {"exit_code": rc, "seconds": round(time.monotonic() - started, 3)})
                    report(study)
                    print("[finished]", task["key"], arm, "exit", rc, flush=True)
                state["status"] = "attempted"
            except Exception as exc:
                # Keep exception type in public status; diagnostics stay in the local log.
                state.update(status="infrastructure_error", error_type=type(exc).__name__)
                print("[task error]", type(exc).__name__, str(exc), flush=True)
            finally:
                save(state_file, state)
                report(study)
        summary = report(study, export=True)
        print(json.dumps(summary, indent=2), flush=True)
        return 0 if summary["ready_for_review"] == len(manifest["tasks"]) * 3 else 2
    except Exception as exc:
        save(study / "batch-error.json", {"error_type": type(exc).__name__, "message": str(exc)})
        print("[batch stopped]", type(exc).__name__, str(exc), flush=True)
        if manifest:
            from report_study import report
            report(study, export=True)
        return 2
    finally:
        if client:
            client.close()
        lock.unlink(missing_ok=True)
        if args.run and os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)


if __name__ == "__main__":
    raise SystemExit(main())
