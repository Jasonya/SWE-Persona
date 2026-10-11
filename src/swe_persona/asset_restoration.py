"""Explicit, checksum-bound repairs for unavailable upstream build assets."""
import hashlib
import json

OLD_FONT_REV = '527de472741cabc95291aff416337569c5d91d6e'
FONT_REV = 'f01fff049773b5a3141538a1f3cb3dd1beadae82'
FONT_SHA256 = 'fb37e6ad75f38640739c416c0325f9c5c5ccff48a05558f9eeb15557cfa8ab26'


def restore_tb3_assets(task, task_id):
    if task_id in {'freecad-impeller', 'freecad-platform-drawing', 'freecad-spring-clip'}:
        path = task / 'tests/Dockerfile'
        original = path.read_bytes()
        target = "RUN pip install --no-cache-dir 'gnucleus-freecad-validator[render]==0.1.3'"
        text = original.decode()
        if text.count(target) != 1:
            raise ValueError('Unexpected FreeCAD verifier dependency recipe')
        replacement = ("RUN rm -rf /opt/conda/lib/python3.11/site-packages/vtk-9.2.6.egg-info\n"
                       "RUN pip install --ignore-installed --no-deps --no-cache-dir 'vtk==9.7.1'\n" + target)
        path.write_text(text.replace(target, replacement))
        (task / 'asset-restoration.json').write_text(json.dumps({
            'variant': 'freecad-verifier-vtk-metadata-v1',
            'vtk_version': '9.7.1',
            'changes': [{'file': 'tests/Dockerfile',
                         'original_sha256': hashlib.sha256(original).hexdigest(),
                         'materialized_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}],
            'reason': 'Remove obsolete distutils VTK metadata and install pinned VTK without replacing other Conda dependencies before the original validator installation. Task and grader code are unchanged.'
        }, indent=2) + '\n')
        return
    if task_id != 'layout-config-recreation2':
        return
    changes = []
    for relative in ('environment/Dockerfile', 'tests/Dockerfile'):
        path = task / relative
        original = path.read_bytes()
        text = original.decode()
        old = 'ARG FONT_REV=' + OLD_FONT_REV
        if old not in text:
            raise ValueError('Unexpected font asset binding in ' + relative)
        marker = 'tar -xJf /tmp/fonts.tar.xz'
        if text.count(marker) != 1:
            raise ValueError('Unexpected font extraction recipe in ' + relative)
        text = text.replace(old, 'ARG FONT_REV=' + FONT_REV)
        text = text.replace(marker, 'echo "' + FONT_SHA256 + '  /tmp/fonts.tar.xz" | sha256sum -c - && ' + marker)
        path.write_text(text)
        changes.append({'file': relative, 'original_sha256': hashlib.sha256(original).hexdigest(),
                        'materialized_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    (task / 'asset-restoration.json').write_text(json.dumps({
        'variant': 'upstream-restored-google-fonts-v1',
        'original_revision': OLD_FONT_REV, 'restored_revision': FONT_REV,
        'archive_sha256': FONT_SHA256, 'changes': changes,
        'reason': 'The original pinned revision returns HTTP 404. Use the upstream restored snapshot with an enforced archive checksum.'
    }, indent=2) + '\n')
