#!/usr/bin/env python3
"""Container helpers adapted to SWE-bench Pro's ``/bin/bash`` entrypoint."""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import subprocess
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


def _run(cmd: List[str], timeout: float = 600.0, input_text: Optional[str] = None) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, input=input_text)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        marker = f"command timed out after {timeout:.1f} seconds"
        return 124, stdout, f"{stderr}\n{marker}".strip()


def _cpuset(cpus: str) -> List[str]:
    # --cpus is only a time quota, so the container still sees every host CPU; tools sized from the
    # visible CPUs (pytest -n auto, nproc) then start one worker per host CPU inside the memory limit.
    visible = sorted(os.sched_getaffinity(0))
    chosen = random.sample(visible, min(len(visible), max(1, math.ceil(float(cpus)))))
    return ["--cpuset-cpus", ",".join(map(str, sorted(chosen)))]


@dataclass
class Mount:
    source: str
    target: str
    read_only: bool = True

    def arg(self) -> List[str]:
        return ["-v", f"{self.source}:{self.target}{':ro' if self.read_only else ''}"]


@dataclass
class Container:
    name: str
    image: str
    mounts: List[Mount] = field(default_factory=list)
    memory: str = "4g"
    cpus: str = "2"
    network: str = "none"
    # The baseline this instance's patch is measured against.  It lives on the
    # container rather than being threaded through the controller, so the reused
    # TB2 `Controller.snapshot()` can call `snapshot_digest()` with no arguments
    # and still hash the right thing on every dataset row.
    base_commit: Optional[str] = None

    def start(self) -> str:
        rc, running, _err = _run(["docker", "inspect", "-f", "{{.State.Running}}", self.name])
        if rc == 0:
            if running.strip() == "true":
                raise RuntimeError(f"container name is already active: {self.name}")
            remove_rc, _out, remove_err = _run(["docker", "rm", self.name])
            if remove_rc != 0:
                raise RuntimeError(f"could not remove stopped container {self.name}: {remove_err.strip()}")
        cmd = [
            "docker", "run", "-d", "--name", self.name,
            "--memory", self.memory, "--cpus", self.cpus, *_cpuset(self.cpus),
            "--network", self.network, "--entrypoint", "/bin/bash",
        ]
        for mount in self.mounts:
            cmd += mount.arg()
        # SWE-bench Pro images carry toolchain paths in image ENV.  A login
        # shell can overwrite PATH (Go disappeared this way); the official
        # local evaluator uses ``bash -c`` as well.
        cmd += [self.image, "-c", "sleep infinity"]
        rc, out, err = _run(cmd, timeout=900.0)
        if rc != 0:
            raise RuntimeError(f"docker run failed: {err.strip()}")
        return out.strip()

    def sh(self, script: str, timeout: float = 600.0, workdir: str = "/app") -> Tuple[int, str, str]:
        return _run(["docker", "exec", "-w", workdir, self.name, "bash", "-c", script], timeout=timeout)

    def commit(self, tag: str) -> str:
        rc, _out, err = _run(["docker", "commit", self.name, tag], timeout=900.0)
        if rc != 0:
            raise RuntimeError(f"docker commit failed: {err.strip()}")
        return tag

    def remove(self) -> None:
        _run(["docker", "rm", "-f", self.name])

    def prepare_clean_base(self, base_commit: str, source_commit: Optional[str] = None) -> Dict[str, str]:
        """Reset the image checkout and prune future descendant history.

        This runs before the user episode begins.  It prevents an agent from
        finding the benchmark solution in local future Git objects while
        retaining the original base commit and all of its ancestors.
        """
        script = f"""
set -euo pipefail
git checkout --detach {base_commit}
git reset --hard {base_commit}
git clean -fd
# Dataset images may contain initialized submodules checked out at the image's
# source revision rather than the requested base revision.  Restore their
# recorded commits before asserting a clean workspace; --force is local-only
# when the image already contains the required objects.
# Only the host-validated, read-only public mirrors are trusted across UID boundaries.
for mirror in /opt/pcb-submodule-cache/teleport-webassets.git /opt/pcb-submodule-cache/vuls-integration.git; do
  if test -d "$mirror"; then git config --global --add safe.directory "$mirror"; fi
done
git submodule sync --recursive
if git config -f .gitmodules --get-regexp '^submodule[.].*[.]url$' 2>/dev/null | \
     grep -q 'git@github.com:gravitational/teleport[.]e[.]git'; then
  # Teleport's image records a private enterprise submodule that the benchmark
  # tasks do not modify and the isolated evaluator cannot authenticate to.
  # Restore every public submodule one level at a time, since public modules
  # such as webassets may themselves record a private enterprise child.
  restore_public_submodules() {{
    local root="$1" key url path
    test -f "$root/.gitmodules" || return 0
    while read -r key url; do
      case "$url" in
        git@github.com:gravitational/*.e.git) continue ;;
      esac
      path=$(git -C "$root" config -f .gitmodules --get "${{key%.url}}.path")
      git -C "$root" -c protocol.file.allow=always \
        -c 'url.file:///opt/pcb-submodule-cache/teleport-webassets.git.insteadOf=https://github.com/gravitational/webassets.git' \
        submodule update --init --force -- "$path"
      restore_public_submodules "$root/$path"
    done < <(git -C "$root" config -f .gitmodules --get-regexp '^submodule[.].*[.]url$')
  }}
  restore_public_submodules .
elif test -d /opt/pcb-submodule-cache/vuls-integration.git; then
  git -c protocol.file.allow=always \
      -c 'url.file:///opt/pcb-submodule-cache/vuls-integration.git.insteadOf=https://github.com/vulsio/integration' \
      submodule update --init --recursive --force
else
  git submodule update --init --recursive --force
fi
git submodule foreach --recursive '
  git reset --hard
  git clean -fd
  for remote in $(git remote); do git remote remove "$remote"; done
  for ref in $(git for-each-ref --format="%(refname)"); do git update-ref --no-deref -d "$ref"; done
  git reflog expire --expire=now --expire-unreachable=now --all
  git gc --prune=now
'
for remote in $(git remote); do git remote remove "$remote"; done
# --no-deref is essential for refs/remotes/*/HEAD.  Without it, update-ref
# deletes the symref target and leaves a dangling symbolic ref that makes
# repack/gc abort with "fatal: bad object refs/remotes/origin/HEAD".
for ref in $(git for-each-ref --format='%(refname)'); do git update-ref --no-deref -d "$ref"; done
git update-ref refs/heads/eval-base {base_commit}
git symbolic-ref HEAD refs/heads/eval-base
git reflog expire --expire=now --expire-unreachable=now --all
git gc --prune=now
test "$(git rev-parse HEAD)" = "{base_commit}"
test -z "$(git status --porcelain=v1 --untracked-files=all)"
"""
        rc, out, err = self.sh(script, timeout=1800.0)
        if rc != 0:
            detail = "\n".join(
                part for part in (
                    f"stdout:\n{out.strip()}" if out.strip() else "",
                    f"stderr:\n{err.strip()}" if err.strip() else "",
                ) if part
            )
            raise RuntimeError(f"could not prepare clean base: {detail[-4000:]}")
        future = "not_checked"
        if source_commit and source_commit != base_commit:
            rc, _out, _err = self.sh(f"git cat-file -e {source_commit}^{{commit}}", timeout=60.0)
            if rc == 0:
                raise RuntimeError(f"future source commit is still readable after pruning: {source_commit}")
            future = "pruned"
        return {"base_commit": base_commit, "source_commit": source_commit or "", "future_history": future}

    def verify_prepared_base(self, base_commit: str, source_commit: Optional[str] = None) -> Dict[str, str]:
        rc, out, err = self.sh(
            "git rev-parse HEAD; git status --porcelain=v1 --untracked-files=all", timeout=120.0,
        )
        lines = out.splitlines()
        if rc != 0 or not lines or lines[0].strip() != base_commit or any(line.strip() for line in lines[1:]):
            raise RuntimeError(f"prepared image is not a clean {base_commit}: {(err or out).strip()[-2000:]}")
        future = "not_checked"
        if source_commit and source_commit != base_commit:
            rc, _out, _err = self.sh(f"git cat-file -e {source_commit}^{{commit}}", timeout=60.0)
            if rc == 0:
                raise RuntimeError(f"prepared image exposes future source commit: {source_commit}")
            future = "pruned"
        return {"base_commit": base_commit, "source_commit": source_commit or "", "future_history": future}

    def snapshot_digest(self, base_commit: Optional[str] = None) -> str:
        """Hash the task patch and untracked contents without walking the full image.

        The baseline defaults to `self.base_commit`; there is deliberately no literal
        fallback commit, because silently hashing against another row's baseline would
        make every snapshot digest -- and therefore every staleness decision -- wrong
        without failing.
        """
        base_commit = base_commit or self.base_commit
        if not base_commit:
            raise RuntimeError(
                "snapshot_digest needs a base commit: construct Container(base_commit=...) "
                "from the instance manifest"
            )
        script = f"""
set -o pipefail
git diff --binary {base_commit}
git status --porcelain=v1 --untracked-files=all
for p in $(git ls-files --others --exclude-standard); do
  printf '%s\\0' "$p"
  sha256sum "$p" 2>/dev/null || true
done
"""
        rc, out, err = self.sh(script, timeout=300.0)
        if rc != 0:
            raise RuntimeError(f"snapshot digest failed: {err.strip()}")
        return hashlib.sha256(out.encode()).hexdigest()[:32]

    def file_list(self, root: str = "/app") -> List[Dict[str, str]]:
        rc, out, _err = self.sh(
            "git status --porcelain=v1 --untracked-files=all | head -200", timeout=120.0, workdir=root
        )
        rows: List[Dict[str, str]] = []
        if rc == 0:
            for line in out.splitlines():
                if len(line) >= 4:
                    rows.append({"path": f"/app/{line[3:]}", "status": line[:2]})
        return rows


def run_ephemeral(
    image: str,
    script: str,
    mounts: Optional[List[Mount]] = None,
    network: str = "none",
    memory: str = "4g",
    cpus: str = "2",
    timeout: float = 1800.0,
) -> Tuple[int, str, str]:
    cmd = [
        "docker", "run", "--rm", "--memory", memory, "--cpus", cpus, *_cpuset(cpus),
        "--network", network, "--entrypoint", "/bin/bash",
    ]
    for mount in mounts or []:
        cmd += mount.arg()
    cmd += [image, "-c", script]
    return _run(cmd, timeout=timeout)


def remove_image(tag: str, force: bool = True) -> None:
    cmd = ["docker", "rmi"] + (["-f"] if force else []) + [tag]
    _run(cmd, timeout=900.0)


def image_exists(ref: str) -> bool:
    rc, _out, _err = _run(["docker", "image", "inspect", ref], timeout=120.0)
    return rc == 0


def pull_image(ref: str, attempts: int = 4) -> None:
    failures = []
    for attempt in range(1, attempts + 1):
        rc, out, err = _run(["docker", "pull", ref], timeout=3600.0)
        if rc == 0:
            return
        failures.append(f"attempt {attempt}: {(err or out).strip()[-3000:]}")
        if attempt < attempts:
            time.sleep(min(2 ** attempt, 20))
    raise RuntimeError(f"docker pull failed for {ref}: {' | '.join(failures)[-6000:]}")


def image_identity(ref: str) -> Dict[str, str]:
    rc, out, err = _run(["docker", "image", "inspect", ref], timeout=120.0)
    if rc != 0:
        raise RuntimeError(f"docker inspect failed for {ref}: {err.strip()}")
    try:
        records = json.loads(out)
        record = records[0]
        raw_digests = record.get("RepoDigests") or []
        digests = ",".join(str(value) for value in raw_digests) \
            if isinstance(raw_digests, list) else ""
        return {"image_id": str(record.get("Id") or ""), "repo_digests": digests}
    except (json.JSONDecodeError, IndexError, TypeError, AttributeError) as exc:
        raise RuntimeError(f"could not parse docker image identity for {ref}: {exc}") from exc


def params_digest(payload: Dict) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()[:12]
