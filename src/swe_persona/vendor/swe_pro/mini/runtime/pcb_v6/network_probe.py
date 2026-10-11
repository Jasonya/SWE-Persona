"""Diagnose transient Docker probe failures without relaxing network policy."""
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path


def probe_kernel(environment_type, trial_dir, timeout=120, attempts=2):
    trial_dir = Path(trial_dir)
    trial_dir.mkdir(parents=True, exist_ok=True)
    command = environment_type._engine_cmd(
        'container', 'run', '--rm', environment_type._EGRESS_CONTROL_KERNEL_PROBE_IMAGE,
        'sh', '-c', environment_type._EGRESS_CONTROL_KERNEL_PROBE_SCRIPT)
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    identity = hashlib.sha256(json.dumps([
        command, boot, os.environ.get('DOCKER_HOST'), os.environ.get('DOCKER_CONTEXT'),
    ]).encode()).hexdigest()
    cache = trial_dir / 'network_probe_success.json'
    log = trial_dir / 'network_probe.jsonl'

    def record(value):
        with log.open('a') as stream:
            stream.write(json.dumps(dict(timestamp=time.time(), **value)) + '\n')

    try:
        saved = json.loads(cache.read_text())
        if saved['identity'] == identity and 0 <= time.time() - saved['timestamp'] < 3600:
            record({'status': 'cached_success'})
            return True
    except (OSError, ValueError, KeyError):
        pass

    last = None
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
            last = {'attempt': attempt, 'returncode': result.returncode,
                    'stdout': result.stdout[-4000:], 'stderr': result.stderr[-4000:]}
        except (subprocess.TimeoutExpired, OSError) as exc:
            last = {'attempt': attempt, 'error': type(exc).__name__, 'message': str(exc)}
        last['elapsed_sec'] = round(time.monotonic() - started, 3)
        record(last)
        if last.get('returncode') == 0:
            temporary = cache.with_suffix('.tmp')
            temporary.write_text(json.dumps({'identity': identity, 'timestamp': time.time()}))
            temporary.replace(cache)
            return True
    raise RuntimeError(
        'Docker network kernel probe failed; allowlist remains required. '
        f'Inspect {log}. Last result: {last}')
