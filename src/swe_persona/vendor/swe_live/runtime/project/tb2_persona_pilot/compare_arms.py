#!/usr/bin/env python3
"""Report the arms separately (plan section 6).

Joint success is only ever 1 when the base task, every current evolved
requirement and the validity checks all hold; the interaction judge is reported
next to it, never folded into it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent


def load(run_ids: List[str]) -> List[Dict[str, Any]]:
    out = []
    for rid in run_ids:
        path = HERE / "runs" / rid / "episode.json"
        if path.is_file():
            out.append(json.loads(path.read_text(encoding="utf-8")))
    return out


def row(ep: Dict[str, Any]) -> Dict[str, Any]:
    m = ep.get("metrics", {})
    ev = m.get("evolved_requirement_success", {})
    inter = m.get("interaction_delivery", {})
    cov = m.get("valid_evaluation_coverage", {})
    judge = m.get("interaction_judge") or {}
    models = ep.get("models") or {}
    def short(name: str) -> str:
        return (name or "?").replace("gpt-6-astra", "gpt6").replace("glm-", "glm")
    ref = ep.get("outcome", {}).get("undisclosed_reference_checks") or []
    return {
        "run": ep["run_id"],
        "arm": ep["arm"],
        "base": m.get("base_task_success"),
        "evolved": f"{ev.get('met', 0)}/{ev.get('disclosed', 0)}",
        "unknown": ev.get("unknown"),
        "joint": m.get("joint_success"),
        "turns": inter.get("agent_turns_used"),
        "discl": inter.get("disclosures"),
        "revis": inter.get("revisions"),
        "cited": inter.get("evidence_citations"),
        "claimed_not_met": len(inter.get("claimed_but_not_met") or []),
        "valid": cov.get("episode_valid"),
        "judge": f"{judge.get('total')}/{judge.get('max_total')}" if judge else "-",
        "end": inter.get("end_state"),
        "sec": ep.get("seconds"),
        "agent_m": short(models.get("agent", "gpt6")),
        "sim_m": short(models.get("simulator", "gpt6")),
        "judge_m": short(models.get("rubric_judge") or models.get("judge", "gpt6")),
        "calls": (ep.get("llm") or ep.get("gpt6") or {}).get("calls"),
        "in_tok": (ep.get("llm") or ep.get("gpt6") or {}).get("input_tokens"),
        "undisclosed_ref": ";".join(f"{r['checker'].split('@')[0]}={r['status']}" for r in ref) or "-",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--out", default=str(HERE / "runs" / "arm_comparison.json"))
    args = ap.parse_args()
    rows = [row(ep) for ep in load(args.runs)]
    cols = ["run", "arm", "agent_m", "sim_m", "judge_m", "base", "evolved", "joint", "valid", "turns",
            "discl", "revis", "cited", "claimed_not_met", "judge", "end", "calls", "in_tok", "sec"]
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    print(" | ".join(c.ljust(widths[c]) for c in cols))
    print("-+-".join("-" * widths[c] for c in cols))
    for r in rows:
        print(" | ".join(str(r[c]).ljust(widths[c]) for c in cols))
    Path(args.out).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
