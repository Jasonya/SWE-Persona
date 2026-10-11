"""Freeze one upstream task for environment validation, never for agent input."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from datasets import load_dataset
from huggingface_hub import HfApi

ROOT = Path(__file__).resolve().parents[2]
DATASET = "SWE-bench-Live/MultiLang"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", default="c")
    parser.add_argument("--instance-id", default="rsyslog__rsyslog-6047")
    parser.add_argument("--revision", help="Hugging Face dataset commit; defaults to current HEAD")
    args = parser.parse_args()
    revision = args.revision or HfApi().dataset_info(DATASET).sha
    rows = load_dataset(DATASET, split=args.split, revision=revision,
                        cache_dir=str(ROOT / ".local" / "hf-cache"))
    matches = [row for row in rows if row["instance_id"] == args.instance_id]
    if len(matches) != 1:
        raise SystemExit(f"Expected one {args.instance_id} in {args.split}@{revision}; found {len(matches)}")
    out = ROOT / ".local" / "multilang-smoke"
    out.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(matches[0], ensure_ascii=False) + "\n").encode("utf-8")
    (out / "task.private.jsonl").write_bytes(payload)
    upstream = ROOT / ".local" / "SWE-bench-Live"
    def sha(path):
        return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
    manifest = {
        "purpose": "gold-patch environment validation only; contains hidden evaluation material",
        "dataset": DATASET, "dataset_revision": revision, "split": args.split,
        "instance_id": args.instance_id, "task_sha256": hashlib.sha256(payload).hexdigest(),
        "upstream_commit": sha(upstream), "repolaunch_commit": sha(upstream / "launch"),
        "platform": "linux", "workers": 1,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    print(f"Private evaluator input: {out / 'task.private.jsonl'}")


if __name__ == "__main__":
    main()
