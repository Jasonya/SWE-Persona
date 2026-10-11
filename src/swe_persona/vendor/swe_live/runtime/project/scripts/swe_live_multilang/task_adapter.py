"""Explicit public/private boundary for SWE-bench-Live MultiLang tasks."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read_task(path):
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != 1:
        raise ValueError("The pilot accepts exactly one task, not a full dataset")
    task = rows[0]
    for field in ("instance_id", "problem_statement", "base_commit", "docker_image"):
        if not isinstance(task.get(field), str) or not task[field].strip():
            raise ValueError(f"Missing required field: {field}")
    return task


def public_view(task):
    # Never copy the full row and remove a blacklist: unknown future columns
    # could include solutions. Only these fields may cross the boundary.
    return {
        "instance_id": task["instance_id"],
        "problem_statement": task["problem_statement"],
        "environment": {"platform": "linux", "workdir": "/testbed", "cpus": 4,
                        "memory": "16g", "network": "none"},
    }


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def prepare(private_path, output):
    task = read_task(private_path)
    public = public_view(task)
    pool = json.loads((ROOT / "human-interactive-coding-personas.v3.json").read_text(encoding="utf-8"))
    persona = next(p for p in pool["personas"] if p["id"] == "manager_no_code")
    template = next(t for t in persona["attributes"]["evolved_requirements"]
                    if t["id"] == "manager_conclusion_only_delivery")
    # This slot-free conversational requirement has no made-up business facts.
    # A real judge calibration is still required; preparation does not mark met.
    candidate = {
        "persona_id": persona["id"], "template_id": template["id"],
        "statement": template["user_requirement"], "bindings": {},
        "rubric": template["satisfaction"], "activation_status": "pending_validation",
        "satisfaction_status": "unknown", "source": "human-interactive-coding-personas.v3.json",
        "public_contract_digest": digest(public), "persona_digest": digest(persona),
        "schedule": {"earliest_turn": 2, "deadline_turn": 2, "required": True},
        "validation_required": "Grounded judge calibration on positive, negative and unsupported evidence arms",
        "scope": "One conversational requirement for the first pilot; not the full persona coverage experiment",
    }
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    for name, value in (("task.public.json", public), ("persona.candidate.json", candidate)):
        (output / name).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return public, candidate


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--task", type=Path, default=ROOT / ".local/multilang-smoke/task.private.jsonl")
    p.add_argument("--output", type=Path, default=ROOT / ".local/multilang-pilot/prepared")
    args = p.parse_args()
    public, candidate = prepare(args.task, args.output)
    print(json.dumps({"instance_id": public["instance_id"], "public_contract_digest": digest(public),
                      "candidate_status": candidate["activation_status"], "output": str(args.output)}, indent=2))
