"""Original Pier task/verifier path plus the existing independent integrity audit.

Never imports this module on the default legacy path. No replacement grader.
"""
import json
import math
import shlex
import shutil
import time
from pathlib import Path

from pier.utils.scripts import build_execution_command
from . import integrity
from .pier_runtime import PrivateDockerEnvironment, task_bundle


def native_task_bundle(root, source, image, resources, *, agent_timeout_sec=None, verifier_timeout_sec=None):
    import toml
    source, root = Path(source), Path(root)
    original = toml.loads((source/'task.toml').read_text())
    if original['verifier'].get('environment_mode') != 'separate':
        raise ValueError('Native adapter requires the original separate verifier configuration')
    if original.get('artifacts') != ['/logs/artifacts/model.patch']:
        raise ValueError('Unsupported original artifact contract; review before adapting')
    if not original['verifier'].get('collect') or original.get('steps'):
        raise ValueError('Native adapter requires original single-task collect hooks')
    timeout = original.get('agent', {}).get('timeout_sec')
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError('Original task must declare a positive finite agent.timeout_sec')
    source_timeout = timeout
    from .delivery_timeouts import DEFAULT_VERIFIER_SECONDS, positive_seconds
    source_verifier_timeout = original['verifier']['timeout_sec']
    verifier_timeout = (min(source_verifier_timeout, DEFAULT_VERIFIER_SECONDS)
                        if verifier_timeout_sec is None else positive_seconds(verifier_timeout_sec))
    if agent_timeout_sec is not None:
        from .delivery_timeouts import positive_seconds
        timeout = positive_seconds(agent_timeout_sec)
    # Keep the proven private, network-isolated agent environment profile.
    task_bundle(root, image, resources, timeout)
    adapter = toml.loads((root/'task.toml').read_text())
    original['environment'] = adapter['environment']
    original['agent']['timeout_sec'] = timeout
    original['verifier']['timeout_sec'] = verifier_timeout
    # Preserve agent fields except an explicitly declared time override. Pier applies
    # the timeout to each coding segment; the coordinator has a separate budget.
    (root/'task.toml').write_text(toml.dumps(original))
    shutil.rmtree(root/'tests')
    shutil.copytree(source/'tests', root/'tests')
    if integrity.tree_manifest(source/'tests') != integrity.tree_manifest(root/'tests'):
        raise ValueError('Original verifier copy differs')
    (root/'adapter-overrides.json').write_text(json.dumps({
        'instruction': 'placeholder; actual task/persona delivered by simulator only',
        'environment': adapter['environment'], 'agent_timeout_sec': timeout,
        'original_agent_timeout_sec': source_timeout,
        'agent_timeout_policy': 'explicit_frozen_plan' if agent_timeout_sec is not None else 'preserve_original_task',
        'agent_timeout_overridden': agent_timeout_sec is not None,
        'agent_timeout_scope': 'Pier coding segment; coordinator interaction budget is separate',
        'original_verifier_timeout_sec': source_verifier_timeout,
        'verifier_timeout_sec': verifier_timeout,
        'verifier_max_attempts': 1,
        'verifier_timeout_policy': 'explicit_frozen_plan' if verifier_timeout_sec is not None else 'default_900_second_cap',
        'verifier_timeout_scope': 'entire verification attempt; cancellation and cleanup may take additional time',
        'preserved': (['agent'] if agent_timeout_sec is None else ['agent fields except timeout_sec']) + ['verifier fields except timeout_sec', 'verifier.collect', 'artifacts', 'tests/**'],
        'source_task_sha256': integrity.digest((source/'task.toml').read_bytes()),
        'tests_manifest': integrity.tree_manifest(source/'tests'),
    }, indent=2))
    return root


class AuditedDockerEnvironment(PrivateDockerEnvironment):
    """Observe original verifier command exit and baked input hashes on the host.

    Pier's Verifier returns rewards but drops exec's exit status. Keep that
    evidence outside the verifier's mounted output directory, without changing
    the command, tests or score.
    """
    async def exec(self, command, **kwargs):
        expected = build_execution_command('/tests/test.sh',
            stdout_path='/logs/verifier/test-stdout.txt', task_os=self.task_os)
        is_verifier = self.environment_dir.name == 'tests' and command == expected
        if not is_verifier:
            return await super().exec(command=command, **kwargs)
        names = [p.name for p in self.environment_dir.iterdir()
                 if p.is_file() and p.name != 'Dockerfile']
        # Only digests leave the verifier; no hidden test text enters prompts.
        script = ('import hashlib,json,pathlib,sys; r=pathlib.Path("/tests"); '
                  'names=json.loads(sys.argv[1]); '
                  'assert all((r/n).is_file() and not (r/n).is_symlink() for n in names); '
                  'print(json.dumps({n:hashlib.sha256((r/n).read_bytes()).hexdigest() for n in names}))')
        async def manifest():
            value = await super(AuditedDockerEnvironment, self).exec(
                command=shlex.join(['python3','-c',script,json.dumps(names)]), timeout_sec=30)
            if value.return_code != 0:
                raise ValueError('Cannot attest original verifier inputs')
            return json.loads(value.stdout)
        record = {'before': await manifest(), 'code': None, 'after': None}
        wanted = {n:integrity.digest((self.environment_dir/n).read_bytes()) for n in names}
        if record['before'] != wanted:
            raise ValueError('Baked verifier inputs differ from pinned task')
        started = time.monotonic()
        try:
            result = await super().exec(command=command, **kwargs)
            record['code'] = result.return_code
            return result
        finally:
            record['seconds'] = time.monotonic()-started
            record['after'] = await manifest()
            path = self.trial_paths.trial_dir/'native-verifier-executions.jsonl'
            with path.open('a') as f:
                f.write(json.dumps(record)+'\n')


def regular_bytes(path, limit=32*1024*1024):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ValueError('Missing or unsafe artifact: '+path.name)
    if path.stat().st_size > limit:
        raise ValueError('Artifact exceeds bounded importer limit: '+path.name)
    return path.read_bytes()


def import_grade(trial, patch, out):
    """Package native output in the unchanged canonical benchmark record schema."""
    from .task import SOURCE_ROOTS
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    verifier = out/'verifier'; verifier.mkdir(exist_ok=True)
    # Retain bounded regular reports only; the full native directory also remains.
    for path in trial._trial_paths.verifier_dir.iterdir():
        if path.is_file() and not path.is_symlink():
            (verifier/path.name).write_bytes(regular_bytes(path))
    def report(name):
        try: return json.loads(regular_bytes(verifier/name))
        except (ValueError, OSError, UnicodeError): return None
    reward = report('reward.json')
    if reward != trial.result.verifier_result.rewards:
        raise ValueError('Native Pier rewards differ from original reward.json')
    records = [json.loads(line) for line in regular_bytes(
        trial.trial_dir/'native-verifier-executions.jsonl').splitlines()]
    if not records:
        raise ValueError('Original verifier execution evidence missing')
    last = records[-1]
    config = json.loads((trial._task.paths.tests_dir/'config.json').read_text())
    audited = integrity.audit(patch, SOURCE_ROOTS, config, reward, report('ctrf.json'),
                              last['code'], last['before'], last['after'])
    output = regular_bytes(verifier/'test-stdout.txt').decode(errors='replace')
    record = {'record_id': out.name, 'patch_sha256': integrity.digest(patch.encode()),
              'code': last['code'], 'status': 'passed' if (reward or {}).get('reward')==1 else 'failed',
              'reward': reward, 'seconds': sum(r['seconds'] for r in records),
              'output_tail': output[-12000:], 'integrity': audited,
              'runtime': 'pier-native-v2', 'verifier_attempts': len(records)}
    (out/'output.txt').write_text(output)
    (out/'integrity.json').write_text(json.dumps(audited, indent=2))
    (out/'result.json').write_text(json.dumps(record, indent=2))
    return record
