"""Timing, seconds-based deadlines, and cleanup for one official evaluator job."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

OWNER_LABEL = "personacode.evaluation_job"


class Timeline:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def add(self, **event):
        event = {"wall_time": time.time(), **event}
        with self.lock, self.path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(event, ensure_ascii=False) + "\n")


def install_runtime_adapters(runtime_type, command_seconds, timeline):
    """RepoLaunch takes MINUTES, whereas the evaluator's constant is SECONDS."""
    original_init = runtime_type.__init__
    original_send = runtime_type.send_command
    runtimes = []

    def initialize(self, container, command_timeout=30):
        self.container, self.stopped = container, False
        runtimes.append(self)
        # Do not forward 9000 seconds as 9000 minutes (150 hours).
        return original_init(self, container, command_timeout=command_seconds / 60.0)

    def send(self, command, timeout=None):
        seconds = command_seconds if timeout is None else timeout * 60
        started = time.monotonic()
        timeline.add(event="command_started", command=command[:180], timeout_seconds=seconds,
                     container_id=self.container.id)
        print(f"[official] command started; timeout={seconds:g}s: {command[:100]}", flush=True)
        try:
            result = original_send(self, command, timeout)
            code = int(result.metadata.exit_code)
            timeline.add(event="command_finished", seconds=round(time.monotonic() - started, 3),
                         command=command[:180], exit_code=code, output_chars=len(result.output),
                         container_id=self.container.id)
            print(f"[official] command finished in {time.monotonic() - started:.1f}s; exit={code}", flush=True)
            if code == 124:  # launch.core.platforms.base.TIMEOUT_EXIT_CODE
                raise TimeoutError("Official command exceeded its time budget")
            return result
        except Exception as exc:
            timeline.add(event="command_error", seconds=round(time.monotonic() - started, 3),
                         error_type=type(exc).__name__, container_id=self.container.id)
            raise

    def cleanup(self, prune_dangling=False):
        # This job only owns this container. Global image pruning is unrelated
        # work and may remove another job's cached images.
        if getattr(self, "stopped", False):
            return
        started = time.monotonic()
        try:
            self.container.remove(force=True)
        except Exception as exc:
            if getattr(exc, "status_code", None) != 404:
                raise
        finally:
            self.stopped = True
            sock = getattr(self, "sock", None)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            timeline.add(event="container_cleanup", seconds=round(time.monotonic() - started, 3))

    runtime_type.__init__, runtime_type.send_command, runtime_type.cleanup = initialize, send, cleanup
    return runtimes


def label_created_containers(owner):
    from docker.models.containers import ContainerCollection
    original = ContainerCollection.run

    def run(self, *args, **kwargs):
        labels = dict(kwargs.pop("labels", None) or {})
        labels[OWNER_LABEL] = owner
        if os.environ.get("PERSONACODE_SAMPLE_OWNER"):
            labels["personacode.sample"] = os.environ["PERSONACODE_SAMPLE_OWNER"]
        return original(self, *args, labels=labels, **kwargs)

    ContainerCollection.run = run


def cleanup_owned(owner):
    import docker
    client = docker.from_env(timeout=30)
    removed = []
    try:
        for container in client.containers.list(all=True, filters={"label": OWNER_LABEL + "=" + owner}):
            # Inspect before removal; never rely on a guessed name or pid.
            if container.labels.get(OWNER_LABEL) == owner:
                container.remove(force=True)
                removed.append(container.id)
        return removed
    finally:
        client.close()


def stop_process(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       capture_output=True, timeout=30)
    else:
        if os.getpgid(process.pid) == process.pid:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()  # Nested worker shares its outer sample's process group.
    process.wait(timeout=30)


def supervise(command, output_dir, seconds=0):
    """A wall deadline covers rebuild, all tests, log parsing and cleanup together.

    Zero disables the wall deadline. Timeout is UNKNOWN, never a model failure.
    """
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    timeline = Timeline(output / "runtime-events.jsonl")
    owner = uuid.uuid4().hex
    env = dict(os.environ, PERSONACODE_EVAL_OWNER=owner)
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": not bool(os.environ.get("PERSONACODE_SAMPLE_OWNER"))}
    started = time.monotonic()
    timeline.add(event="evaluation_started", owner=owner, wall_budget_seconds=seconds or None)
    # Explicit standard handles are required with CREATE_NO_WINDOW on Windows;
    # otherwise the worker can run successfully while its evaluator log is empty.
    process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                               stdout=sys.stdout, stderr=sys.stderr, **options)
    timeout, removed, cleanup_error, code = False, [], None, None
    try:
        code = process.wait(timeout=seconds or None)
    except subprocess.TimeoutExpired:
        timeout = True
        stop_process(process)
        code = 124
    except BaseException:
        stop_process(process)
        raise
    finally:
        try:
            removed = cleanup_owned(owner)
        except Exception as exc:
            cleanup_error = type(exc).__name__
        duration = time.monotonic() - started
        timeline.add(event="evaluation_finished", seconds=round(duration, 3), timed_out=timeout,
                     removed_containers=len(removed), cleanup_error=cleanup_error)
        (output / "runtime-summary.json").write_text(json.dumps({
            "status": "wall_time_exceeded" if timeout else "process_exited" if code == 0 else "process_failed",
            "exit_code": code,
            "seconds": round(duration, 3), "wall_budget_seconds": seconds or None,
            "cleanup_error": cleanup_error, "removed_containers": removed,
            "evaluation_valid": False if timeout or cleanup_error or code != 0 else None,
        }, indent=2) + "\n", encoding="utf-8")
    if cleanup_error:
        print("[official] cleanup failed:", cleanup_error, flush=True)
        return 2
    if timeout:
        print(f"[official] wall deadline exceeded ({seconds:g}s); result remains unknown", flush=True)
    return code
