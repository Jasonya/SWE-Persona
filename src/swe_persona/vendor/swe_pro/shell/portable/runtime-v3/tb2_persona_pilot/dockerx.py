#!/usr/bin/env python3
"""Thin docker helpers: the episode container, immutable snapshots, and the
fresh containers every checker and the outcome verifier run in."""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


def _run(cmd: List[str], timeout: float = 600.0, input_text: Optional[str] = None) -> Tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input_text)
    return proc.returncode, proc.stdout, proc.stderr


@dataclass
class Mount:
    source: str
    target: str
    read_only: bool = True
    volume: bool = False

    def arg(self) -> List[str]:
        ro = ":ro" if self.read_only else ""
        return ["-v", f"{self.source}:{self.target}{ro}"]


@dataclass
class Container:
    name: str
    image: str
    mounts: List[Mount] = field(default_factory=list)
    memory: str = "2g"
    cpus: str = "1"
    network: str = "none"

    def start(self) -> str:
        _run(["docker", "rm", "-f", self.name])
        cmd = ["docker", "run", "-d", "--name", self.name, "--memory", self.memory, "--cpus", self.cpus,
               "--network", self.network]
        for m in self.mounts:
            cmd += m.arg()
        cmd += [self.image, "sleep", "infinity"]
        rc, out, err = _run(cmd)
        if rc != 0:
            raise RuntimeError(f"docker run failed: {err.strip()}")
        return out.strip()

    def sh(self, script: str, timeout: float = 600.0, workdir: str = "/app") -> Tuple[int, str, str]:
        return _run(["docker", "exec", "-w", workdir, self.name, "bash", "-lc", script], timeout=timeout)

    def commit(self, tag: str) -> str:
        rc, out, err = _run(["docker", "commit", self.name, tag], timeout=900.0)
        if rc != 0:
            raise RuntimeError(f"docker commit failed: {err.strip()}")
        return tag

    def remove(self) -> None:
        _run(["docker", "rm", "-f", self.name])

    def snapshot_digest(self, roots: Tuple[str, ...] = ("/app",), exclude: Tuple[str, ...] = ("/app/logs", "/app/logs_bulk")) -> str:
        """Content hash of the mutable workspace: agent-written files only."""
        prune = " ".join(f"-path {shlex.quote(p)} -prune -o" for p in exclude)
        script = (
            f"find {' '.join(roots)} {prune} -type f -print0 2>/dev/null | sort -z | "
            "xargs -0 -r sha256sum 2>/dev/null | sha256sum | cut -d' ' -f1"
        )
        rc, out, _ = self.sh(script, timeout=300.0)
        return out.strip()[:32] if rc == 0 and out.strip() else "unknown"

    def file_list(self, root: str = "/app", exclude: Tuple[str, ...] = ("/app/logs", "/app/logs_bulk")) -> List[Dict[str, str]]:
        prune = " ".join(f"-path {shlex.quote(p)} -prune -o" for p in exclude)
        rc, out, _ = self.sh(
            f"find {root} {prune} -type f -printf '%p\\t%s\\n' 2>/dev/null | sort | head -200", timeout=120.0
        )
        rows = []
        for line in (out or "").splitlines():
            if "\t" in line:
                path, size = line.rsplit("\t", 1)
                rows.append({"path": path, "size_bytes": size})
        return rows


def run_ephemeral(
    image: str,
    script: str,
    mounts: Optional[List[Mount]] = None,
    network: str = "none",
    memory: str = "2g",
    cpus: str = "1",
    timeout: float = 1800.0,
) -> Tuple[int, str, str]:
    """One-shot container from an immutable snapshot image (checkers, verifier)."""
    cmd = ["docker", "run", "--rm", "--memory", memory, "--cpus", cpus, "--network", network]
    for m in mounts or []:
        cmd += m.arg()
    cmd += [image, "bash", "-lc", script]
    return _run(cmd, timeout=timeout)


def remove_image(tag: str) -> None:
    _run(["docker", "rmi", "-f", tag])


def params_digest(payload: Dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
