"""Offline repetitions of the public Loki Remove_Keys envelope, all runs kept."""
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time

ROOT = Path('/testbed')
SAMPLES = 100000


def trial(workers, index):
    lock = threading.Lock()
    state = {'records': 0, 'wrong_records': 0, 'examples': [], 'requests': 0, 'errors': []}

    class Sink(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                body = self.rfile.read(int(self.headers['Content-Length']))
                if self.headers.get('Content-Encoding') == 'gzip':
                    body = gzip.decompress(body)
                data = json.loads(body)
                with lock:
                    state['requests'] += 1
                    for stream in data['streams']:
                        for value in stream['values']:
                            line = json.loads(value[1])
                            state['records'] += 1
                            if line != {'log': 'message'}:
                                state['wrong_records'] += 1
                                if len(state['examples']) < 3:
                                    state['examples'].append(line)
                self.send_response(204)
            except Exception as error:
                with lock:
                    state['errors'].append(type(error).__name__)
                self.send_response(400)
            self.end_headers()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Sink)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = f'''[SERVICE]
    Flush 0.2
    Grace 1
    Log_Level error
[INPUT]
    Name dummy
    Interval_sec 0
    Interval_nsec 100
    Samples {SAMPLES}
    Threaded on
    Dummy {{"log":"message","kubernetes":{{"pod_name":"pod-name-01"}},"severity":"warn","k8s_cluster_name":"cls01"}}
[OUTPUT]
    Name loki
    Match *
    Host 127.0.0.1
    Port {server.server_port}
    Remove_Keys nonexistent,kubernetes,severity,k8s_cluster_name
    Structured_Metadata pod=$kubernetes['pod_name'],level=$severity
    Labels cluster=$k8s_cluster_name
    Retry_Limit 0
    Workers {workers}
'''
    with tempfile.TemporaryDirectory(prefix='persona-loki-') as temporary:
        path = Path(temporary)
        (path / 'fluent.conf').write_text(config)
        started = time.monotonic()
        with (path / 'stderr.txt').open('w+') as error_log:
            process = subprocess.Popen([str(ROOT / 'build/bin/fluent-bit'), '-c', str(path / 'fluent.conf')],
                                       stdout=subprocess.DEVNULL, stderr=error_log, start_new_session=True)
            while time.monotonic() - started < 20 and process.poll() is None:
                with lock:
                    complete = state['records'] >= SAMPLES
                if complete:
                    break
                time.sleep(.05)
            timed_out = state['records'] < SAMPLES and process.poll() is None
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            error_log.seek(0)
            stderr = error_log.read()
        server.shutdown()
        server.server_close()
        thread.join()
    return {'workers': workers, 'repetition': index, 'config': config, 'expected_records': SAMPLES,
            'seconds': time.monotonic() - started, 'timed_out': timed_out, 'exit_code': process.returncode,
            'stderr': stderr[-3000:], **state,
            'complete': state['records'] == SAMPLES and not state['errors'] and not timed_out}


def main():
    binary = ROOT / 'build/bin/fluent-bit'
    # Identify the measured image executable explicitly. These are baseline
    # observations, never a claim that a candidate source patch was tested.
    records = [trial(workers, iteration) for iteration in range(1, 4) for workers in (0, 2)]
    paths = ['plugins/out_loki/loki.c', 'plugins/out_loki/loki.h', 'src/flb_mp.c']
    sources = []
    for name in paths:
        content = (ROOT / name).read_text()
        terms = ('remove_keys', 'flb_mp_accessor_keys_remove', 'mp_accessor', 'cb_loki_flush')
        lines = content.splitlines()
        include = set()
        for number, line in enumerate(lines):
            if any(term in line for term in terms):
                include.update(range(max(0, number - 2), min(len(lines), number + 9)))
        sources.append({'path': name, 'sha256': hashlib.sha256(content.encode()).hexdigest(),
                        'excerpts': '\n'.join(f'{i + 1}: {lines[i]}' for i in sorted(include))})
    print(json.dumps({'base_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                      'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(), 'records': records,
                      'sources': sources, 'measurement_scope': 'pinned image executable; source excerpts are baseline anchors, not causal proof'}))


if __name__ == '__main__':
    main()
