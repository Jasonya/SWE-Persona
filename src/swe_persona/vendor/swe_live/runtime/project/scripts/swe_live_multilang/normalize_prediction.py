"""Recover a patch exported before initial-image untracked files were excluded.

Only removes new-file hunks proven byte-identical to a pre-existing untracked
file in the original image. Never reads gold or hidden tests.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile

from run_baseline import AgentContainer, save
from task_adapter import read_task


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir", type=Path, required=True)
    args = p.parse_args()
    task = read_task(args.run_dir / "task.private.jsonl")
    original = json.loads((args.run_dir / "predictions.json").read_text(encoding="utf-8"))
    patch = original[task["instance_id"]]["model_patch"]
    chunks = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    import docker
    client = docker.from_env()
    runtime = None
    kept, removed = [], []
    try:
        # Use the immutable image ID recorded BEFORE rollout, not a moving tag.
        summary = json.loads((args.run_dir / "summary.json").read_text(encoding="utf-8"))
        runtime = AgentContainer(client, summary["image_id"])
        for chunk in chunks:
            match = re.match(r"diff --git a/(.+) b/(.+)\nnew file mode ", chunk)
            if not match or match[1] != match[2] or '"' in match[1]:
                kept.append(chunk)
                continue
            path = match[1]
            # Only an original UNTRACKED existing file is eligible.
            tracked = runtime.container.exec_run(["git", "ls-files", "--error-unmatch", "--", path], workdir="/testbed")
            exists = runtime.container.exec_run(["test", "-f", path], workdir="/testbed")
            if tracked.exit_code == 0 or exists.exit_code != 0:
                kept.append(chunk)
                continue
            data = chunk.encode("utf-8")
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w") as tar:
                info = tarfile.TarInfo("candidate.diff")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            runtime.container.put_archive("/tmp", archive.getvalue())
            check = runtime.container.exec_run(
                ["git", "apply", "--reverse", "--check", "/tmp/candidate.diff"], workdir="/testbed")
            if check.exit_code == 0:
                removed.append({"path": path, "reason": "New-file patch reverses cleanly against the original untracked image file",
                                "hunk_sha256": hashlib.sha256(data).hexdigest()})
            else:
                kept.append(chunk)
        normalized = "".join(kept)
        save(args.run_dir / "predictions.normalized.json", {task["instance_id"]: {"model_patch": normalized}})
        audit = {"original_patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
                 "normalized_patch_sha256": hashlib.sha256(normalized.encode()).hexdigest(),
                 "removed_unchanged_image_files": removed, "image_id": summary["image_id"],
                 "model_was_not_rerun": True}
        save(args.run_dir / "patch_normalization.json", audit)
        print(json.dumps(audit, indent=2))
    finally:
        if runtime:
            runtime.remove()
        client.close()


if __name__ == "__main__":
    main()
