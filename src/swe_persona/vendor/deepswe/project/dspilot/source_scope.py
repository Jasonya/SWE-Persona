"""Literal source roots plus opt-in root-file selectors such as root:*.go.

The prefix is explicit so existing literal roots and checker glob semantics stay
unchanged. A root-file selector never grants access to subdirectories. This file
also runs as standalone standard-library code in disposable task containers.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
from pathlib import Path, PurePosixPath
import re


def root_pattern(selector):
    if not selector.startswith('root:'):
        return None
    if not re.fullmatch(r'root:\*\.[A-Za-z0-9_-]+', selector):
        raise ValueError('Root source selector must be root:*.<extension>')
    return selector[5:]


def root_file_matches(name, selector):
    pattern = root_pattern(selector)
    return bool(pattern and '/' not in name and '\\' not in name
                and name not in ('', '.', '..') and fnmatch.fnmatchcase(name, pattern))


def source_matches(name, roots):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts:
        return False
    return any(root_file_matches(name, root) if root_pattern(root) else
               root == '.' or name == root or name.startswith(root.rstrip('/') + '/')
               for root in roots)


def check_candidate_scope(snapshot, roots):
    """Public committed-path check, independent of hidden tests/final grading.

    The agent can repair its candidate; never broaden the declared scope or
    remove files automatically. Dirty/user-owned files retain separate checks.
    """
    paths=(snapshot or {}).get('files')
    if not isinstance(paths,list) or not all(isinstance(p,str) and p for p in paths):
        return {'status':'unknown','errors':['Missing committed-path observations.'],
                'allowed_roots':list(roots),'repairable':True}
    outside=sorted({p for p in paths if not source_matches(p,roots)})
    return {'status':'not_met' if outside else 'met', 'allowed_roots':list(roots),
        'outside_paths':outside,'repairable':True,
        'errors':['Outside the disclosed task source scope: '+p for p in outside],
        'evidence_scope':'Committed candidate paths relative to the pinned baseline; not functional acceptance.',
        'repair_guidance':'Remove only your own out-of-scope changes from the candidate patch and commit the correction. Preserve preexisting/user-owned work. Put requested evidence under /deliverables; do not expand the allowed roots.' if outside else None}


def source_files(base, roots, *, ordered=False):
    base = Path(base)
    result = []
    for root in roots:
        pattern = root_pattern(root)
        if pattern:
            result.extend(p for p in base.glob(pattern) if p.is_file() or p.is_symlink())
        elif (base / root).exists():
            path = base / root
            result.extend(sorted(p for p in path.rglob('*')
                                 if p.is_file() and '__pycache__' not in p.parts)
                          if path.is_dir() else [path])
    return result if ordered else sorted(set(result))


def source_digest(base, roots):
    base = Path(base)
    digest = hashlib.sha256()
    for path in source_files(base, roots):
        name = path.relative_to(base).as_posix()
        digest.update(name.encode() + b'\0')
        # A newly introduced root symlink changes identity without reading its
        # target. Existing literal-root hashing remains byte-for-byte compatible.
        if path.is_symlink() and any(root_file_matches(name, root) for root in roots):
            digest.update(b'ROOT_SOURCE_SYMLINK\0' + os.fsencode(os.readlink(path)))
        else:
            digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def describe_root(root, *, recursive=True):
    pattern = root_pattern(root)
    if pattern:
        return f'{pattern} files directly in the repository root (no subdirectories)'
    return root.rstrip('/') + '/**' if recursive else root


def git_pathspec(root):
    pattern = root_pattern(root)
    return ':(top,glob)' + pattern if pattern else root
