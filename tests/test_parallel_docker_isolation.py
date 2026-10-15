"""Batches that run the same task concurrently on one Docker daemon must not share or remove each other's
resources, and containers must see only the CPUs their quota allows."""
import ast
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).parents[1]
VENDOR = ROOT / 'src/swe_persona/vendor'
RUNNER = VENDOR / 'swe_pro/shell/portable/runtime-v3/runner'
TB3 = VENDOR / 'tb3/runtime'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module while the class is built
    spec.loader.exec_module(module)
    return module


dockerx = load('fixture_parallel_dockerx', RUNNER / 'dockerx.py')


def shell_docker_names(run_id, run_dir):
    """Evaluate run_episode.main's own naming statements without importing the episode runtime."""
    main = next(node for node in ast.parse((RUNNER / 'run_episode.py').read_text()).body
                if isinstance(node, ast.FunctionDef) and node.name == 'main')
    names = {'run_slug', 'run_hash'}
    statements = [node for node in ast.walk(main) if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id in names for target in node.targets)]
    keywords = {node.arg: node.value for node in ast.walk(main) if isinstance(node, ast.keyword)
                and node.arg in ('name', 'snapshot_tag_prefix') and isinstance(node.value, ast.JoinedStr)}
    namespace = dict(re=re, hashlib=hashlib, run_id=run_id, run_dir=run_dir)
    exec(compile(ast.Module(body=statements, type_ignores=[]), 'run_episode.py', 'exec'), namespace)
    evaluate = lambda node: eval(compile(ast.Expression(node), 'run_episode.py', 'eval'), namespace)
    return evaluate(keywords['name']), evaluate(keywords['snapshot_tag_prefix'])


class ShellRunNameTests(unittest.TestCase):
    RUN_ID = 'instance_example__repo-0123456789abcdef-student_needing_explanation-20261002-a1'

    def test_batches_get_distinct_container_and_snapshot_names(self):
        first = shell_docker_names(self.RUN_ID, Path('/runs/batch-a/0034') / self.RUN_ID)
        second = shell_docker_names(self.RUN_ID, Path('/runs/batch-b/0034') / self.RUN_ID)
        self.assertNotEqual(first[0], second[0])
        self.assertNotEqual(first[1], second[1])

    def test_names_are_deterministic_and_keep_their_format(self):
        run_dir = Path('/runs/batch-a/0034') / self.RUN_ID
        container, snapshot = shell_docker_names(self.RUN_ID, run_dir)
        self.assertEqual((container, snapshot), shell_docker_names(self.RUN_ID, run_dir))
        self.assertRegex(container, r'^swepro-instance_example__repo-0123456789ab-[0-9a-f]{10}$')
        self.assertTrue(snapshot.startswith('swepro-snap-') and container[-10:] == snapshot[-10:])


def compose_down_untags(tags, project):
    """Compose's down --rmi local --remove-orphans removes the first sorted tag of each project-labelled image
    and the service image name <project>-main; the image is deleted once no tag is left."""
    tags = sorted(tags)
    return set(tags) - {tags[0], project + '-main:latest'}


class TB3RuntimeImageTagTests(unittest.TestCase):
    IMAGE = 'sha256:' + 'ab' * 32

    def tags_from_setup(self):
        """Run PersonaTerminalAgent.setup with only the Docker boundary substituted."""
        agent = next(node for node in ast.parse((TB3 / 'terminal_agent.py').read_text()).body
                     if isinstance(node, ast.ClassDef) and node.name == 'PersonaTerminalAgent')
        setup = next(node for node in agent.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'setup')
        details = [{'Id': 'main-id', 'Image': self.IMAGE, 'Config': {'Labels': {'com.docker.compose.service': 'main'}},
                    'HostConfig': {'DeviceRequests': []}, 'State': {'Running': True}}]

        async def docker(*args):
            return json.dumps(details) if args[0] == 'inspect' else ''

        recorder = AsyncMock(side_effect=docker)
        namespace = dict(json=json, docker=recorder)
        exec(compile(ast.Module(body=[setup], type_ignores=[]), 'terminal_agent.py', 'exec'), namespace)
        environment = SimpleNamespace(_run_docker_compose_command=AsyncMock(
            return_value=SimpleNamespace(stdout='main-id\n')))
        asyncio.run(namespace['setup'](SimpleNamespace(gpu_device=None), environment))
        return [call.args[2] for call in recorder.await_args_list if call.args[0] == 'tag']

    def test_runtime_image_survives_compose_teardown_in_either_sort_order(self):
        tags = self.tags_from_setup()
        self.assertEqual(len(set(tags)), 2, tags)
        for project in ('aaa-task-persona-0001__env', 'zzz-task-persona-0001__env'):
            with self.subTest(project=project):
                self.assertTrue(compose_down_untags(tags + [project + '-main:latest'], project))

    def test_a_single_keep_tag_is_lost_when_the_project_sorts_after_it(self):
        single = ['swe-persona-runtime:' + self.IMAGE.split(':', 1)[1]]
        project = 'zzz-task-persona-0001__env'
        self.assertFalse(compose_down_untags(single + [project + '-main:latest'], project))


class ShellCpusetTests(unittest.TestCase):
    def test_cpuset_matches_the_cpu_quota(self):
        with patch.object(dockerx.os, 'sched_getaffinity', return_value=set(range(128)), create=True):
            for cpus, count in (('2', 2), ('1.5', 2), ('1', 1)):
                with self.subTest(cpus=cpus):
                    flag, value = dockerx._cpuset(cpus)
                    chosen = [int(cpu) for cpu in value.split(',')]
                    self.assertEqual(flag, '--cpuset-cpus')
                    self.assertEqual(len(set(chosen)), count)
                    self.assertTrue(all(0 <= cpu < 128 for cpu in chosen))
        with patch.object(dockerx.os, 'sched_getaffinity', return_value={3}, create=True):
            self.assertEqual(dockerx._cpuset('2'), ['--cpuset-cpus', '3'])

    def test_episode_and_checker_containers_are_pinned(self):
        commands = []

        def run(cmd, timeout=600.0, input_text=None):
            commands.append(cmd)
            return (1, '', 'No such object') if cmd[1] == 'inspect' else (0, 'container-id', '')

        with patch.object(dockerx, '_run', side_effect=run), \
             patch.object(dockerx.os, 'sched_getaffinity', return_value=set(range(128)), create=True):
            dockerx.Container(name='fixture', image='image', cpus='2').start()
            dockerx.run_ephemeral('image', 'true', cpus='2')
        runs = [cmd for cmd in commands if cmd[1] == 'run']
        self.assertEqual(len(runs), 2)
        for cmd in runs:
            index = cmd.index('--cpuset-cpus')
            self.assertEqual(cmd[index - 2:index], ['--cpus', '2'])
            self.assertEqual(len(cmd[index + 1].split(',')), 2)


if __name__ == '__main__':
    unittest.main()
