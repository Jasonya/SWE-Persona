"""Run the upstream evaluator with host-path and terminal-output adapters.

The upstream LinuxRuntime.apply_patch uses os.path.join for a container path,
which produces a backslash on Windows. Keep host paths native, container paths
POSIX, and patch files UTF-8/LF. Fail closed if either patch cannot be applied.
Collect terminal output in linear time; retain the complete official test log.
"""
import os
import argparse
from pathlib import Path, PurePosixPath
import runpy
import shlex
import sys
import uuid
import hashlib
import json
from protected_patch import paths as patch_paths, project

from runtime_output import make_output_reader
from evaluation_runtime import Timeline, install_runtime_adapters, label_created_containers, supervise

ROOT = Path(__file__).resolve().parents[2]
UPSTREAM = ROOT / ".local" / "SWE-bench-Live"


def apply_patch(self, patch, verbose=False):
    if os.environ.get('PERSONACODE_PROTECT_OFFICIAL_TESTS') == '1':
        count = getattr(self, '_protected_patch_count', 0)
        self._protected_patch_count = count + 1
        if count == 0:
            self._official_test_paths = patch_paths(patch)
        elif count == 1:
            original = patch
            patch, excluded = project(patch, self._official_test_paths)
            audit = Path(os.environ['PERSONACODE_PROJECTION_DIR'])
            audit.mkdir(parents=True, exist_ok=True)
            key = hashlib.sha256(original.encode()).hexdigest()
            (audit / (key + '.json')).write_text(json.dumps({'policy': 'official-test-files-authoritative-v1',
                'original_sha256': key, 'projected_sha256': hashlib.sha256(patch.encode()).hexdigest(),
                'excluded_paths': excluded, 'official_paths': sorted(self._official_test_paths)}, indent=2), encoding='utf-8')
            (audit / (key + '.diff')).write_text(patch, encoding='utf-8', newline='\n')
            if not patch.strip():
                print('Candidate has no changes outside official test files; evaluate unchanged implementation.', flush=True)
                return True
    filename = f"{uuid.uuid4()}.diff"
    host_path = Path(self.mnt_host) / filename
    host_path.write_text(patch, encoding="utf-8", newline="\n")
    container_path = shlex.quote(str(PurePosixPath(self.mnt_container) / filename))
    try:
        result = self.send_command(f"git apply --reject --whitespace=nowarn {container_path}")
    finally:
        host_path.unlink(missing_ok=True)
    if int(result.metadata.exit_code) != 0:
        # Upstream evaluation ignores a False return and otherwise keeps testing.
        self.cleanup()
        raise RuntimeError(f"Patch application failed; evaluation invalid: {result.output}")
    print("Patch applied successfully (POSIX container path, UTF-8/LF).", flush=True)
    return True


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--wall-timeout-seconds", type=float, default=0)
    parser.add_argument("--command-timeout-seconds", type=float, default=9000)
    parser.add_argument("--_worker", action="store_true")
    parser.add_argument('--protect-official-tests', action='store_true')
    args, upstream_args = parser.parse_known_args()
    if args.wall_timeout_seconds < 0 or args.command_timeout_seconds <= 0:
        parser.error("Wall timeout must be nonnegative; command timeout must be positive (seconds)")
    # Resolve host paths BEFORE the upstream changes cwd to its own checkout.
    for flag in ("--dataset", "--patch_dir", "--output_dir"):
        if flag in upstream_args:
            index = upstream_args.index(flag) + 1
            value = upstream_args[index]
            if flag == "--output_dir" or (value != "gold" and Path(value).exists()):
                upstream_args[index] = str(Path(value).resolve())
    output = Path(upstream_args[upstream_args.index("--output_dir") + 1]) if "--output_dir" in upstream_args else ROOT / ".local/evaluation"
    if "--output_dir" not in upstream_args:
        upstream_args += ["--output_dir", str(output)]
    if not args._worker:
        if args.protect_official_tests:
            os.environ['PERSONACODE_PROTECT_OFFICIAL_TESTS'] = '1'
            os.environ['PERSONACODE_PROJECTION_DIR'] = str(output.resolve() / 'patch-projection')
        command = [sys.executable, "-u", str(Path(__file__).resolve()), "--_worker",
                   "--command-timeout-seconds", str(args.command_timeout_seconds), *upstream_args]
        return supervise(command, output, args.wall_timeout_seconds)
    owner = os.environ.get("PERSONACODE_EVAL_OWNER")
    if not owner:
        raise RuntimeError("Evaluator worker must be launched by its supervisor")
    label_created_containers(owner)
    os.chdir(UPSTREAM)
    sys.path[:0] = [str(UPSTREAM), str(UPSTREAM / "launch")]
    from launch.core.platforms.linux import LinuxRuntime
    from launch.core.platforms.base import CmdOutputMetadata, ANSI_ESCAPE
    LinuxRuntime.apply_patch = apply_patch
    LinuxRuntime._read_raw_output = make_output_reader(CmdOutputMetadata, ANSI_ESCAPE)
    runtimes = install_runtime_adapters(LinuxRuntime, args.command_timeout_seconds, Timeline(output / "runtime-events.jsonl"))
    print(f"Runtime adapter: seconds-v2; command timeout={args.command_timeout_seconds:g}s; full logs retained; official commands unchanged.", flush=True)
    sys.argv = [sys.argv[0], *upstream_args]
    try:
        runpy.run_module("evaluation.evaluation", run_name="__main__")
    finally:
        for runtime in runtimes:
            runtime.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
