"""Read-only result collection, shareable plots, and an allowlisted PR review bundle."""
import bootstrap
import argparse
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import shutil
import textwrap

from support import read, save, digest, file_hash
from audit_sample import audit, export_review

os.environ.setdefault("MPLCONFIGDIR", str(bootstrap.ROOT / ".local/matplotlib"))

ARMS = ("original", "persona0", "persona1")
LABELS = ("Original", "Persona 0", "Persona 1")
NAMES = ("single_turn", "manager_no_code", "first_time_pr_contributor")


def optional(path, default=None):
    return read(path) if path.is_file() else ({} if default is None else default)


def collect(study, manifest):
    rows = []
    for task in manifest["tasks"]:
        state = optional(study / (task["key"] + "-state.json"))
        cells = []
        for arm, name in zip(ARMS, NAMES):
            parent = study / "episodes" / task["key"] / arm
            attempts = sorted(p for p in parent.glob("attempt-*") if p.is_dir())
            run = attempts[-1] if attempts else None
            episode = optional(run / "episode.json") if run else {}
            attempt = optional(run / "attempt.json") if run else {}
            metrics = episode.get("metrics", {})
            release = audit(run) if run else {"ready_for_review": False, "problems": ["not_run"]}
            requirements = episode.get("outcome", {}).get("evolved_requirements", [])
            persona_status = "n/a" if arm == "original" else (
                "met" if requirements and all(r["status"] == "met" for r in requirements)
                else "not_met" if any(r["status"] == "not_met" for r in requirements) else "unknown")
            status = ("valid" if release["ready_for_review"] else "invalid" if episode
                      else "incomplete" if attempt else "running" if run else state.get("status", "pending"))
            cells.append({"arm": arm, "persona": name, "status": status,
                "base_success": metrics.get("base_task_success"), "persona_status": persona_status,
                "joint_success": metrics.get("joint_success"), "valid": release["ready_for_review"],
                "problems": release["problems"], "seconds": episode.get("seconds", attempt.get("seconds")),
                "timing": episode.get("timing", {}), "run": run.relative_to(study).as_posix() if run else None,
                "attempts": [p.relative_to(study).as_posix() for p in attempts],
                "agent_turns": metrics.get("interaction_delivery", {}).get("agent_turns_used"),
                "transcript_digest": file_hash(run / "transcript.json") if run and (run / "transcript.json").is_file() else None})
        rows.append({"key": task["key"], "language": task["language"], "task": task["instance_id"],
                     "gold_seconds": state.get("gold_seconds"), "state": state.get("status", "pending"), "cells": cells})
    return rows


def score(value):
    return "unknown" if value is None else f"{int(value)}.00"


def cell_text(cell):
    seconds = "--" if cell["seconds"] is None else f"{cell['seconds']:.0f}s"
    return (f"{cell['persona']}\n{score(cell['base_success'])} / {seconds}\n"
            f"requirement: {cell['persona_status']} | {cell['status']}")


def plot_summary(rows, output, manifest):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10})
    fig, ax = plt.subplots(figsize=(19, 2.4 + .76 * len(rows)))
    fig.patch.set_facecolor("#f7f8fc")
    ax.set_facecolor("#f7f8fc")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, len(rows) + 1)
    ax.axis("off")
    fig.suptitle(f"SWE-bench-Live MultiLang | {len(rows)} tasks, 3 conditions", x=.055, ha="left", y=.98,
                 fontsize=20, fontweight="bold", color="#17243c")
    fig.text(.055, .95, "Score = original official task success (0/1), not persona score. Unknown is never counted as failure.",
             fontsize=10, color="#42526b")
    fig.text(.055, .933, "Time includes interaction + final review + any in-episode calibration; gold preparation is reported separately.",
             fontsize=10, color="#42526b")
    xs = [.0, .285, .525, .765]
    for x, title in zip(xs, ("Task / language",) + LABELS):
        ax.text(x, len(rows) + .6, title, weight="bold", size=12, va="center", color="#17243c")
    for i, row in enumerate(rows):
        y = len(rows) - i - .05
        if i % 2 == 0:
            ax.add_patch(plt.Rectangle((-.01, y - .7), 1.02, .94, color="#edf0f7", zorder=0))
        name = textwrap.fill(row["task"], width=36, break_long_words=True)
        ax.text(0, y, f"{i + 1:02d}  {name}\n{row['language'].upper()}", va="top", fontsize=9.5)
        for x, cell in zip(xs[1:], row["cells"]):
            color = "#176b55" if cell["valid"] else "#7c4d24" if cell["run"] else "#788396"
            ax.text(x, y, cell_text(cell), va="top", fontsize=8.7, linespacing=1.5, color=color)
    fig.subplots_adjust(left=.055, right=.99, top=.9, bottom=.025)
    fig.savefig(output / "summary.png", dpi=150, facecolor=fig.get_facecolor())
    fig.savefig(output / "summary.svg", facecolor=fig.get_facecolor())
    plt.close(fig)


def trajectory(study, row, output):
    import matplotlib.pyplot as plt
    episodes = [optional(study / c["run"] / "episode.json") if c["run"] else {} for c in row["cells"]]
    transcripts = [optional(study / c["run"] / "transcript.json", []) if c["run"] else [] for c in row["cells"]]
    max_turns = max([m.get("turn", 0) for tr in transcripts for m in tr] + [1])
    fig, axes = plt.subplots(1, 3, figsize=(18, max(4.8, 2.4 + .95 * max_turns)))
    fig.suptitle(row["task"], fontsize=17, fontweight="bold", y=.98)
    for ax, cell, transcript, episode, title in zip(axes, row["cells"], transcripts, episodes, LABELS):
        ax.set(xlim=(0, 1), ylim=(-1, max_turns + 1))
        ax.axis("off")
        ax.set_title(title + "\n" + cell["persona"], loc="left", size=11, pad=15)
        if not transcript:
            ax.text(.03, max_turns, cell["status"], color="#788396")
        for turn in sorted({m.get("turn", 0) for m in transcript}):
            messages = [m for m in transcript if m.get("turn") == turn]
            agent = next((m for m in messages if m["role"] == "agent"), None)
            user = next((m for m in messages if m["role"] == "user"), {})
            disclosure = user.get("disclosed_requirement")
            y = max_turns - turn + .85
            color = "#ecad38" if disclosure else "#4479b5"
            ax.plot([.02, .02], [y - .63, y], color=color, linewidth=4)
            status = f"{agent.get('status')} | shell {agent.get('shell_steps', 0)} | {agent.get('seconds', 0):.0f}s" if agent else "user / closing"
            ax.text(.05, y, f"Turn {turn}  {status}", size=9, weight="bold", va="top")
            body = ("DISCLOSED " + disclosure + ": " if disclosure else "")
            body += (agent or user).get("text", "").replace("\n", " ")
            ax.text(.05, y - .23, textwrap.fill(body[:150], 54), va="top", size=8, color="#42526b")
        footer = (f"Official: {score(cell['base_success'])} | requirement: {cell['persona_status']}\n"
                  f"Joint: {score(cell['joint_success'])} | evaluation: {cell['status']}")
        ax.text(.02, -.5, footer, size=9, va="top", color="#17243c")
    fig.subplots_adjust(top=.85, bottom=.075, left=.035, right=.98, wspace=.15)
    fig.savefig(output.with_suffix(".png"), dpi=130, bbox_inches="tight")
    fig.savefig(output.with_suffix(".svg"), bbox_inches="tight")
    plt.close(fig)
    parts = ["<html><meta charset='utf-8'><title>Trajectory</title>",
             "<style>body{font:15px system-ui;max-width:1400px;margin:35px auto;color:#17243c}pre{white-space:pre-wrap;overflow-wrap:anywhere}section{border-top:1px solid #ccd;padding:15px}.user{background:#edf1fa;padding:12px}.agent{padding:12px}</style>",
             f"<h1>{html.escape(row['task'])}</h1><img style='width:100%' src='{output.name}.png'>"]
    for cell, transcript, episode, title in zip(row["cells"], transcripts, episodes, LABELS):
        parts.append(f"<section><h2>{title}: {html.escape(cell['persona'])}</h2><p>{html.escape(cell_text(cell))}</p>")
        for message in transcript:
            parts.append(f"<div class='{message['role']}'><b>{html.escape(message['message_id'])}: {message['role']}</b>"
                         f"<pre>{html.escape(message.get('text', ''))}</pre></div>")
        parts.append("<details><summary>Typed events and final outcome</summary><pre>" +
                     html.escape(json.dumps({"events": episode.get("events"), "outcome": episode.get("outcome")}, indent=2, ensure_ascii=False)) + "</pre></details></section>")
    parts.append("</html>")
    output.with_suffix(".html").write_text("\n".join(parts), encoding="utf-8")


def write_html(rows, output, summary, manifest):
    parts = ["<html><meta charset='utf-8'><title>MultiLang study</title>",
        "<style>body{font:15px system-ui;margin:35px;color:#17243c;background:#f7f8fc}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:18px;border-bottom:1px solid #ccd}tr:nth-child(even){background:#edf0f7}pre{font:13px system-ui;white-space:pre-wrap}a{color:#245c99}</style>",
        f"<h1>{len(rows)} task study: Original + two personas</h1>",
        f"<p>Ready for review: {summary['ready_for_review']}/{summary['planned']}; attempted: {summary['attempted']}. Unknown is not a zero.</p>",
        "<p>Official score is binary original-task success. Persona and joint success are separate. Times include episode interaction and final review; image downloads, gold preflight and shared checker calibration are extra.</p>",
        "<p>Original uses the full issue in one turn; personas use a vague opening and evolving requirements. This comparison does not isolate persona effects from interaction effects.</p>",
        "<p><a href='summary.png'>Summary PNG</a> · <a href='summary.svg'>Summary SVG</a></p>",
        "<table><tr><th>Task</th><th>Original</th><th>Persona 0</th><th>Persona 1</th></tr>"]
    for row in rows:
        parts.append(f"<tr><td><a href='trajectories/{row['key']}.html'>{html.escape(row['task'])}</a><br>{row['language']}</td>")
        parts.extend("<td><pre>" + html.escape(cell_text(c)) + "</pre></td>" for c in row["cells"])
        parts.append("</tr>")
    parts.append("</table></html>")
    (output / "index.html").write_text("\n".join(parts), encoding="utf-8")


def export_bundle(study, manifest, rows, summary):
    # Exports are immutable snapshots; do not overwrite an earlier review bundle.
    destination = study / "review-bundles" / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    destination.mkdir(parents=True)
    save(destination / "manifest.json", manifest)
    save(destination / "results.json", rows)
    save(destination / "summary.json", summary)
    shutil.copytree(study / "reports", destination / "reports")
    for row in rows:
        for cell in row["cells"]:
            for relative in cell["attempts"]:
                run = study / relative
                target = destination / relative
                if audit(run)["ready_for_review"]:
                    export_review(run, target)
                else:
                    target.mkdir(parents=True)
                    for name in ("transcript.json", "transcript.md", "episode.json", "attempt.json", "protocol.json", "checks.jsonl",
                                 "message_guard.json", "turns.json", "first_message.json"):
                        if (run / name).is_file():
                            shutil.copyfile(run / name, target / name)
                    save(target / "release_check.json", audit(run))
    # Scan literal locally configured secret values and complete private payloads.
    forbidden = []
    env = bootstrap.ROOT / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            value = value.strip().strip("\"'")
            if sep and any(word in key.upper() for word in ("KEY", "SECRET", "TOKEN", "PASSWORD")) and len(value) >= 8:
                forbidden.append(value.encode())
    for task in manifest["tasks"]:
        row = json.loads((study / task["path"]).read_text(encoding="utf-8"))
        for field in ("patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS"):
            value = row.get(field)
            if value and len(str(value)) > 80:
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                forbidden.extend((text.encode(), json.dumps(text, ensure_ascii=False)[1:-1].encode()))
    findings = []
    for path in destination.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            if any(marker in data for marker in forbidden):
                findings.append(path.relative_to(destination).as_posix())
    security = {"passed": not findings, "flagged_files": findings, "scope": "Known local secret values and complete private oracle payloads; public artifacts still need semantic review"}
    save(destination / "export_check.json", security)
    (destination / "README.md").write_text(
        "# SWE-bench-Live MultiLang study\n\n"
        f"{summary['attempted']}/{summary['planned']} attempted; {summary['ready_for_review']} eligible for transcript review.\n\n"
        "Open [the result table](reports/index.html) or [the summary image](reports/summary.png). Task links open full transcripts and trajectory graphics. All attempts and invalid results are retained.\n\n"
        "This is a review bundle, not automatic paper finalization. Review openings, persona binding and disclosures, rubric grounding and invalid reasons. Valid task failures remain benchmark data. Unknown/invalid results are not zero scores.\n\n"
        "Original is a full-issue single turn; the persona arms use the reference multi-turn disclosure protocol and one bound requirement each. This design does not isolate persona alone.\n\n"
        "The manifest freezes source hashes, dataset revision, selection seed, model and budgets. Episode protocols include image IDs and gold proof. Private task rows, oracle patches, provider logs and agent thought history are excluded.\n\n"
        "Check export_check.json before copying this directory into a GitHub PR. A failed export check blocks publication.\n",
        encoding="utf-8")
    save(destination / "files.sha256.json", {p.relative_to(destination).as_posix(): file_hash(p) for p in destination.rglob("*") if p.is_file()})
    save(study / "latest-review-bundle.json", {"path": str(destination), "export_check_passed": security["passed"]})
    return destination


def report(study, export=False):
    study = Path(study)
    manifest = read(study / "manifest.json")
    rows = collect(study, manifest)
    output = study / "reports"
    output.mkdir(exist_ok=True)
    trajectories = output / "trajectories"
    trajectories.mkdir(exist_ok=True)
    cells = [c for r in rows for c in r["cells"]]
    summary = {"planned": len(cells), "attempted": sum(bool(c["attempts"]) for c in cells),
               "ready_for_review": sum(c["valid"] for c in cells),
               "official_known": sum(c["base_success"] is not None for c in cells),
               "publication_status": "awaiting_transcript_review" if all(c["valid"] for c in cells) else "incomplete_or_invalid_results_present",
               "updated_utc": datetime.now(timezone.utc).isoformat()}
    previous = optional(output / "render-cache.json")
    state = {"summary": digest(rows), "trajectories": {r["key"]: digest(r) for r in rows}}
    if previous.get("summary") != state["summary"]:
        plot_summary(rows, output, manifest)
    for row in rows:
        if previous.get("trajectories", {}).get(row["key"]) != state["trajectories"][row["key"]]:
            trajectory(study, row, trajectories / row["key"])
    save(output / "render-cache.json", state)
    write_html(rows, output, summary, manifest)
    save(study / "results.json", rows)
    save(study / "summary.json", summary)
    save(output / "results.json", rows)
    save(output / "summary.json", summary)
    if export:
        export_bundle(study, manifest, rows, summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study", type=Path)
    parser.add_argument("--export", action="store_true")
    args = parser.parse_args()
    print(json.dumps(report(args.study, args.export), indent=2))
