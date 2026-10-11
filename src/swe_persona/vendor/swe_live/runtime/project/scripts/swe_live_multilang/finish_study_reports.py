"""Watch an existing local study and finish its figures; no model/Docker calls.

Reporting is separate from the frozen experiment. This renderer's hash is saved
in the reports. It never changes transcripts, patches, outcomes or metrics.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parent / "reference_v2"))
import bootstrap
from support import digest, file_hash, read, save
from report_study import optional, score, LABELS, report, export_bundle

RENDERER_SHA256 = file_hash(__file__)


def excerpt_lines(text, width=54, max_lines=4):
    """Fit bilingual excerpts in a column; full messages remain in the HTML."""
    remaining = " ".join(text.split())
    lines = []
    while remaining and len(lines) < max_lines:
        used, end = 0, 0
        for char in remaining:
            size = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in "WF" else 1)
            if used + size > width:
                break
            used += size
            end += 1
        if end < len(remaining):
            space = remaining.rfind(" ", 0, end + 1)
            if space > end // 2:
                end = space
        lines.append(remaining[:end].rstrip())
        remaining = remaining[end:].lstrip()
    if remaining:
        lines[-1] = lines[-1][:-3].rstrip() + "..."
    return "\n".join(lines)


def render_trajectory(study, row, target):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    available = {font.name for font in font_manager.fontManager.ttflist}
    cjk = next((name for name in ("Microsoft YaHei", "Noto Sans CJK SC", "SimHei", "WenQuanYi Zen Hei")
                if name in available), None)
    plt.rcParams["font.family"] = ["DejaVu Sans"] + ([cjk] if cjk else [])
    transcripts = [optional(study / c["run"] / "transcript.json", []) if c["run"] else [] for c in row["cells"]]
    turns = max([m.get("turn", 0) for tr in transcripts for m in tr] + [1])
    fig, axes = plt.subplots(1, 3, figsize=(18, max(4.8, 2.5 + .95 * turns)))
    height = fig.get_figheight()
    fig.patch.set_facecolor("#f7f8fc")
    fig.text(.035, 1 - .15 / height, row["task"], fontsize=17, weight="bold", va="top", color="#17243c")
    fig.text(.035, 1 - .50 / height, "Recorded interaction turns | yellow marks a requirement disclosure | full messages are in the matching HTML",
             size=9, color="#42526b", va="top")
    for ax, cell, transcript, title in zip(axes, row["cells"], transcripts, LABELS):
        ax.set(xlim=(0, 1), ylim=(-1, turns + .6))
        ax.axis("off")
        ax.text(0, 1.02, title + "\n" + cell["persona"], transform=ax.transAxes,
                fontsize=11, va="bottom", color="#17243c", linespacing=1.5)
        status = cell["status"]
        if not cell["run"] and status in ("running", "attempted", "preparing"):
            status = "queued"
        if not transcript:
            ax.text(.03, turns, status, color="#788396", fontsize=11)
        for turn in sorted({m.get("turn", 0) for m in transcript}):
            messages = [m for m in transcript if m.get("turn") == turn]
            agent = next((m for m in messages if m["role"] == "agent"), None)
            user = next((m for m in messages if m["role"] == "user"), {})
            disclosure = user.get("disclosed_requirement")
            y = turns - turn + .85
            ax.plot([.02, .02], [y - .66, y], color="#e1a128" if disclosure else "#4479b5", lw=4)
            detail = (f"{agent.get('status')} | {agent.get('shell_steps', 0)} shell steps | {agent.get('seconds', 0):.0f}s"
                      if agent else "user / closing")
            ax.text(.055, y, f"Turn {turn}  {detail}", fontsize=9, weight="bold", va="top")
            body = ("DISCLOSED " + disclosure + ": " if disclosure else "")
            body += (agent or user).get("text", "").replace("\n", " ")
            ax.text(.055, y - .22, excerpt_lines(body), va="top", fontsize=8, color="#42526b")
        ax.text(.02, -.5, f"Official: {score(cell['base_success'])} | requirement: {cell['persona_status']}\n"
                f"Joint: {score(cell['joint_success'])} | {status}", va="top", fontsize=9, color="#17243c")
    # Reserve a fixed header band; subplot titles must not collide with the task title.
    fig.subplots_adjust(top=1 - 1.25 / height, bottom=.09, left=.035, right=.98, wspace=.15)
    for suffix in (".png", ".svg"):
        fig.savefig(target.with_suffix(suffix), dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)


def image_stamps(output, key):
    stamps = {}
    for suffix in (".png", ".svg"):
        path = output / "trajectories" / (key + suffix)
        if path.is_file():
            stat = path.stat()
            stamps[suffix] = [stat.st_size, stat.st_mtime_ns]
    return stamps


def finish_figures(study, force=False):
    rows = read(study / "results.json")
    output = study / "reports"
    cache_path = output / "figure-finish-cache.json"
    previous = optional(cache_path)
    current = {"renderer_sha256": RENDERER_SHA256, "rows": {r["key"]: digest(r) for r in rows}, "images": {}}
    for row in rows:
        if (force or previous.get("renderer_sha256") != current["renderer_sha256"]
                or previous.get("rows", {}).get(row["key"]) != current["rows"][row["key"]]
                or previous.get("images", {}).get(row["key"]) != image_stamps(output, row["key"])):
            render_trajectory(study, row, output / "trajectories" / row["key"])
        current["images"][row["key"]] = image_stamps(output, row["key"])
    save(cache_path, current)
    save(output / "figure-renderer.json", {"script": "scripts/swe_live_multilang/finish_study_reports.py",
         "sha256": RENDERER_SHA256, "updated_utc": datetime.now(timezone.utc).isoformat(),
         "scope": "Figure layout only; official metrics and experiment source hashes are unchanged"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("study", type=Path)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    study = args.study.resolve()
    while True:
        # Main reporting writes complete JSON after each episode. Retry transient
        # read races without changing any experiment artifact.
        try:
            finish_figures(study)
        except (json.JSONDecodeError, PermissionError):
            time.sleep(2)
            continue
        if not args.watch or not (study / "study.lock").exists():
            break
        time.sleep(20)
    # Preserve the main runner's export, then create a fresh immutable bundle
    # containing the finished graphics. No API or Docker work occurs here.
    summary = report(study)
    finish_figures(study, force=True)
    export_bundle(study, read(study / "manifest.json"), read(study / "results.json"), summary)
    print("Finished figures and review bundle:", study / "latest-review-bundle.json", flush=True)


if __name__ == "__main__":
    main()
