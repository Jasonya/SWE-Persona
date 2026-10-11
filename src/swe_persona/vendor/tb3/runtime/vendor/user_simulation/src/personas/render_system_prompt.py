#!/usr/bin/env python3
"""render_system_prompt.py — render a persona into the user-simulator SYSTEM PROMPT.

This is the bridge between the persona machinery and Harbor: the persona becomes the
`--append-system-prompt` of the claude-code agent that plays the simulated USER (the SUT
is the Copilot CLI the user drives). It replaces the old inject_persona.py prose path,
which baked the persona into instruction.md (a user prompt) instead of a system prompt.

Pick the persona one of three ways:
  --db personas.json --id persona-00007    # one entry from a generated database
  --db persona/persona_data/generated/...  # a directory of accepted-*.jsonl.gz shards
  --persona profile.json                    # a single persona JSON (rich schema)
  --generate --seed 7                        # sample a fresh persona from the schema

Optional:
  --scenario terminal|vscode                 # include that scenario's traits (default: none)

Prints the system prompt to stdout (nothing else), so bash can capture it directly:
  SP="$(python -m src.personas.render_system_prompt --db personas.json --id persona-00007)"
"""
from __future__ import annotations

import argparse
import gzip
import json
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
from src.personas.persona import Persona  # noqa: E402


def _persona_id(d: dict) -> str | None:
    return d.get("basic_information", {}).get("id") or d.get("id")


def _is_summary(d: dict) -> bool:
    """A summarized persona (from src.personas.summarize) is just {basic_information.id,
    summary}; the `summary` string is ALREADY a ready-to-use system prompt."""
    return isinstance(d.get("summary"), str) and d["summary"].strip() != ""


def _as_persona_or_summary(d: dict):
    return d["summary"] if _is_summary(d) else Persona.from_dict(d)


def _open_records_file(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("rt", encoding="utf-8")


def _iter_jsonl(path: Path):
    with _open_records_file(path) as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _jsonl_shards(db_path: Path) -> list[tuple[Path, int | None]]:
    if db_path.is_file() and (db_path.name.endswith(".jsonl") or db_path.name.endswith(".jsonl.gz")):
        return [(db_path, None)]
    if not db_path.is_dir():
        return []

    manifest = db_path / "manifest.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            shards = []
            for item in data.get("shards", []):
                p = db_path / item["path"]
                if p.name.endswith(".gz") and p.with_suffix("").exists():
                    p = p.with_suffix("")
                if p.exists():
                    shards.append((p, int(item["rows"])))
            if shards:
                return shards
        except Exception:
            pass

    files = sorted([*db_path.glob("*.jsonl"), *db_path.glob("*.jsonl.gz")])
    return [(p, None) for p in files]


def _iter_db_records(db_path: Path):
    shards = _jsonl_shards(db_path)
    if shards:
        for path, _rows in shards:
            yield from _iter_jsonl(path)
        return

    raw = json.loads(db_path.read_text(encoding="utf-8"))
    entries = raw.get("personas", raw if isinstance(raw, list) else [])
    yield from entries


def _record_at_index_from_shards(shards: list[tuple[Path, int | None]], index: int) -> dict | None:
    remaining = index
    for path, rows in shards:
        if rows is not None and remaining >= rows:
            remaining -= rows
            continue
        for i, record in enumerate(_iter_jsonl(path)):
            if i == remaining:
                return record
        return None
    return None


def _select_random_record(db_path: Path, seed: int | None) -> dict:
    """Pick one persona without loading million-row gzip shards into memory."""
    shards = _jsonl_shards(db_path)
    rng = random.Random(seed)
    if shards and all(rows is not None for _p, rows in shards):
        total = sum(rows or 0 for _p, rows in shards)
        if total <= 0:
            raise SystemExit(f"{db_path} has no personas")
        record = _record_at_index_from_shards(shards, rng.randrange(total))
        if record is None:
            raise SystemExit(f"failed to select a random persona from {db_path}")
        return record

    selected = None
    for i, record in enumerate(_iter_db_records(db_path), start=1):
        if rng.randrange(i) == 0:
            selected = record
    if selected is None:
        raise SystemExit(f"{db_path} has no personas")
    return selected


def _load_from_db_record(db_path: Path, pid: str) -> dict:
    for d in _iter_db_records(db_path):
        if _persona_id(d) == pid:
            return d
    raise SystemExit(f"persona id {pid!r} not found in {db_path}")


def _load_from_db(db_path: Path, pid: str):
    """Returns (persona_or_summary_str, raw_record)."""
    record = _load_from_db_record(db_path, pid)
    return _as_persona_or_summary(record), record


def _load_random(db_path: Path, seed: int | None):
    """Pick any persona from the DB. Returns (persona_or_summary_str, raw_record)."""
    record = _select_random_record(db_path, seed)
    return _as_persona_or_summary(record), record


def _load_single(path: Path) -> Persona:
    d = json.loads(path.read_text())
    if "basic_information" not in d:
        raise SystemExit(
            f"{path} is a flat/legacy profile (keys: {list(d)}); the system-prompt path needs a "
            "rich-schema persona. Use --db/--id from a generated database, or --generate.")
    return Persona.from_dict(d)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, help="persona database JSON (from PersonaGenerator.save_database)")
    ap.add_argument("--id", help="persona id to select from --db (e.g. persona-00007)")
    ap.add_argument("--random", action="store_true",
                    help="with --db: pick ANY persona from the database (--seed for reproducibility). "
                         "The chosen id is printed to stderr so the run is traceable.")
    ap.add_argument("--persona", type=Path, help="a single rich-schema persona JSON")
    ap.add_argument("--generate", action="store_true", help="sample a fresh persona from the schema")
    ap.add_argument("--seed", type=int, default=0, help="seed/index for --generate")
    ap.add_argument("--scenario", choices=["terminal", "vscode"], default=None)
    ap.add_argument("--format", choices=["prompt", "json", "id"], default="prompt",
                    help="what to print: rendered prompt, raw selected persona JSON, or selected id")
    ap.add_argument("--shell-quote", action="store_true",
                    help="shlex.quote the output so it can be embedded verbatim in a shell "
                         "command string (Harbor's build_cli_flags does NOT quote flag values, "
                         "so a multi-word/multi-line system prompt must arrive pre-quoted).")
    args = ap.parse_args(argv)

    if args.db:
        if args.random:
            persona, record = _load_random(args.db, args.seed if args.seed else None)
            chosen = _persona_id(record) or "?"
            print(f"[render] picked persona {chosen} from {args.db}", file=sys.stderr)
        elif args.id:
            persona, record = _load_from_db(args.db, args.id)
        else:
            raise SystemExit("--db requires either --id or --random")
    elif args.persona:
        record = json.loads(args.persona.read_text(encoding="utf-8"))
        persona = _load_single(args.persona)
    elif args.generate:
        from src.personas.generator import PersonaGenerator
        persona = PersonaGenerator(seed=args.seed).generate_one(args.seed)
        record = persona.to_dict()
    else:
        raise SystemExit("pick one of: --db/--id, --persona, or --generate")

    if args.format == "json":
        sys.stdout.write(json.dumps(record, indent=2))
        return 0
    if args.format == "id":
        sys.stdout.write(_persona_id(record) or "")
        return 0

    # A summarized persona is already a system-prompt string; a rich Persona renders one.
    prompt = persona if isinstance(persona, str) else persona.to_system_prompt(scenario=args.scenario)
    if args.shell_quote:
        import shlex
        prompt = shlex.quote(prompt)
    sys.stdout.write(prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
