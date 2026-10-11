#!/usr/bin/env python3
"""Frozen, multi-instance adapter for the SWE-bench Pro test split.

Only :meth:`TaskManifest.public_view` may be rendered into agent/simulator
prompts. Gold patches, test patches and required-test labels remain checker
assets mounted into isolated verifier containers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

DATASET_ID = "ScaleAI/SWE-bench_Pro"
DATASET_REVISION = "7ab5114912baf22bb098818e604c02fe7ad2c11f"
DATASET_SPLIT = "test"
HARNESS_REVISION = "ca10a60a5fcae51e6948ffe1485d4153d421e6c5"
IMAGE_REPOSITORY = "docker.io/jefzda/sweap-images"
DEFAULT_SAMPLE_SEED = 20260916
DEFAULT_SAMPLE_SIZE = 20
EXPECTED_ONE_BASED_ROWS = [
    44, 112, 164, 248, 250, 380, 392, 450, 514, 518,
    548, 576, 588, 606, 618, 628, 660, 678, 680, 700,
]

HERE = Path(__file__).resolve().parent
DEFAULT_HARNESS_CACHE = Path(os.environ.get("PCB_OFFICIAL_ASSET_CACHE", Path.home() / ".cache/personacodebench/official")) / HARNESS_REVISION

# The official task text.  `scaleapi/SWE-bench_Pro-os@helper_code/create_problem_statement.py`
# renders exactly this, and `helper_code/generate_sweagent_instances.py` feeds the result to the
# scaffold as `problem_statement`.  Reproduced verbatim -- including the raw (unstripped) dataset
# fields -- so `native_prompt_digest()` is comparable with the upstream string.
NATIVE_PROBLEM_STATEMENT_TEMPLATE = """{problem_statement}

Requirements:
{requirements}

New interfaces introduced:
{interface}"""

# The official scaffolds state the test-file policy in the instance template, not in the task text:
# `mini-swe-agent@src/minisweagent/config/swebp.yaml` and `SWE-agent@config/{default,tool_use}.yaml`
# both say "you DON'T have to modify the testing logic or any of the tests in any way" and ask for
# "minimal changes to non-tests files".  Upstream phrases that as the requester speaking; here the
# requester is a persona whose opening message is deliberately vague, so the same content is stated
# as an environment fact to avoid putting words the persona never said into its mouth.
NATIVE_TEST_POLICY = (
    "The maintainers have already made every test-file change this task needs. Do not modify the "
    "testing logic or any of the existing tests in any way. Make the minimal changes to non-test "
    "files that satisfy the specification."
)

# The instruction block the official scaffolds hand the coding agent, verbatim from
# `mini-swe-agent@src/minisweagent/config/swebp.yaml` (`agent.instance_template`); SWE-agent's
# `config/{default,tool_use}.yaml` carry the same wording.  Two upstream quirks are preserved
# deliberately: the repository is called "python" for every language, and the reproduce step
# names `python <filename.py>` even for Go/JS rows.
#
# What is left out is the template's trailing `## Important Rules` / `## Formatting your response`
# / `## Useful command examples` sections.  Those describe mini-swe-agent's one-bash-block action
# protocol, which this pilot's agent does not use, so showing them to a persona would teach it a
# false picture of how its agent is allowed to answer.  Nothing in them constrains the task.
NATIVE_AGENT_PROMPT_TEMPLATE = """<uploaded_files>
{working_dir}
</uploaded_files>
I've uploaded a python code repository in the directory {working_dir}. Consider the following PR description:

<pr_description>
{problem_statement}
</pr_description>

Can you help me implement the necessary changes to the repository so that the requirements specified in the <pr_description> are met?
I've already taken care of all changes to any of the test files described in the <pr_description>. This means you DON'T have to modify the testing logic or any of the tests in any way!
Your task is to make the minimal changes to non-tests files in the {working_dir} directory to ensure the <pr_description> is satisfied.
Follow these steps to resolve the issue:
1. As a first step, it might be a good idea to find and read code relevant to the <pr_description>
2. Create a script to reproduce the error and execute it with `python <filename.py>` using the bash tool, to confirm the error
3. Edit the sourcecode of the repo to resolve the issue
4. Rerun your reproduce script and confirm that the error is fixed!
5. Think about edgecases and make sure your fix handles them as well
Your thinking should be thorough and so it's fine if it's very long."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def decode_public_field(value: Any) -> str:
    """Unwrap the dataset's inconsistent extra JSON-string layer.

    The odd-row public inventory decodes this layer before hashing. Runtime
    binding identity checks use that same public view, while native prompt
    rendering deliberately keeps the raw upstream representation.
    """
    raw = str(value)
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        if isinstance(decoded, str):
            return decoded
    return raw


@lru_cache(maxsize=1)
def load_rows():
    """Load the immutable HF revision without silently following ``main``.

    ``datasets.load_dataset`` takes a write lock even when its complete Arrow
    cache is present.  Evaluation hosts commonly expose that cache read-only,
    so prefer the exact revision's Arrow/Parquet artifact and fall back to a
    workspace-local cache only when it is absent.
    """
    from datasets import Dataset, load_dataset

    cache_root = Path(
        os.environ.get("HF_DATASETS_CACHE", Path.home() / ".cache/huggingface/datasets")
    )
    arrow = (
        cache_root / "ScaleAI___swe-bench_pro" / "default" / "0.0.0"
        / DATASET_REVISION / "swe-bench_pro-test.arrow"
    )
    if arrow.is_file():
        return Dataset.from_file(str(arrow))

    try:
        from huggingface_hub import try_to_load_from_cache

        parquet = try_to_load_from_cache(
            DATASET_ID, "data/test-00000-of-00001.parquet",
            revision=DATASET_REVISION, repo_type="dataset",
        )
        if isinstance(parquet, str) and Path(parquet).is_file():
            import pyarrow.parquet as pq

            return Dataset(pq.read_table(parquet))
    except (ImportError, OSError):
        pass

    local_cache = Path(os.environ.get("HF_DATASETS_CACHE", Path.home() / ".cache/huggingface/datasets"))
    local_cache.mkdir(parents=True, exist_ok=True)
    return load_dataset(
        DATASET_ID,
        revision=DATASET_REVISION,
        split=DATASET_SPLIT,
        cache_dir=str(local_cache),
        download_mode="reuse_cache_if_exists",
    )


def sampled_zero_based_indices(
    *, seed: int = DEFAULT_SAMPLE_SEED, count: int = DEFAULT_SAMPLE_SIZE,
) -> List[int]:
    """Sample from human-visible even rows (Python indices 1, 3, ..., 729)."""
    import random

    rows = load_rows()
    eligible = list(range(1, len(rows), 2))
    if count > len(eligible):
        raise ValueError(f"cannot sample {count} rows from {len(eligible)} eligible rows")
    return sorted(random.Random(seed).sample(eligible, count))


def selection_manifest(
    *, seed: int = DEFAULT_SAMPLE_SEED, count: int = DEFAULT_SAMPLE_SIZE,
) -> Dict[str, Any]:
    rows = load_rows()
    selected = sampled_zero_based_indices(seed=seed, count=count)
    return {
        "dataset_id": DATASET_ID,
        "dataset_revision": DATASET_REVISION,
        "split": DATASET_SPLIT,
        "index_convention": "one_based_even_rows",
        "seed": seed,
        "count": count,
        "tasks": [TaskManifest.from_row(dict(rows[index]), index).selection_record() for index in selected],
    }


def _download(url: str, destination: Path, attempts: int = 4) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    last: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "PersonaCodeBench/1.0"})
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - pinned GitHub URL
                payload = response.read()
            if not payload:
                raise RuntimeError(f"empty download from {url}")
            destination.write_bytes(payload)
            return
        except (OSError, urllib.error.URLError, RuntimeError) as exc:
            last = exc
            if attempt < attempts:
                time.sleep(min(2 ** attempt, 10))
    raise RuntimeError(f"could not download {url}: {last}")


def _official_asset(instance_id: str, name: str, cache_root: Path) -> Path:
    if name not in ("run_script.sh", "parser.py"):
        raise ValueError(f"unsupported official harness asset {name!r}")
    path = cache_root / instance_id / name
    if not path.is_file():
        url = (
            "https://raw.githubusercontent.com/scaleapi/SWE-bench_Pro-os/"
            f"{HARNESS_REVISION}/run_scripts/{instance_id}/{name}"
        )
        _download(url, path)
    return path


def _source_commit(before_repo_set_cmd: str) -> Optional[str]:
    """Extract the future test-source commit without executing dataset shell."""
    for line in reversed((before_repo_set_cmd or "").splitlines()):
        match = re.match(r"\s*git\s+checkout\s+([0-9a-f]{40})\s+--(?:\s|$)", line)
        if match:
            return match.group(1)
    return None


@dataclass
class TaskManifest:
    row: Dict[str, Any]
    row_index_zero: int
    dataset: str = DATASET_ID
    dataset_version: str = DATASET_REVISION
    environment: Dict[str, Any] = field(default_factory=dict)
    limits: Dict[str, Any] = field(default_factory=lambda: {"cpus": 2, "memory": "4g", "network": "none"})

    @classmethod
    def from_row(cls, row: Dict[str, Any], row_index_zero: int) -> "TaskManifest":
        required = (
            "repo", "instance_id", "base_commit", "patch", "test_patch", "problem_statement",
            "requirements", "interface", "repo_language", "fail_to_pass", "pass_to_pass",
            "selected_test_files_to_run", "before_repo_set_cmd", "dockerhub_tag",
        )
        missing = [key for key in required if key not in row or row[key] is None]
        if missing:
            raise RuntimeError(f"dataset row {row_index_zero} is missing fields: {missing}")
        return cls(row=row, row_index_zero=row_index_zero)

    @classmethod
    def from_dataset(
        cls,
        *,
        instance_id: Optional[str] = None,
        dataset_index: Optional[int] = None,
        index_base: int = 0,
    ) -> "TaskManifest":
        if (instance_id is None) == (dataset_index is None):
            raise ValueError("provide exactly one of instance_id or dataset_index")
        rows = load_rows()
        if dataset_index is not None:
            if index_base not in (0, 1):
                raise ValueError("index_base must be 0 or 1")
            zero = dataset_index - index_base
            if zero < 0 or zero >= len(rows):
                raise IndexError(f"dataset index {dataset_index} (base {index_base}) is out of range")
            return cls.from_row(dict(rows[zero]), zero)
        for zero, item in enumerate(rows):
            if item["instance_id"] == instance_id:
                return cls.from_row(dict(item), zero)
        raise KeyError(f"instance {instance_id!r} not found in {DATASET_ID}@{DATASET_REVISION}")

    @property
    def task_id(self) -> str:
        return str(self.row["instance_id"])

    @property
    def base_commit(self) -> str:
        return str(self.row["base_commit"])

    @property
    def source_commit(self) -> Optional[str]:
        return _source_commit(str(self.row["before_repo_set_cmd"]))

    @property
    def docker_image(self) -> str:
        return f"{IMAGE_REPOSITORY}:{self.row['dockerhub_tag']}"

    @property
    def instruction(self) -> str:
        return "\n\n".join(
            str(self.row[key]).strip()
            for key in ("problem_statement", "requirements", "interface")
            if str(self.row[key]).strip()
        )

    @property
    def native_problem_statement(self) -> str:
        """The upstream SWE-bench Pro task text, byte-for-byte.

        `instruction` is the same three fields joined without labels and is kept as the
        adapter's rewrite input; this property is what gets rendered into prompts so the
        agent reads the specification in the form the public benchmark uses.
        """
        return NATIVE_PROBLEM_STATEMENT_TEMPLATE.format(
            problem_statement=self.row["problem_statement"],
            requirements=self.row["requirements"],
            interface=self.row["interface"],
        )

    def native_prompt_digest(self) -> str:
        return _sha(self.native_problem_statement)

    @property
    def interface_floor(self) -> str:
        """The `interface` field, decoded for reading.

        The dataset stores this field inconsistently: on 9 of the 20 sampled rows the value
        carries an extra layer of JSON quoting, and on 3 of those the line breaks are literal
        `\\n` escapes rather than newlines.  Upstream pastes the raw value into the prompt, so
        a native agent reads the escapes as-is.  This pilot states the interface in a section
        of its own, which upstream has no counterpart for, so there is no parity to keep here
        and the value is unwrapped instead.  `native_problem_statement` still embeds the raw
        field, which is what `native_prompt_digest()` is taken over.
        """
        return decode_public_field(self.row["interface"])

    @property
    def public_contract(self) -> Dict[str, str]:
        """Canonical decoded public fields used by persona-data bindings."""
        return {
            key: decode_public_field(self.row[key])
            for key in ("problem_statement", "requirements", "interface")
        }

    def native_agent_prompt(self, working_dir: str = "/app") -> str:
        """The whole upstream instruction block, specification included.

        This is what a native SWE-bench Pro run puts in front of the model.  In this pilot the
        agent does not read it -- the persona does, when it writes its opening message -- so the
        simulated human knows the real constraints of the task it is asking for, including that
        the tests are already written and must not be touched.
        """
        return NATIVE_AGENT_PROMPT_TEMPLATE.format(
            working_dir=working_dir, problem_statement=self.native_problem_statement
        )

    @property
    def hidden(self) -> Dict[str, Any]:
        return {
            "gold_patch": self.row["patch"],
            "test_patch": self.row["test_patch"],
            "fail_to_pass": self.row["fail_to_pass"],
            "pass_to_pass": self.row["pass_to_pass"],
            "selected_test_files_to_run": self.row["selected_test_files_to_run"],
        }

    def public_view(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "source": f"{self.dataset}@{self.dataset_version}",
            "dataset_row_zero_based": self.row_index_zero,
            "dataset_row_one_based": self.row_index_zero + 1,
            "repository": self.row["repo"],
            "repo_language": self.row["repo_language"],
            "base_commit": self.base_commit,
            "problem_statement": self.row["problem_statement"],
            "requirements": self.row["requirements"],
            "interface": self.row["interface"],
            "frozen_contract": {
                "repository": self.row["repo"],
                "base_commit": self.base_commit,
                "must_change_repository": True,
                "public_interface_floor": self.row["interface"],
                "final_judgement": "the pinned SWE-bench Pro fail-to-pass and pass-to-pass tests",
            },
            "environment": self.environment,
            "limits": self.limits,
        }

    def frozen_digest(self) -> str:
        payload = json.dumps(self.public_view(), sort_keys=True, ensure_ascii=False)
        return _sha(payload)[:16]

    def field_hashes(self) -> Dict[str, str]:
        return {
            key: _sha(str(self.row[key]))
            for key in (
                "problem_statement", "requirements", "interface", "patch", "test_patch",
                "fail_to_pass", "pass_to_pass", "selected_test_files_to_run", "before_repo_set_cmd",
            )
        }

    def selection_record(self) -> Dict[str, Any]:
        return {
            "row_index_zero": self.row_index_zero,
            "row_index_one": self.row_index_zero + 1,
            "instance_id": self.task_id,
            "repo": self.row["repo"],
            "repo_language": self.row["repo_language"],
            "base_commit": self.base_commit,
            "source_commit": self.source_commit,
            "dockerhub_tag": self.row["dockerhub_tag"],
            "docker_image": self.docker_image,
            "field_hashes": self.field_hashes(),
        }

    def hidden_markers(self) -> List[str]:
        public = self.instruction
        markers: List[str] = []
        for patch_key in ("patch", "test_patch"):
            for line in str(self.row[patch_key]).splitlines():
                if not line.startswith("+") or line.startswith("+++"):
                    continue
                marker = line[1:].strip()
                if len(marker) >= 55 and marker not in public:
                    markers.append(marker)
        return markers[:40]

    def assert_no_leak(self, rendered: str, where: str) -> None:
        checked = rendered
        if where == "simulator observation":
            # Agent messages and their shell stdout are untrusted echoes, not
            # controller disclosures.  A correct agent can independently
            # write a line that also occurs in the gold patch; treating that
            # overlap as a harness leak creates a false infrastructure error.
            # Continue checking every controller-owned observation field.
            try:
                observation = json.loads(rendered)
            except json.JSONDecodeError:
                observation = None
            if isinstance(observation, dict):
                observation.pop("agent_message", None)
                checked = json.dumps(observation, ensure_ascii=False)
        for marker in self.hidden_markers():
            if marker in checked:
                raise AssertionError(f"hidden SWE-bench Pro material leaked into {where}: {marker[:70]!r}")

    def materialize_checker_assets(
        self,
        destination: Path,
        fixtures: Path,
        *,
        harness_cache: Path = DEFAULT_HARNESS_CACHE,
    ) -> Path:
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "test.patch").write_text(str(self.row["test_patch"]), encoding="utf-8")
        (destination / "gold.patch").write_text(str(self.row["patch"]), encoding="utf-8")
        spec = {
            "instance_id": self.task_id,
            "row_index_zero": self.row_index_zero,
            "row_index_one": self.row_index_zero + 1,
            "repo": self.row["repo"],
            "repo_language": self.row["repo_language"],
            "base_commit": self.base_commit,
            "source_commit": self.source_commit,
            "problem_statement": self.row["problem_statement"],
            "requirements": self.row["requirements"],
            "interface": self.row["interface"],
            "fail_to_pass": self.row["fail_to_pass"],
            "pass_to_pass": self.row["pass_to_pass"],
            "selected_test_files_to_run": self.row["selected_test_files_to_run"],
        }
        (destination / "task_spec.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
        for name in ("run_script.sh", "parser.py"):
            source = _official_asset(self.task_id, name, harness_cache)
            shutil.copy2(source, destination / name)
        shutil.copy2(fixtures / "checker_driver.py", destination / "checker_driver.py")
        asset_hashes = {
            name: hashlib.sha256((destination / name).read_bytes()).hexdigest()
            for name in ("run_script.sh", "parser.py", "checker_driver.py", "task_spec.json")
        }
        (destination / "asset_hashes.json").write_text(json.dumps(asset_hashes, indent=2), encoding="utf-8")
        return destination


def build_environment_overlay(manifest: TaskManifest, image_ref: Optional[str] = None) -> Dict[str, Any]:
    return {
        "container_image": image_ref or manifest.docker_image,
        "working_directory": "/app",
        "cpus": manifest.limits["cpus"],
        "memory": manifest.limits["memory"],
        "network": "disabled",
        "runtime": f"{manifest.row['repo_language']} dependencies from the pinned SWE-bench Pro image",
        "permissions": [
            "edit the local repository only",
            "do not clean/reset pre-existing work, commit, push, or open a remote PR",
            "do not modify existing test files or testing logic",
            "no network access",
        ],
        "registered_tools": {
            "/harness/run_script.sh": "official repository test runner; hidden test assets are not mounted here",
        },
        "registered_fixtures": {
            "/app": f"clean {manifest.row['repo']} repository at {manifest.base_commit}",
        },
    }


def assert_expected_sample(records: Sequence[Dict[str, Any]]) -> None:
    actual = [int(item["row_index_one"]) for item in records]
    if actual != EXPECTED_ONE_BASED_ROWS:
        raise AssertionError(f"sample drift: expected {EXPECTED_ONE_BASED_ROWS}, got {actual}")
