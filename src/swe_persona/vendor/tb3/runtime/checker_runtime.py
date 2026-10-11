"""Provision the private Python grader without altering the agent environment."""
import asyncio
import hashlib
import tempfile
from pathlib import Path

from terminal_agent import docker

RECIPE = '''ARG MAIN_IMAGE
FROM ${MAIN_IMAGE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends python3 && rm -rf /var/lib/apt/lists/*
'''

# These public native tasks export a git diff instead of the modified tree.
# Reconstruct it before overlaying additional persona exports; otherwise the
# checker sees the image's original source instead of the submitted source.
PATCH_ROOTS = {
    'cumulative-layout-shift': '/app/barber-shop-site',
    'shadow-relay': '/app',
}


async def materialize_collected_patch(task_id, artifacts_dir, container):
    if task_id not in PATCH_ROOTS:
        return False
    patch = Path(artifacts_dir) / 'tmp/agent.patch'
    if not patch.is_file():
        raise RuntimeError('Native collection patch is unavailable; cannot reconstruct submitted source')
    await docker('cp', str(patch), container + ':/tmp/persona-collected.patch')
    await docker('exec', container, 'git', '-C', PATCH_ROOTS[task_id],
        'apply', '--allow-empty', '--whitespace=nowarn', '/tmp/persona-collected.patch')
    return True


async def ensure_checker_python(main_image):
    """Reuse Python when present, otherwise build a Debian checker-only layer.

    This is infrastructure preparation, before the offline grading container.
    Docker labels bind cached layers to the exact base image and recipe.
    Build failures propagate to the caller as infrastructure unknown.
    """
    identity = (await docker('image', 'inspect', '--format', '{{.Id}}', main_image)).strip()
    exists = (await docker('run', '--rm', '--network', 'none', '--user', '0:0',
        '--entrypoint', '/bin/sh', identity, '-c', 'command -v python3 || true')).strip()
    if exists:
        return identity
    recipe_hash = hashlib.sha256(RECIPE.encode()).hexdigest()
    key = hashlib.sha256((identity + recipe_hash).encode()).hexdigest()[:24]
    tag = 'pcb-persona-checker-python:' + key
    try:
        labels = await docker('image', 'inspect', '--format', '{{index .Config.Labels "org.personacodebench.checker.base"}} {{index .Config.Labels "org.personacodebench.checker.recipe"}}', tag)
    except RuntimeError:
        labels = ''
    if labels.strip() != identity + ' ' + recipe_hash:
        base_tag = 'pcb-persona-checker-base:' + key
        await docker('tag', identity, base_tag)
        with tempfile.TemporaryDirectory(prefix='pcb-checker-python-') as temporary:
            Path(temporary, 'Dockerfile').write_text(RECIPE)
            await docker('build', '--build-arg', 'MAIN_IMAGE=' + base_tag,
                '--label', 'org.personacodebench.checker.base=' + identity,
                '--label', 'org.personacodebench.checker.recipe=' + recipe_hash,
                '-t', tag, temporary)
    await docker('run', '--rm', '--network', 'none', '--user', '0:0',
        '--entrypoint', 'python3', tag, '-c', 'import json, pathlib, subprocess; print("checker-python-ready")')
    return (await docker('image', 'inspect', '--format', '{{.Id}}', tag)).strip()
