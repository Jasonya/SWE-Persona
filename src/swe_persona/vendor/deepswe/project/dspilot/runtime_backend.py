"""Runtime selection is independent of coding-agent selection; Pier is optional."""
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path

PIER_COMMIT = '4d3c14041d16443f3f9f460dcdf23629994a304e'

def selected_runtime():
    value = os.environ.get('DS_RUNTIME', 'legacy')
    if value not in ('legacy', 'pier', 'pier_native', 'harbor'):
        raise ValueError('Unknown runtime; choose legacy, pier or pier_native')
    return value

def runtime_metadata():
    name = selected_runtime()
    if name == 'harbor':
        from .harbor_runtime import metadata
        return metadata()
    if name == 'legacy':
        return {'name': 'legacy', 'version': 'legacy-box-v1'}
    from .coding_backend import selected_backend
    if selected_backend() != 'native_act':
        raise ValueError('Pier runtime v1 supports native_act only; legacy retains all coding backends')
    spec = importlib.util.find_spec('pier')
    if spec is None:
        raise ValueError('Optional Pier dependency missing; install requirements-pier.lock in a separate venv')
    root = Path(next(iter(spec.submodule_search_locations)))
    lock = json.loads((Path(__file__).parents[1]/'pier-source-lock.json').read_text())
    actual = {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
              for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'}
    if actual != lock['files']:
        raise ValueError('Installed Pier source differs from the pinned source lock')
    # Record the actual resolved dependency environment as well as source identity.
    packages = sorted((d.metadata['Name'].lower(), d.version) for d in importlib.metadata.distributions()
                      if d.metadata.get('Name'))
    return {'name':name, 'version':'pier-native-v2' if name=='pier_native' else 'pier-benchmark-v1', 'upstream_commit':PIER_COMMIT,
            'source_sha256':hashlib.sha256(json.dumps(actual,sort_keys=True).encode()).hexdigest(),
            'dependency_versions':dict(packages), 'coding_backend':'native_act',
            'evaluation':'unchanged benchmark DeepSWE grader and public checks',
            'scope':('Pier Trial, coding segments, native collect and separate original verifier; external interaction coordinator' if name=='pier_native' else 'Pier Trial and Docker lifecycle; benchmark interaction and grading remain host-owned')}

def verify_runtime(receipt):
    actual = runtime_metadata()
    if receipt.get('runtime_backend', {'name':'legacy','version':'legacy-box-v1'}) != actual:
        raise ValueError('Runtime differs from frozen configuration; freeze a fresh version')
    return actual

def add_runtime_argument(parser):
    parser.add_argument('--runtime', choices=('legacy','pier','pier_native','harbor'), default=None,
                        help='Optional Pier trial runtime; default legacy')

def apply_runtime_argument(args):
    if getattr(args,'runtime',None) is not None:
        os.environ['DS_RUNTIME'] = args.runtime
    selected_runtime()
