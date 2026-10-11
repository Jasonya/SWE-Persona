"""Host-read cgroup v2 accounting for registered public workloads.

This is a container-lifetime high-water mark, including setup, build, page cache,
and every descendant. It is deliberately not described as application RSS or
an isolated warm-run peak. Candidate stdout never supplies the measurement.
"""
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

VERSION = 'host-cgroup-v2-memory-peak-v1'


@dataclass(frozen=True)
class DockerHostSource:
    container_id: str
    pid: int
    image_id: str


# Run only in a pristine sibling, never through the candidate container's tools.
# Host PID/cgroup namespaces expose the daemon's Linux kernel on Docker Desktop.
_HOST_SAMPLE = r'''
import json,re,sys
from pathlib import Path
pid,cid=sys.argv[1:]
assert pid.isdigit() and int(pid)>0 and re.fullmatch('[a-f0-9]{64}',cid)
members=Path('/proc',pid,'cgroup').read_text().splitlines()
relative=next(line[3:] for line in members if line.startswith('0::'))
root=Path('/sys/fs/cgroup').resolve()
path=(root/relative.lstrip('/')).resolve()
assert path.is_relative_to(root) and (cid in path.parts or cid in path.name)
events=dict(line.split() for line in (path/'memory.events').read_text().splitlines())
print(json.dumps({'peak_bytes':int((path/'memory.peak').read_text()),
 'limit_bytes':int((path/'memory.max').read_text()),
 'events':{key:int(events[key]) for key in ('max','oom','oom_kill')}}))
'''


def _docker_host_sample(source):
    from .sandbox import docker
    info = json.loads(docker('inspect', '--format', '{{json .}}', source.container_id)['output'])
    if (info.get('Id') != source.container_id or info.get('Image') != source.image_id
            or info.get('State', {}).get('Pid') != source.pid
            or not info.get('State', {}).get('Running')):
        raise ValueError('Resource container identity changed or stopped')
    result = docker('run', '--rm', '--runtime=runc', '-e', 'NVIDIA_VISIBLE_DEVICES=void', '-e', 'CUDA_VISIBLE_DEVICES=', '--platform', 'linux/amd64', '--network', 'none',
        '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
        '--pids-limit', '32', '--memory', '128m', '--cpus', '0.25',
        '--pid', 'host', '--cgroupns', 'host', '--entrypoint', 'python3',
        source.image_id, '-I', '-c', _HOST_SAMPLE, str(source.pid), source.container_id,
        timeout=30)
    value = json.loads(result['output'])
    if (set(value) != {'peak_bytes', 'limit_bytes', 'events'}
            or type(value['peak_bytes']) is not int or value['peak_bytes'] <= 0
            or type(value['limit_bytes']) is not int or value['limit_bytes'] <= 0
            or not isinstance(value['events'], dict)
            or set(value['events']) != {'max', 'oom', 'oom_kill'}
            or any(type(v) is not int or v < 0 for v in value['events'].values())):
        raise ValueError('Incomplete Docker-host memory accounting')
    return value


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def memory_bytes(value):
    match = re.fullmatch(r'([1-9][0-9]*)([bkmg]?)', str(value).lower())
    if not match:
        raise ValueError('Unsupported registered memory limit')
    return int(match[1]) * 1024 ** {'': 0, 'b': 0, 'k': 1, 'm': 2, 'g': 3}[match[2]]


def protocol(environment, probe):
    return {
        'version': VERSION,
        'memory_limit_bytes': memory_bytes(environment['memory']),
        'timeout_per_run_seconds': probe['timeout_seconds'],
        'runs': ['cold', 'warm'],
        'scope': 'Fresh network-disabled container lifetime: setup, build, complete registered workload, all descendants and charged page cache. Warm repeats the same command in the same container; the kernel peak is cumulative, not reset.',
        'workload': 'Exact public script, extra files and command; no subsampling. Successful full script exit is required in both runs. This is the registered synthetic workload only, not a production-scale benchmark.',
        'limits': 'Memory peak must not exceed the registered Docker memory limit; any memory.max event, OOM, timeout, incomplete run or fixture change fails. CPU and PID settings remain environment allocation limits, not measured performance budgets.',
        'storage': 'No temporary-disk budget is registered; disk peak is not measured or claimed. No claim of isolated application RSS or resource scaling.',
    }


def validate_protocol(value, probe, environment=None):
    if not isinstance(value, dict) or value.get('version') != VERSION:
        raise ValueError('Unregistered resource measurement protocol')
    if type(value.get('memory_limit_bytes')) is not int or value['memory_limit_bytes'] <= 0:
        raise ValueError('Invalid resource memory limit')
    if value != protocol(environment or {'memory': value['memory_limit_bytes']}, probe):
        raise ValueError('Resource measurement conventions differ from registered protocol')


def locate(box):
    from .sandbox import docker
    # Select paths from Docker identity and kernel membership, never agent prose.
    raw = docker('inspect', '--format', '{{json .}}', box.name)['output']
    info = json.loads(raw)
    cid, pid = info['Id'], info['State']['Pid']
    if not re.fullmatch('[a-f0-9]{64}', cid) or type(pid) is not int or pid <= 0:
        raise ValueError('Missing local container identity')
    if sys.platform == 'darwin':
        image_id = info.get('Image', '')
        if not re.fullmatch('sha256:[a-f0-9]{64}', image_id):
            raise ValueError('Missing immutable resource observer image')
        return DockerHostSource(cid, pid, image_id), cid
    memberships = Path(f'/proc/{pid}/cgroup').read_text().splitlines()
    relative = next(line[3:] for line in memberships if line.startswith('0::'))
    root = Path('/sys/fs/cgroup').resolve()
    path = (root / relative.lstrip('/')).resolve()
    if not path.is_relative_to(root) or cid not in path.parts[-1] and cid not in path.parts:
        raise ValueError('Local cgroup does not identify the selected Docker container')
    return path, cid


def sample(path):
    if isinstance(path, DockerHostSource):
        return _docker_host_sample(path)
    events = dict(line.split() for line in (path / 'memory.events').read_text().splitlines())
    return {'peak_bytes': int((path / 'memory.peak').read_text()),
            'limit_bytes': int((path / 'memory.max').read_text()),
            'events': {key: int(events[key]) for key in ('max', 'oom', 'oom_kill')}}


def verdict(record, expected, expected_probe_hash, *, measure_budget=True):
    """Recompute acceptance from raw host fields, never from a passed flag."""
    resource = (record or {}).get('resource_measurement')
    if not resource or resource.get('protocol_sha256') != identity(expected) or resource.get('probe_sha256') != expected_probe_hash:
        return 'unknown', ['Resource measurement or registered workload identity is missing/mismatched.']
    if resource.get('error'):
        return 'unknown', ['Resource measurement unavailable: ' + resource['error']]
    if not isinstance(resource.get('container_id'), str) or not re.fullmatch('[a-f0-9]{64}', resource['container_id']):
        return 'unknown', ['Resource container identity missing.']
    readings = [resource.get('initial')] + [r.get('measurement') for r in resource.get('runs', [])]
    runs = resource.get('runs', [])
    if any(r.get('execution',{}).get('code')==124 for r in runs):
        return 'not_met', ['Registered resource workload timed out.']
    if [r.get('label') for r in runs] != expected['runs'] or len(readings) != 3:
        return 'unknown', ['Both complete cold and warm measurement records are required.']
    for run in runs:
        execution = run.get('execution', {})
        if type(execution.get('code')) is not int or not isinstance(execution.get('output'), str):
            return 'unknown', ['Resource workload completion evidence missing.']
        if execution['code'] != 0:
            return 'not_met', ['The complete registered workload did not finish successfully in both runs.']
    if record.get('tampered_fixture_files'):
        return 'not_met', ['Registered workload files changed during execution.']
    if not measure_budget:
        return 'met', []  # Independent semantic result: no memory-budget predicate.
    for value in readings:
        if (not isinstance(value, dict) or type(value.get('peak_bytes')) is not int or value['peak_bytes'] <= 0
                or value.get('limit_bytes') != expected['memory_limit_bytes']
                or not isinstance(value.get('events'), dict)
                or any(type(value['events'].get(k)) is not int or value['events'][k] < 0 for k in ('max', 'oom', 'oom_kill'))):
            return 'unknown', ['Resource accounting fields/limits are incomplete or inconsistent.']
    if any(b['peak_bytes'] < a['peak_bytes'] or any(b['events'][k] < a['events'][k] for k in ('max', 'oom', 'oom_kill')) for a, b in zip(readings, readings[1:])):
        return 'unknown', ['Container lifetime resource counters decreased.']
    if any(v['peak_bytes'] > expected['memory_limit_bytes'] or any(v['events'].values()) for v in readings):
        return 'not_met', ['Measured memory exceeded/reached enforcement limits or an OOM occurred.']
    return 'met', []


def public_probe(patch, directory, probe):
    from .sandbox import Box
    from .task import BASE, RESOURCES, sha
    expected = probe['resource_measurement']
    validate_protocol(expected, probe, RESOURCES)
    directory.mkdir(parents=True, exist_ok=False)
    measured = {'protocol_sha256': identity(expected), 'probe_sha256': identity(probe), 'runs': []}
    files = [{'filename': probe['filename'], 'script': probe['script']}] + probe.get('extra_files', [])
    tampered = set()
    source = ''
    with Box() as box:
        try:
            path, measured['container_id'] = locate(box)
            measured['initial'] = sample(path)
            box.setup()
            box.put('/tmp/candidate.patch', patch)
            applied = box.shell('git apply --allow-empty /tmp/candidate.patch')
            for item in files:
                box.put('/tmp/' + item['filename'], item['script'])
            for label in expected['runs']:
                execution = box.shell(probe['command'], timeout=probe['timeout_seconds']) if applied['code'] == 0 else applied
                measured['runs'].append({'label': label, 'execution': execution, 'measurement': sample(path)})
                tampered.update(item['filename'] for item in files if box.read('/tmp/' + item['filename']) != item['script'])
                if execution['code'] == 124:
                    break  # Never run concurrently with a timed-out process tree.
            source = box.shell(probe.get('source_context_command', 'git diff --stat'))['output']
        except (OSError, ValueError, KeyError, StopIteration, RuntimeError) as exc:
            measured['error'] = str(exc)
    record = {'record_id': directory.name, 'base_commit': BASE, 'patch_sha256': sha(patch),
              'probe_id': probe['id'], 'case_sha256': sha(probe['script']), 'script': probe['script'],
              'extra_files': probe.get('extra_files', []), 'source_context': source,
              'resource_measurement': measured, 'tampered_fixture_files': sorted(tampered)}
    status, errors = verdict(record, expected, identity(probe))
    functional_status,_=verdict(record, expected, identity(probe),measure_budget=False)
    output = '\n'.join(r['label'] + ':\n' + r['execution']['output'] for r in measured['runs'])
    record.update(passed=functional_status == 'met', resource_status=status,functional_status=functional_status,
                  execution={'code': 0 if functional_status == 'met' else 1,
                             'output': output + '\nHOST RESOURCE MEASUREMENT:\n' + json.dumps(measured) + '\n' + '\n'.join(errors)})
    (directory / 'record.json').write_text(json.dumps(record, indent=2))
    return record
