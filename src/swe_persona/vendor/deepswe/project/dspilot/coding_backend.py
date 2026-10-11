"""Optional coding transports; the staged runner still owns execution and checks.

No task, persona, future-stage plan, checker or credential is passed to a backend.
DS_CODING_BACKEND is deliberately separate from host model/credential settings.
"""
import hashlib
import json
import os
from pathlib import Path

JSON_ACT_VERSION = 'json-act-v2-feedback'

BACKENDS = ('json_act', 'native_act', 'terminus2_compat', 'terminus2_native')


def selected_backend():
    name = os.environ.get('DS_CODING_BACKEND', 'native_act')
    if name not in BACKENDS:
        raise ValueError('Unknown coding backend; choose json_act, native_act, terminus2_compat or terminus2_native')
    return name


def backend_metadata(name=None):
    name = selected_backend() if name is None else name
    if name not in BACKENDS:
        raise ValueError('Unknown coding backend')
    if name == 'terminus2_native':
        from .terminus_dependency import runtime_metadata
        return runtime_metadata()
    if name == 'json_act':
        return {'name':'json_act','version':JSON_ACT_VERSION,'executor':'Box.shell','max_commands_per_response':1,
                'max_transport_retries_per_action':2,'max_transport_retries_per_episode':12}
    if name == 'native_act':
        return {'name': name, 'version': 'native-act-v1', 'executor': 'Box.shell'}
    vendor = Path(__file__).parent / 'vendor/terminus2'
    provenance = json.loads((vendor / 'PROVENANCE.json').read_text())
    for file, record in provenance['files'].items():
        if hashlib.sha256((vendor / file).read_bytes()).hexdigest() != record['sha256']:
            raise ValueError('Pinned Terminus component changed: ' + file)
    return {'name': name, 'version': 'terminus2-compat-v1', 'executor': 'Box.shell',
            'upstream_commit': provenance['commit'], 'upstream_files': provenance['files'],
            'scope': 'Terminus JSON parser and adapted action protocol; not the native tmux agent',
            'transport': 'Responses terminus_step tool', 'max_commands_per_response': 1,
            'automatic_summarization': False}


def verify_backend(receipt):
    """Old receipts mean native only; never silently switch an admitted backend."""
    actual = backend_metadata()
    expected = receipt.get('coding_backend', backend_metadata('native_act'))
    if expected != actual:
        raise ValueError('Coding backend differs from the frozen configuration; freeze a new version')
    return actual


def backend_sources(root):
    root = Path(root)
    paths = [root / 'dspilot' / name for name in
             ('coding_backend.py', 'coding_json.py', 'harbor_runtime.py', 'coding_native.py', 'coding_terminus.py',
              'coding_terminus_native.py', 'terminus_dependency.py', 'terminus_environment.py')]
    paths += [p for p in (root / 'dspilot/vendor').rglob('*')
              if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc']
    paths += [root / "requirements-terminus2.lock", root / "environments/terminus2/Dockerfile"]
    paths += [root/'dspilot'/name for name in ('runtime_backend.py','pier_box.py','pier_trace.py','pier_runtime.py','pier_native_runtime.py','pier_native_grading.py')]
    paths += [root/'pier-source-lock.json',root/'requirements-pier.lock']
    return sorted(paths)


class NativeBackend:
    """Wrap the original action function without changing the API wire history."""
    def __init__(self, client, out, rejected):
        from .coding_native import AGENT_PROTOCOL
        self.client, self.out, self.rejected = client, out, rejected
        self.history = [{'role': 'system', 'content': AGENT_PROTOCOL}]
        self.metadata = backend_metadata('native_act')
        self.pending = None

    def lifecycle(self, box):
        from contextlib import nullcontext
        return nullcontext()

    def deliver_user(self, message):
        if self.pending is not None:
            raise ValueError('Cannot deliver a user message before observing the dispatched action')
        self.history.append({'role': 'user', 'content': message})

    def next_action(self, number):
        from .coding_native import request_native_action
        if self.pending is not None:
            raise ValueError('Previous action has no execution observation')
        action, identity, response = request_native_action(
            self.client, self.history, self.out, number, self.rejected)
        self.history.extend(response['output'])
        self.pending = identity
        return action, identity, response

    def observe(self, identity, value):
        if self.pending is None or identity != self.pending:
            raise ValueError('Observation does not match the dispatched action')
        self.history.append({'type': 'function_call_output', 'call_id': identity,
                             'output': json.dumps(value)[-24000:]})
        self.pending = None


def create_backend(client, out, rejected):
    name = selected_backend()
    if name == 'json_act':
        from .coding_json import JsonActBackend
        return JsonActBackend(client,out,rejected)
    if name == 'native_act':
        return NativeBackend(client, out, rejected)
    if name == 'terminus2_native':
        from .coding_terminus_native import NativeTerminusBackend
        return NativeTerminusBackend(client, out, rejected)
    # Lazy import: the original backend does not load the Terminus parser.
    from .coding_terminus import TerminusCompatBackend
    return TerminusCompatBackend(client, out, rejected)


def add_backend_argument(parser):
    from .runtime_backend import add_runtime_argument
    add_runtime_argument(parser)
    parser.add_argument('--coding-backend', choices=BACKENDS,
                        help='Optional coding transport; default native_act or DS_CODING_BACKEND. Freeze and run must match.')


def apply_backend_argument(args):
    from .runtime_backend import apply_runtime_argument
    apply_runtime_argument(args)
    if args.coding_backend is not None:
        os.environ['DS_CODING_BACKEND'] = args.coding_backend
    selected_backend()
