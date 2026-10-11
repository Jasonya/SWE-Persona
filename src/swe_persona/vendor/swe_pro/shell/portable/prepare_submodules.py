#!/usr/bin/env python3.12
"""Prepare public submodule mirrors outside the immutable release directory."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

SOURCES = {'vuls-integration.git': 'https://github.com/vulsio/integration',
           'teleport-webassets.git': 'https://github.com/gravitational/webassets.git'}


def git(*args):
    p = subprocess.run(['git', *args], check=True, capture_output=True, text=True, timeout=900)
    return p.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cache', type=Path, required=True)
    ap.add_argument('--receipt', type=Path, required=True)
    ap.add_argument('--required-commits', type=Path,
                    help='Optional JSON mapping mirror name to required gitlink commit IDs.')
    args = ap.parse_args()
    cache = args.cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    required = json.loads(args.required_commits.read_text()) if args.required_commits else {}
    receipts = {}
    for name, url in SOURCES.items():
        path = cache / name
        if not path.exists():
            git('clone', '--mirror', url, str(path))
        if git('-C', str(path), 'remote', 'get-url', 'origin').strip() != url:
            raise ValueError('Unexpected origin in public submodule cache: ' + name)
        git('-C', str(path), 'fsck', '--full')
        for commit in required.get(name, []):
            git('-C', str(path), 'cat-file', '-e', commit + '^{commit}')
        refs = git('-C', str(path), 'show-ref')
        receipts[name] = {'url': url, 'fsck': 'pass',
                          'refs_sha256': hashlib.sha256(refs.encode()).hexdigest(),
                          'required_commits_verified': required.get(name, []),
                          'identity_scope': 'Exact task gitlinks are verified when each pinned base is checked out; current refs are not task commit pins.'}
    args.receipt.write_text(json.dumps(receipts, indent=2) + '\n')
    print(json.dumps(receipts, indent=2))


if __name__ == '__main__':
    main()
