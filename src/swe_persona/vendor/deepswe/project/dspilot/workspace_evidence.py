"""Git-visible pre-existing changes: byte identities, index state and ownership.

This collector is also executed verbatim inside the isolated repository. It uses
NUL-separated paths and never returns file contents. Existing changed paths are
reserved as a whole; editing another hunk in one requires a separate ownership
agreement, which the current staged protocol does not implement.
"""
import hashlib
import os
import stat
import subprocess


def capture(root):
    def git(*args):
        return subprocess.check_output(['git','-C',str(root),*args])
    def paths(*args):
        return {os.fsdecode(p) for p in git(*args).split(b'\0') if p}
    staged=paths('diff','--cached','--name-only','--no-renames','-z','HEAD')
    unstaged=paths('diff','--name-only','--no-renames','-z')
    untracked=paths('ls-files','--others','--exclude-standard','-z')
    changed=staged|unstaged|untracked
    index={p:[] for p in changed}
    for row in git('ls-files','--stage','-z').split(b'\0'):
        if not row:continue
        meta,path=row.split(b'\t',1);path=os.fsdecode(path)
        if path in index:index[path].append(meta.decode('ascii'))
    files={}
    for name in sorted(changed):
        path=os.path.join(root,name)
        try:
            info=os.lstat(path);mode=stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                content=os.fsencode(os.readlink(path));kind='symlink'
                digest=hashlib.sha256(content).hexdigest()
            elif stat.S_ISREG(info.st_mode):
                h=hashlib.sha256()
                with open(path,'rb') as f:
                    for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
                digest=h.hexdigest();kind='file'
            else:
                raise ValueError('Unsupported changed workspace object: '+name)
            value={'kind':kind,'mode':mode,'sha256':digest}
        except FileNotFoundError:value={'kind':'missing'}
        files[name]={'working':value,'index':sorted(index[name])}
    return {'version':1,'staged':sorted(staged),'unstaged':sorted(unstaged),
            'untracked':sorted(untracked),'files':files}


def preservation_errors(initial, current, committed_paths=(), *, final=True):
    """Missing host evidence is an error, never evidence of a clean workspace."""
    if not all(isinstance(v,dict) and v.get('version')==1 and
               isinstance(v.get('files'),dict) and
               all(isinstance(v.get(k),list) for k in ('staged','unstaged','untracked'))
               for v in (initial,current)):
        return ['Initial/current workspace identity evidence is missing.']
    errors=[];owned=set(initial['files'])
    for path in sorted(owned):
        if initial['files'][path]!=current['files'].get(path):
            errors.append('Pre-existing working/index content was changed, removed or incorporated: '+path)
        elif any((path in initial[k])!=(path in current[k]) for k in ('staged','unstaged','untracked')):
            errors.append('Pre-existing Git staging state was changed: '+path)
    if owned.intersection(committed_paths):
        errors.append('Committed task patch overlaps pre-existing changed paths: '+', '.join(sorted(owned.intersection(committed_paths))))
    if final and set(current['files'])-owned:
        errors.append('New uncommitted task changes remain: '+', '.join(sorted(set(current['files'])-owned)))
    return errors
