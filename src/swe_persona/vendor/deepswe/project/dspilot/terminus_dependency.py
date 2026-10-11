"""Optional native Terminus installation/environment identity (stdlib only)."""
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import re
import sys

COMMIT = '31668af15560fb50a9d0584816d12726036656cf'


def runtime_metadata():
    if sys.version_info < (3, 12):
        raise ValueError('Native Terminus requires Python >=3.12 in its optional environment')
    try:
        dist = metadata.distribution('harbor')
    except metadata.PackageNotFoundError:
        raise ValueError('Native Terminus is optional: install requirements-terminus2.lock in a separate venv') from None
    origin = json.loads(dist.read_text('direct_url.json') or '{}')
    if origin.get('vcs_info', {}).get('commit_id') != COMMIT:
        raise ValueError('Native Terminus requires the pinned Harbor Git commit')
    image = os.environ.get('DS_TERMINUS_IMAGE', '')
    if not re.fullmatch(r'(?:[^\s]+@)?sha256:[a-f0-9]{64}', image):
        raise ValueError('Set DS_TERMINUS_IMAGE to an immutable image ID/digest with tmux; see optional Dockerfile')
    context = int(os.environ.get('DS_TERMINUS_CONTEXT_TOKENS', '128000'))
    if not 16000 <= context <= 1000000:
        raise ValueError('DS_TERMINUS_CONTEXT_TOKENS outside supported configured range')
    sources = {}
    for entry in dist.files or []:
        if str(entry).startswith('harbor/') and entry.suffix in ('.py', '.txt', '.sh', '.json'):
            sources[str(entry)] = hashlib.sha256(Path(dist.locate_file(entry)).read_bytes()).hexdigest()
    if 'harbor/agents/terminus_2/terminus_2.py' not in sources:
        raise ValueError('Harbor source manifest incomplete')
    return {'name': 'terminus2_native', 'version': 'terminus2-interactive-v3',
            'upstream_commit': COMMIT, 'harbor_version': dist.version,
            'harbor_source_sha256': hashlib.sha256(json.dumps(sources,sort_keys=True).encode()).hexdigest(),
            'dependency_versions': {d.metadata['Name']: d.version for d in metadata.distributions()},
            'agent_image': image, 'executor': 'upstream Terminus2 + TmuxSession',
            'context_budget_tokens': context, 'automatic_summarization': True,
            'cost_usd_status': 'unavailable; upstream numeric zero is not measured free usage',
            'snapshot_boundary': 'Docker cgroup pause; isolated observer reads frozen task root',
            'observer_resources': {'cpus':0.5,'memory':'512m','pids':64,'network':'none'},
            'trajectory_authority': 'benchmark action records; upstream trajectory may include undispatched batch proposals',
            'interaction_extensions': ['explicit reply/validate', 'controller-owned completion',
                                       'pause between terminal operations', 'discard stale command batches'],
            'terminal_exit_codes': 'unknown; terminal observations are not shell completion receipts'}


def check_image(base_image):
    import subprocess
    image = runtime_metadata()['agent_image']
    p = subprocess.run(['docker','image','inspect',image],capture_output=True,text=True,timeout=30)
    if p.returncode:
        raise ValueError('Native Terminus agent image unavailable')
    info = json.loads(p.stdout)[0]
    if image != base_image and (info.get('Config', {}).get('Labels') or {}).get('dspilot.terminus.base-image') != base_image:
        raise ValueError('Native Terminus image is not labeled with this pinned task base image')
    p = subprocess.run(['docker','run','--rm','--network','none','--platform','linux/amd64',
                        '--entrypoint','bash',image,'-lc','command -v tmux >/dev/null'],
                       capture_output=True,timeout=45)
    if p.returncode:
        raise ValueError('Native Terminus agent image must already contain tmux; runtime installation is disabled')
    return {'passed': True, 'agent_image': image, 'base_image': base_image, 'tmux_available': True}
