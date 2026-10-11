"""Build submitted Docsify source and check the existing-browser user path."""
import json
from pathlib import Path
import subprocess
import sys


def verdict(record):
    steps = record.get('steps', [])
    if len(steps) != 2 or {s['action'] for s in steps} != {'click_cover_title', 'direct_fragment'}:
        return 'unknown', 'browser_path_incomplete'
    if record.get('external_requests'):
        return 'fail', 'unauthorized_external_dependency'
    if not all(s['navigation_pass'] and s['content_pass'] for s in steps):
        return 'fail', 'navigation_or_business_content_changed'
    return 'pass', 'business_content_and_navigation_preserved'


def main():
    root = Path('/testbed')
    base = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    build = subprocess.run(['npm', 'run', 'build:js'], cwd=root, capture_output=True, text=True, timeout=180)
    if build.returncode:
        print(json.dumps({'base_commit': base, 'candidate_build_failed': True, 'records': [],
                          'build': {'exit_code': build.returncode, 'output_tail': (build.stdout + build.stderr)[-3000:]}}))
        return
    args = ['node', str(Path(__file__).with_name('docsify_browser.cjs'))]
    if '--calibration' in sys.argv:
        args.append('--calibration')
    output = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=90)
    if output.returncode:
        raise RuntimeError('Browser fixture unavailable: ' + output.stderr[-3000:])
    observed = json.loads(output.stdout)
    for record in observed['records']:
        record['status'], record['reason'] = verdict(record)
    print(json.dumps({'base_commit': base, **observed,
                      'build': {'exit_code': build.returncode, 'output_tail': (build.stdout + build.stderr)[-2000:]}}))


if __name__ == '__main__':
    main()
