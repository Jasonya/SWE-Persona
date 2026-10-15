"""Restore known native entrypoint metadata in the private checker copy only."""
import hashlib
import json
from pathlib import Path
import stat
import sys

POLICY = 'tb3-native-entrypoint-permissions-v1'
WRAPPERS = {
    # The native tests/test_state.py restores this wrapper to 0555 before
    # executing it. Artifact materialization can leave it at 0644.
    'risk-scorer-replay': (
        'app/rebuild_parity_report.sh',
        '7c4f2e1e46fd940b4383b3f02e1cdc72ed2bd62b03fedc88e3c92dc9e23b957b',
    ),
}


def restore_permissions(task_id, *, root=Path('/')):
    if task_id not in WRAPPERS:
        return []
    relative, expected = WRAPPERS[task_id]
    path = Path(root) / relative
    record = {'policy': POLICY, 'path': '/' + relative, 'target_mode': '0o555'}
    try:
        info = path.lstat()
    except FileNotFoundError:
        return [dict(record, status='skipped', reason='missing')]
    record['mode_before'] = oct(stat.S_IMODE(info.st_mode))
    if not stat.S_ISREG(info.st_mode):
        return [dict(record, status='skipped', reason='not_regular')]
    with path.open('rb') as stream:
        record['sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
    if record['sha256'] != expected:
        # Leave missing or replaced submissions to the existing checker.
        # Permission recovery must not repair or replace candidate contents.
        return [dict(record, status='skipped', reason='content_mismatch')]
    path.chmod(0o555)
    record.update(mode_after=oct(stat.S_IMODE(path.stat().st_mode)),
                  status='unchanged' if record['mode_before'] == '0o555' else 'restored')
    return [record]


if __name__ == '__main__':
    print(json.dumps(restore_permissions(sys.argv[1])))
