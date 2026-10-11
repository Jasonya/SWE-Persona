"""Typed Arrow schema. JSON fields are validated profile-specific contracts."""
import base64
import hashlib
import json
from pathlib import PurePosixPath

import pyarrow as pa

SCHEMA_VERSION = "1.0.0"
PROFILES = ("terminal-bench-3", "swe-bench-pro-mini", "swe-bench-pro-shell", "swe-bench-live", "swe-together", "deep-swe")
S, I, F, B = pa.string(), pa.int32(), pa.float64(), pa.bool_()
def struct(**fields): return pa.struct(list(fields.items()))
RESOURCE = struct(cpus=I, memory_mb=I, storage_mb=I, gpus=I, gpu_types=pa.list_(S))
ENV = struct(image=S, platform=S, workdir=S, resources=RESOURCE, network_mode=S,
             build_context=S, harbor_config_json=S)
SCHEMA = pa.schema([
    ("schema_version", S), ("task_id", S), ("task_revision", S),
    ("source_benchmark", S), ("source_subset", S),
    ("source", struct(task_id=S, instance_id=S, repository=S, revision=S,
                      row_index=I, upstream_uri=S, license_asset_ids=pa.list_(S))),
    ("initial_message", S), ("agent_context", S),
    ("native_task", struct(instruction=S, facts=pa.list_(struct(fact_id=S, text=S)), config_json=S)),
    ("persona", struct(persona_id=S, renderer_id=S, profile_json=S, interaction_contract_json=S)),
    ("requirements", pa.list_(struct(requirement_id=S, disclosure=S, required=B,
        depends_on=pa.list_(S), schedule=struct(trigger_event=S, guard_id=S, earliest_turn=I, deadline_turn=I, priority=I),
        rubric_json=S, checker_id=S, checker_config_json=S, runtime_config_json=S))),
    ("interaction", struct(profile_id=S, simulator_prompt_id=S, judge_prompt_id=S,
        opening_mode=S, progression=S, evidence_policy_id=S, config_json=S)),
    ("environment", struct(agent=ENV, verifier=ENV, verifier_mode=S, expected_base_commit=S)),
    ("budget", struct(episode_timeout_sec=F, max_user_turns=I, max_agent_queries_per_turn=I,
        max_simulator_decisions=I, verifier_timeout_sec=F, timing_policy_id=S)),
    ("evaluation", struct(native_evaluator_id=S, persona_evaluator_id=S, aggregation_policy_id=S, config_json=S)),
    ("artifacts", pa.list_(struct(source=S, destination=S, service=S, required=B, exclude=pa.list_(S)))),
    ("assets", pa.list_(struct(asset_id=S, path=S, purpose=S, delivery_stage=S,
        executable=B, sha256=S, inline_bytes=pa.large_binary(), uri=S))),
    ("content_sha256", S),
], metadata={b"schema_version": b"1.0.0", b"description": b"One immutable task-persona pair per row; historical trajectories excluded."})

def canonical(value):
    def default(x):
        if isinstance(x, bytes): return {"$bytes_sha256": hashlib.sha256(x).hexdigest(), "$bytes_length": len(x)}
        raise TypeError(type(x).__name__)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=default)

def content_hash(row):
    return hashlib.sha256(canonical({k: v for k, v in row.items() if k != "content_sha256"}).encode()).hexdigest()

def safe_path(value):
    p = PurePosixPath(value)
    if not value or p.is_absolute() or ".." in p.parts or "\\" in value:
        raise ValueError(f"Unsafe asset path: {value!r}")
    return p

def validate_row(row):
    if row["schema_version"] != SCHEMA_VERSION: raise ValueError("Unsupported schema version")
    if row["interaction"]["profile_id"] not in PROFILES: raise ValueError("Unsupported interaction profile")
    if not row["task_id"] or not row["source_benchmark"] or not row["initial_message"].strip():
        raise ValueError("Missing task identity, source benchmark or initial message")
    if content_hash(row) != row["content_sha256"]: raise ValueError("Row content hash mismatch: " + row["task_id"])
    seen = set()
    for a in row["assets"]:
        safe_path(a["path"])
        if a["path"] in seen: raise ValueError("Duplicate asset path")
        seen.add(a["path"])
        if (a["inline_bytes"] is None) == (a["uri"] is None): raise ValueError("Asset needs exactly one payload source")
        if a["inline_bytes"] is not None and hashlib.sha256(a["inline_bytes"]).hexdigest() != a["sha256"]:
            raise ValueError("Asset hash mismatch: " + a["path"])
        if any(p in {"receipts", "runs", "trajectories", "__pycache__"} for p in safe_path(a["path"]).parts):
            raise ValueError("Historical or generated data in runtime assets")
    ids = [x["requirement_id"] for x in row["requirements"]]
    if len(ids) != len(set(ids)) or not ids: raise ValueError("Empty or duplicate requirement IDs")
    visited = set()
    for req in row["requirements"]:
        if not req["disclosure"].strip(): raise ValueError("Empty requirement")
        if not set(req["depends_on"]) <= visited: raise ValueError("Unordered/cyclic requirement dependency")
        visited.add(req["requirement_id"])
        for k in ["rubric_json", "checker_config_json", "runtime_config_json"]: json.loads(req[k])
    if "environment/Dockerfile" not in seen and "environment/docker-compose.yaml" not in seen and not row["environment"]["agent"]["image"]:
        raise ValueError("No executable agent environment")
    if "tests/test.sh" not in seen: raise ValueError("Missing original test entry point")
    if row["budget"]["episode_timeout_sec"] <= 0: raise ValueError("Invalid time budget")
    return row
