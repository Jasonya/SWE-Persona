"""The private grader restores native wrapper metadata after artifact copying."""
import ast
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import AsyncMock, patch
import uuid

ROOT = Path(__file__).parents[1]
RUNTIME = ROOT / 'src/swe_persona/vendor/tb3/runtime'
TASK = 'risk-scorer-replay'
RELATIVE = 'app/rebuild_parity_report.sh'
PAYLOAD = b'#!/bin/sh\nprintf "fixture\\n"\n'
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


permissions = load('fixture_tb3_permissions', RUNTIME / 'checker_permissions.py')
terminal = ModuleType('terminal_agent')
terminal.docker = AsyncMock()
with patch.dict(sys.modules, {'terminal_agent': terminal, 'checker_permissions': permissions}):
    checker_runtime = load('fixture_tb3_checker_runtime', RUNTIME / 'checker_runtime.py')


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / RELATIVE
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(PAYLOAD)
        self.path.chmod(0o644)
        self.mapping = patch.dict(permissions.WRAPPERS, {TASK: (RELATIVE, DIGEST)})
        self.mapping.start()
        self.addCleanup(self.mapping.stop)

    def test_only_mode_is_restored_and_repeated_setup_is_idempotent(self):
        record, = permissions.restore_permissions(TASK, root=self.root)
        self.assertEqual(self.path.read_bytes(), PAYLOAD)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o555)
        self.assertEqual(record['sha256'], DIGEST)
        self.assertEqual(record['mode_before'], '0o644')
        self.assertEqual(record['mode_after'], '0o555')
        self.assertEqual(record['status'], 'restored')
        again, = permissions.restore_permissions(TASK, root=self.root)
        self.assertEqual(again['status'], 'unchanged')

    def test_modified_contents_are_left_to_existing_checks(self):
        changed = PAYLOAD + b'echo changed\n'
        self.path.write_bytes(changed)
        record, = permissions.restore_permissions(TASK, root=self.root)
        self.assertEqual(record['reason'], 'content_mismatch')
        self.assertEqual(record['status'], 'skipped')
        self.assertEqual(self.path.read_bytes(), changed)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)

    def test_symlink_target_is_not_modified(self):
        target = self.root / 'target.sh'
        target.write_bytes(PAYLOAD)
        target.chmod(0o644)
        self.path.unlink()
        self.path.symlink_to(target)
        record, = permissions.restore_permissions(TASK, root=self.root)
        self.assertEqual(record['reason'], 'not_regular')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        self.assertTrue(self.path.is_symlink())

    def test_missing_script_is_not_recreated(self):
        self.path.unlink()
        record, = permissions.restore_permissions(TASK, root=self.root)
        self.assertEqual(record['reason'], 'missing')
        self.assertFalse(self.path.exists())

    def test_unrelated_task_does_not_restore_permissions_or_call_docker(self):
        self.assertEqual(permissions.restore_permissions('other-task', root=self.root), [])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)
        with patch.object(checker_runtime, 'docker', new_callable=AsyncMock) as docker:
            self.assertEqual(asyncio.run(
                checker_runtime.restore_checker_permissions('other-task', 'container')), [])
            docker.assert_not_awaited()


class GraderIntegrationTests(unittest.TestCase):
    def check(self, expected):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = root / 'artifacts'
            wrapper = artifacts / RELATIVE
            wrapper.parent.mkdir(parents=True)
            wrapper.write_bytes(PAYLOAD)
            wrapper.chmod(0o644)
            private = root / 'container'
            installed = private / RELATIVE
            installed.parent.mkdir(parents=True)
            installed.write_bytes(PAYLOAD)
            installed.chmod(0o755)
            (root / 'inventory.json').write_text(json.dumps({'cases': [
                {'task_id': TASK, 'gpus': 0, 'cpus': 2, 'memory_mb': 2048}]}))
            checker = root / 'checker.py'
            checker.write_text('# The fixture Docker runner supplies the independent verdict.\n')
            removed = []

            async def docker(*args):
                if args[:2] == ('image', 'inspect'):
                    return 'sha256:fixture\n'
                if args[0] == 'run':
                    return 'fixture\n'
                if args[:3] == ('exec', 'fixture', 'mkdir'):
                    (private / args[-1].lstrip('/')).mkdir(parents=True, exist_ok=True)
                    return ''
                if args[0] == 'cp':
                    source, target = args[1:]
                    self.assertTrue(target.startswith('fixture:'))
                    target = private / target.split(':', 1)[1].lstrip('/')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if source.endswith('/.'):
                        shutil.copytree(source[:-2], target, dirs_exist_ok=True)
                    else:
                        shutil.copy2(source, target)
                    return ''
                if args[:4] == ('exec', 'fixture', 'python3', '-I'):
                    self.assertEqual(args[4], '-S')
                    self.assertTrue((private / args[5].lstrip('/')).is_file())
                    return json.dumps(permissions.restore_permissions(args[6], root=private))
                if args[:4] == ('exec', 'fixture', 'python3', '--version'):
                    return 'Python 3.12\n'
                if args[:4] == ('exec', 'fixture', 'python3', '-c'):
                    self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o555)
                    self.assertEqual(installed.read_bytes(), PAYLOAD)
                    return 'PERSONA_CHECK_RESULT=' + json.dumps(expected) + '\n'
                if args == ('rm', '-f', 'fixture'):
                    removed.append('fixture')
                    return ''
                raise AssertionError(args)

            # Load the actual entrypoint without importing an active task's
            # protocol/agent. Only the Docker boundary and independent verdict
            # are substituted; artifact copies preserve real filesystem modes.
            tree = ast.parse((RUNTIME / 'trial_adapter.py').read_text())
            function = next(node for node in tree.body
                            if isinstance(node, ast.AsyncFunctionDef) and node.name == 'check_artifacts')
            namespace = dict(Path=Path, asyncio=asyncio, json=json, uuid=uuid, ROOT=root,
                read=lambda path: json.loads(path.read_text()), docker=docker,
                ensure_checker_python=AsyncMock(return_value='sha256:fixture'),
                materialize_collected_patch=AsyncMock(),
                restore_checker_permissions=checker_runtime.restore_checker_permissions)
            exec(compile(ast.Module(body=[function], type_ignores=[]), 'trial_adapter.py', 'exec'), namespace)
            binding = {'original_artifacts': [], 'proposal': {'requirements': [{'id': 'R1'}, {'id': 'R2'}]}}
            with patch.dict(permissions.WRAPPERS, {TASK: (RELATIVE, DIGEST)}), \
                 patch.object(checker_runtime, 'docker', docker):
                result = asyncio.run(namespace['check_artifacts'](
                    TASK, artifacts, binding, checker, runtime_image='sha256:fixture'))
            self.assertEqual(result['checks'], expected, result)
            self.assertNotIn('error_type', result)
            receipt, = result['checker_permission_restorations']
            self.assertEqual(receipt['status'], 'restored')
            self.assertEqual(receipt['mode_before'], '0o644')
            self.assertEqual(receipt['mode_after'], '0o555')
            self.assertEqual(stat.S_IMODE(wrapper.stat().st_mode), 0o644)
            self.assertEqual(wrapper.read_bytes(), PAYLOAD)
            self.assertEqual(removed, ['fixture'])
            return result

    def test_restore_happens_after_overlay_and_unknown_grade_is_preserved(self):
        result = self.check({'R1': {'status': 'pass'}, 'R2': {'status': 'unknown'}})
        self.assertEqual(result['status'], 'unknown')

    def test_metadata_repair_does_not_override_an_independent_failure(self):
        result = self.check({'R1': {'status': 'fail'}, 'R2': {'status': 'pass'}})
        self.assertEqual(result['status'], 'fail')


if __name__ == '__main__':
    unittest.main()
