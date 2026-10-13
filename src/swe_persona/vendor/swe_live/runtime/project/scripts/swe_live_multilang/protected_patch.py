"""Project a candidate onto files outside the held-out official test patch.

Only the evaluator sees the official patch. Never feed these paths to the agent.
The complete candidate remains in predictions.json; projection is recorded.
"""
import re
import subprocess
import tempfile


def paths(patch):
    if not patch.strip():
        return set()
    with tempfile.TemporaryDirectory(prefix='swe-persona-patch-inventory-') as directory:
        result = subprocess.run(['git', 'apply', '--numstat', '-z'], input=patch.encode('utf-8'),
                                cwd=directory, capture_output=True, timeout=20)
    if result.returncode:
        raise ValueError('Cannot parse patch paths: ' + result.stderr.decode('utf-8', errors='replace')[:300])
    fields = result.stdout.split(b'\0'); names = set(); index = 0
    while index < len(fields):
        field = fields[index]; index += 1
        if not field:
            continue
        columns = field.split(b'\t', 2)
        if len(columns) != 3:
            raise ValueError('Malformed git patch path inventory')
        if columns[2]:
            names.add(columns[2].decode('utf-8'))
        else:
            names.update(f.decode('utf-8') for f in fields[index:index+2]); index += 2
    return names


def project(candidate, official_paths):
    if not candidate.strip() or not official_paths:
        return candidate, []
    chunks = re.split(r'(?m)(?=^diff --git )', candidate)
    kept, excluded = [], []
    for chunk in chunks:
        if not chunk.strip():
            continue
        if not chunk.startswith('diff --git '):
            raise ValueError('Unsupported candidate patch framing; refusing silent filtering')
        changed = paths(chunk)
        if changed & official_paths:
            excluded.extend(sorted(changed))
        else:
            kept.append(chunk)
    return ''.join(kept), sorted(set(excluded))
