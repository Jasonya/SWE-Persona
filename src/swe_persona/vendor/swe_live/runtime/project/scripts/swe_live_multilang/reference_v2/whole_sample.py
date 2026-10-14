"""Supervise exactly one owned sample; retain deadline failures as scored timeouts."""
import os
import math
from pathlib import Path
import subprocess
import time
import uuid

from support import read, save
from evaluation_runtime import stop_process

LABEL = "personacode.sample"


def cleanup_sample(owner):
    import docker
    client = docker.from_env(timeout=20)
    try:
        removed = []
        containers = client.containers.list(all=True, filters={"label": LABEL + "=" + owner})
        # Network-sharing sidecars must leave before their task container.
        for container in sorted(containers, key=lambda c: not bool(c.labels.get("personacode.fixture"))):
            if container.labels.get(LABEL) != owner:
                raise RuntimeError("Sample ownership mismatch")
            container.remove(force=True)
            removed.append(container.id)
        return removed
    finally:
        client.close()


def supervise_sample(command, directory, seconds, cleanup=cleanup_sample):
    """Clock starts before process launch and never resets between phases.

    The wrapper owns the run directory; the worker creates its artifacts in
    `worker/`. Owner-scoped Docker cleanup is outside scored runtime and logged.
    An interrupted attempt remains evidence; resuming never overwrites it.
    """
    directory = Path(directory)
    from swe_persona.time_limit import managed_clock, PHASE_ENV, POLICY
    managed=managed_clock()
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("A finite positive sample cap is required")
    directory.mkdir(parents=True, exist_ok=False)
    owner = uuid.uuid4().hex
    env = dict(os.environ, PERSONACODE_SAMPLE_OWNER=owner)
    started = time.monotonic()
    deadline = started + seconds
    save(directory / "started.json", {"owner": owner, "cap_seconds": seconds,
         "started_unix": time.time(), "deadline_monotonic": None if managed else deadline,
         "timing_policy": POLICY if managed else 'legacy-whole-sample'})
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
               if os.name == "nt" else {"start_new_session": True})
    process, code, timed_out, interrupted, error = None, None, False, False, None
    try:
        with (directory / "worker.log").open("w", encoding="utf-8") as log:
            arguments=["--sample-seconds",str(seconds),"--output-dir",str(directory / "worker")]
            if not managed:arguments += ["--deadline-monotonic",str(deadline)]
            process = subprocess.Popen(command + arguments,
                env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **options)
            # In the unified runner, the host watchdog owns separate preparation,
            # interaction and grading clocks. Do not start a second full-sample
            # timer before the episode container has finished preparation.
            code = process.wait(timeout=None if managed else max(.001, deadline - time.monotonic()))
            timed_out = code == 124 or (not managed and time.monotonic() > deadline)
    except subprocess.TimeoutExpired:
        timed_out = True
    except (KeyboardInterrupt, SystemExit):
        interrupted = True
        raise
    except Exception as exc:
        error = type(exc).__name__
    finally:
        # Capture the sampled duration before cleanup. No late result is admitted.
        elapsed = time.monotonic() - started
        cleanup_started = time.monotonic()
        if process is not None and process.poll() is None:
            try:
                stop_process(process)
            except Exception as exc:
                error = "process_cleanup:" + type(exc).__name__
        removed, cleanup_error = [], None
        try:
            removed = cleanup(owner)
        except Exception as exc:
            cleanup_error = type(exc).__name__
        worker = directory / "worker"
        release = read(worker / "release_check.json") if (worker / "release_check.json").exists() else {}
        episode = read(worker / "episode.json") if (worker / "episode.json").exists() else {}
        metrics = episode.get("metrics", {})
        status = ("interrupted" if interrupted else "timeout" if timed_out else
                  "completed" if code == 0 and release.get("ready_for_review") and not error
                  else "evaluation_invalid")
        if status == 'completed' and release.get('result_status') == 'not_applicable':
            status = 'not_applicable'
        # Even if work completed, failed cleanup makes the next job unsafe.
        receipt = {"status": status, "owner": owner, "exit_code": code,
            "seconds": elapsed, "cap_seconds": seconds, "cleanup_seconds": time.monotonic() - cleanup_started,
            "cleanup_error": cleanup_error, "removed_containers": removed, "error_type": error,
            "completion_within_budget": status == "completed",
            "base_task_success": metrics.get("base_task_success") if not timed_out else None,
            "joint_success": 0 if timed_out else metrics.get("joint_success") if status == "completed" else None,
            "persona_status": episode.get("outcome", {}).get("evolved_requirements", []),
            "worker_release": release,
            "publication_status": "awaiting_human_review" if status in ("completed", "timeout") else "blocked"}
        if managed:
            receipt['timing_policy']=POLICY
            receipt['phase_events']=read(Path(os.environ[PHASE_ENV]))['events']
            phases=receipt['phase_events']
            receipt['interaction_timed_out']=bool(len(phases)>1 and
                phases[1]['started_monotonic']>phases[0]['deadline_monotonic'])
            receipt['completion_within_budget']=status=='completed' and not receipt['interaction_timed_out']
            receipt['seconds_scope']='sample worker including preparation and grading; cap_seconds limits interaction only'
        save(directory / "result.json", receipt)
    if (error and error.startswith("process_cleanup:")) or cleanup_error:
        raise RuntimeError("Owned sample cleanup failed; stop the batch before starting another job")
    return receipt


def existing_result(directory):
    directory = Path(directory)
    if (directory / "result.json").exists():
        result = read(directory / "result.json")
        if result.get("cleanup_error") or str(result.get("error_type", "")).startswith("process_cleanup:"):
            raise RuntimeError("Previous sample cleanup failed; inspect " + str(directory))
        return result
    if directory.exists():
        # A power interruption cannot silently purchase another attempt or turn
        # a missing result into a fast sample. Remove only its labelled containers.
        start = read(directory / "started.json")
        cleanup_sample(start["owner"])
        result = {"status": "interrupted", "seconds": None, "joint_success": None,
                  "base_task_success": None, "reason": "Incomplete attempt retained; no automatic retry"}
        save(directory / "result.json", result)
        return result
    return None
