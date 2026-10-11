"""Explicit NVIDIA device allocation for both Harbor agent and verifier environments."""
import json
import os
import re
from pathlib import Path
import yaml
from harbor.environments.docker.docker import DockerEnvironment


def pin_task_gpu(task_dir, count):
    """Require an explicit host device; never expose all GPUs by default."""
    if not count:
        return None
    selection = os.environ.get('SWE_PERSONA_GPU_DEVICES', '')
    if count != 1 or not re.fullmatch(r'(?:[0-9]+|GPU-[0-9a-fA-F-]+)', selection):
        raise ValueError('This runtime requires one explicit SWE_PERSONA_GPU_DEVICES index or GPU UUID')
    reservation = [{'driver': 'nvidia', 'device_ids': [selection], 'capabilities': ['gpu']}]
    for role in ('environment', 'tests'):
        path = Path(task_dir) / role / 'docker-compose.yaml'
        config = yaml.safe_load(path.read_text()) if path.exists() else {'services': {'main': {}}}
        main = config.setdefault('services', {}).setdefault('main', {})
        main.setdefault('deploy', {}).setdefault('resources', {}).setdefault('reservations', {})['devices'] = reservation
        path.write_text(yaml.safe_dump(config, sort_keys=False))
    return selection


class PinnedNvidiaDockerEnvironment(DockerEnvironment):
    @property
    def capabilities(self):
        return super().capabilities.model_copy(update={'gpus': True})

    def _validate_gpu_support(self):
        super()._validate_gpu_support()
        if self._effective_gpus:
            if self._effective_gpus != 1:
                raise ValueError('Only one explicitly reserved GPU is supported')
            config = yaml.safe_load((self.environment_dir / 'docker-compose.yaml').read_text())
            devices = config['services']['main']['deploy']['resources']['reservations']['devices']
            expected = os.environ.get('SWE_PERSONA_GPU_DEVICES')
            if devices != [{'driver': 'nvidia', 'device_ids': [expected], 'capabilities': ['gpu']}]:
                raise ValueError('GPU reservation differs from explicit host selection')
            self._pinned_device = [expected]

    async def start(self, force_build):
        await super().start(force_build=force_build)
        if self._effective_gpus:
            result = await self._run_docker_compose_command(['ps', '-q', 'main'])
            from terminal_agent import docker
            details = json.loads(await docker('inspect', result.stdout.strip()))[0]
            requests = details['HostConfig'].get('DeviceRequests') or []
            if len(requests) != 1 or requests[0].get('DeviceIDs') != self._pinned_device:
                raise RuntimeError('Actual GPU allocation differs from declaration')
            check = await self.exec(command='nvidia-smi --query-gpu=name,uuid --format=csv,noheader', timeout_sec=30)
            if check.return_code != 0 or len(check.stdout.strip().splitlines()) != 1:
                raise RuntimeError('Exactly one functioning GPU must be visible')
            self.gpu_receipt = {'device_ids': self._pinned_device, 'actual_hardware': check.stdout.strip(),
                                'device_requests': requests}
