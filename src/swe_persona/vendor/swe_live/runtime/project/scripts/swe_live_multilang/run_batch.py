"""One deterministic task per language, gold preflight, then three matched arms.

Only --run invokes Azure. Existing attempt directories are never overwritten.
Re-running skips valid results; failed/interrupted attempts need --retry-failed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from task_adapter import ROOT, read_task
from run_baseline import save

REVISION = "22091f6ce331c5c60c241d76512d4be7ee1a555b"
LANGUAGES = ("c", "cpp", "go", "js", "rust", "java", "ts", "cs")
ARMS = ("single_turn", "interactive_no_evolution", "persona")
SCRIPT = Path(__file__).resolve().parent


def select_task(rows, language):
    # No filtering by model performance or hidden patch/test contents.
    if language == "c":
        return next(row for row in rows if row["instance_id"] == "rsyslog__rsyslog-6047")
    return min(rows, key=lambda row: hashlib.sha256(
        ("multilang-pilot-v1:" + row["instance_id"]).encode()).hexdigest())


def prepare(batch):
    manifest = batch / "manifest.json"
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        for item in data["tasks"]:
            if hashlib.sha256((batch/item["path"]).read_bytes()).hexdigest() != item["sha256"]:
                raise ValueError("Frozen task changed: " + item["language"])
        return data
    from datasets import load_dataset
    batch.mkdir(parents=True, exist_ok=True)
    tasks = []
    for language in LANGUAGES:
        print("[prepare]", language, flush=True)
        rows = load_dataset("SWE-bench-Live/MultiLang", split=language, revision=REVISION,
                            cache_dir=str(ROOT / ".local/hf-cache"))
        row = select_task(rows, language)
        path = batch / "tasks" / language / "task.private.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
        path.write_bytes(payload)
        read_task(path)
        tasks.append({"language":language,"instance_id":row["instance_id"],
                      "path":path.relative_to(batch).as_posix(),"sha256":hashlib.sha256(payload).hexdigest()})
    data = {"dataset_revision":REVISION,"tasks":tasks,"arms":list(ARMS),
            "protocol":"full_public_issue; 24 agent calls; 24 shell steps maximum; 2400s admission budget",
            "selection":"C reuses smoke task; other languages minimum seeded SHA256(instance_id); no replacement on failure"}
    save(manifest, data)
    return data


def latest_attempt(parent):
    attempts = sorted(p for p in parent.glob("attempt-*") if p.is_dir()) if parent.exists() else []
    return attempts[-1] if attempts else None


def result(path):
    file = path / "summary.json" if path else None
    return json.loads(file.read_text(encoding="utf-8")) if file and file.exists() else {}


def execute(command, log):
    print("[run] Log:", log, flush=True)
    with log.open("w", encoding="utf-8") as out:
        return subprocess.run(command, stdout=out, stderr=subprocess.STDOUT).returncode


def aggregate(batch, manifest):
    rows = []
    for task in manifest["tasks"]:
        for arm in ARMS:
            path = latest_attempt(batch / "episodes" / task["language"] / arm)
            summary = result(path)
            rows.append({"language":task["language"],"instance_id":task["instance_id"],"arm":arm,
                         "path":str(path) if path else None,
                         **{key:summary.get(key) for key in ("status","evaluation_valid","base_task_success",
                             "evolved_requirement_status","joint_success","usage")}})
    save(batch / "results.json", rows)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Prepare, pull images, gold-check and run up to 24 paid episodes")
    parser.add_argument("--retry-failed", action="store_true", help="Explicitly create fresh attempts for failed/interrupted episodes")
    parser.add_argument("--batch-dir", type=Path, default=ROOT / ".local/multilang-batch/pilot-v1")
    parser.add_argument("--languages", nargs="+", choices=LANGUAGES, default=list(LANGUAGES))
    parser.add_argument("--arms", nargs="+", choices=ARMS, default=list(ARMS))
    args = parser.parse_args()
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    docker_bin = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Docker/Docker/resources/bin"
    os.environ["PATH"] = str(docker_bin) + os.pathsep + os.environ["PATH"]
    batch = args.batch_dir.resolve()
    # Prevent simultaneous launches from producing duplicate paid episodes.
    batch.mkdir(parents=True, exist_ok=True)
    lock = batch / "batch.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise SystemExit(f"Batch already locked. If the previous process has stopped, remove {lock} before resuming.")
    os.close(fd)
    client = None
    try:
        manifest = prepare(batch)
        selected_tasks = [t for t in manifest["tasks"] if t["language"] in args.languages]
        selected_arms = [a for a in ARMS if a in args.arms]
        planned = len(selected_tasks) * len(selected_arms)
        aggregate(batch, manifest)
        print(f"Selected {len(selected_tasks)} tasks x {len(selected_arms)} arms = {planned} episodes. Manifest:", batch / "manifest.json", flush=True)
        if not args.run:
            print("No Azure calls made. Add --run to execute.")
            return 0
        from llm import load_local_env
        load_local_env()
        if not os.environ.get("AZURE_OPENAI_API_KEY", "").strip():
            raise SystemExit("Configure AZURE_OPENAI_API_KEY in .env first")
        import docker
        client = docker.from_env(timeout=180)
        if client.info()["OSType"] != "linux":
            raise SystemExit("Docker must use Linux containers")
        for item in selected_tasks:
            language = item["language"]
            task_path = batch / item["path"]
            task = read_task(task_path)
            print(f"[task] {language}: {task['instance_id']}", flush=True)
            try:
                try:
                    image = client.images.get(task["docker_image"])
                except docker.errors.ImageNotFound:
                    print("[image] Downloading", task["docker_image"], flush=True)
                    image = client.images.pull(task["docker_image"])
                gold = batch / "gold" / language
                gold.mkdir(parents=True, exist_ok=True)
                stamp = gold / "validated.json"
                expected = {"image_id":image.id,"task_sha256":item["sha256"]}
                valid_gold = stamp.exists() and json.loads(stamp.read_text()) == expected
                if not valid_gold:
                    attempt = gold / ("attempt-%03d" % (len(list(gold.glob("attempt-*"))) + 1))
                    attempt.mkdir()
                    command = [sys.executable,"-u",str(SCRIPT / "run_evaluation.py"),
                        "--dataset",str(task_path),"--patch_dir","gold","--platform","linux",
                        "--workers","1","--overwrite","0","--output_dir",str(attempt / "evaluation")]
                    rc = execute(command, attempt / "evaluation.log")
                    report_path = attempt / "evaluation/results.json"
                    report = json.loads(report_path.read_text()) if report_path.exists() else {}
                    if rc or report.get("submitted") != 1 or report.get("success") != 1 or report.get("error") != 0 or report.get("incomplete") != 0:
                        print("[blocked] Gold environment check failed; no paid episodes for", language, flush=True)
                        continue
                    save(stamp, expected)
                for arm in selected_arms:
                    parent = batch / "episodes" / language / arm
                    previous = latest_attempt(parent)
                    if previous and result(previous).get("evaluation_valid"):
                        print("[skip] Valid result already exists:", language, arm, flush=True)
                        continue
                    if previous and not args.retry_failed:
                        print("[blocked] Previous attempt incomplete; use --retry-failed:", language, arm, flush=True)
                        continue
                    parent.mkdir(parents=True, exist_ok=True)
                    run = parent / ("attempt-%03d" % (sum(p.is_dir() for p in parent.glob("attempt-*")) + 1))
                    log = parent / (run.name + ".log")
                    # Reserve an attempt even if the child fails before creating its output.
                    command = [sys.executable,"-u",str(SCRIPT / "run_persona.py"),"--task",str(task_path),
                        "--arm",arm,"--full-issue","--output-dir",str(run)]
                    rc = execute(command, log)
                    if not run.exists():
                        run.mkdir()
                        save(run / "summary.json", {"status":"startup_failed","evaluation_valid":False,"exit_code":rc})
                    aggregate(batch, manifest)
                    print("[finished]", language, arm, "exit", rc, flush=True)
            except Exception as exc:
                print("[blocked]", language, type(exc).__name__, str(exc), flush=True)
        rows = aggregate(batch, manifest)
        count = sum(row["evaluation_valid"] is True for row in rows
                    if row["language"] in args.languages and row["arm"] in selected_arms)
        print(f"Valid evaluations: {count}/{planned}. Results: {batch / 'results.json'}")
        return 0 if count == planned else 2
    finally:
        if client:
            client.close()
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
