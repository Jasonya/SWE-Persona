"""One wall-clock watchdog for every benchmark profile, with bounded cleanup."""
import math
import os
import signal
import subprocess
import time

POLICY = 'worker-wall-clock-v1'
SCOPE = 'worker startup, image/environment preparation, agent, simulator, judges, and native/persona verification; excludes prior judge calibration and input materialization'


def positive_seconds(value):
    value=float(value)
    if not math.isfinite(value) or value<=0:
        raise ValueError('Time limit must be finite and greater than zero')
    return value


def resolve_limit(row, override=None):
    default=positive_seconds(row['budget']['episode_timeout_sec'])
    return {'policy':POLICY, 'scope':SCOPE, 'default_sec':default,
            'effective_sec':default if override is None else positive_seconds(override),
            'overridden':override is not None, 'cleanup_grace_sec':30,
            'internal_budget':dict(row['budget']),
            'internal_limits_preserved':True}


def run_bounded(command, *, env, log, seconds, cleanup_grace_sec=30):
    """Interrupt the complete worker process group; never interrupt sibling runs."""
    seconds=positive_seconds(seconds)
    started=time.monotonic(); signals=[];timed_out=False
    process=subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True)
    def send(sig):
        try:os.killpg(process.pid,sig);signals.append(sig.name)
        except ProcessLookupError:pass
    try:
        process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        timed_out=True;send(signal.SIGINT)
        try:process.wait(timeout=cleanup_grace_sec)
        except subprocess.TimeoutExpired:
            send(signal.SIGTERM)
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                send(signal.SIGKILL);process.wait(timeout=5)
        # A subprocess may outlive a worker that has already exited. Stop only
        # members of this run's dedicated group, never other benchmark runs.
        send(signal.SIGTERM)
    except BaseException:
        send(signal.SIGINT)
        try:process.wait(timeout=cleanup_grace_sec)
        except subprocess.TimeoutExpired:
            send(signal.SIGKILL);process.wait(timeout=5)
        raise
    elapsed=time.monotonic()-started
    return {'policy':POLICY,'scope':SCOPE,'timeout_sec':seconds,'timed_out':timed_out,
            'elapsed_sec':elapsed,'cleanup_elapsed_sec':max(0,elapsed-seconds) if timed_out else 0,
            'cleanup_grace_sec':cleanup_grace_sec,'signals':signals,
            'process_exit_code':process.returncode,
            'note':'Cleanup grace is outside the scoring budget. Unfinished verification is not synthesized.'}
