"""Registered Linux sampled-process-tree RSS convention, including descendants.

RSS is summed across distinct processes, including shared pages in each process.
This is an observed sampled peak, not an assertion about an unsampled true peak.
The controller's whole-sample deadline remains separate from this workload cap.
"""
import os
from pathlib import Path
import signal
import subprocess
import time


def process_table():
    result = {}
    page_kib = os.sysconf("SC_PAGE_SIZE") // 1024
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            text = (path / "stat").read_text()
            fields = text[text.rfind(")") + 2:].split()
            result[int(path.name)] = {"ppid": int(fields[1]), "group": int(fields[2]),
                                      "start": fields[19], "rss_kib": max(0, int(fields[21])) * page_kib}
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return result


def measure(command, output_path, *, cwd, seconds=30, interval=.01):
    start = time.monotonic()
    peak, samples, seen = 0, 0, {}
    timed_out = False
    with open(output_path, "wb") as output, open(str(output_path) + ".stderr", "wb") as error:
        process = subprocess.Popen(command, cwd=cwd, stdout=output, stderr=error, start_new_session=True)
        while True:
            table = process_table()
            members = {pid for pid, entry in table.items()
                       if pid == process.pid or entry["group"] == process.pid or seen.get(pid) == entry["start"]}
            while True:
                children = {pid for pid, entry in table.items() if entry["ppid"] in members}
                if children <= members:
                    break
                members |= children
            seen.update({pid: table[pid]["start"] for pid in members})
            peak = max(peak, sum(table[pid]["rss_kib"] for pid in members))
            samples += 1
            if process.poll() is not None:
                break
            if time.monotonic() - start > seconds:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                break
            time.sleep(interval)
        code = process.wait(timeout=5)
    elapsed = time.monotonic() - start
    table = process_table()
    survivors = [pid for pid, entry in table.items() if seen.get(pid) == entry["start"] and entry["rss_kib"] > 0]
    # A detached/background worker cannot count as completed processing.
    for pid in survivors:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return {"exit_code": code, "wall_seconds": elapsed, "peak_tree_rss_kib_sampled": peak,
            "sample_interval_seconds": interval, "samples": samples, "processes_observed": len(seen),
            "timeout": timed_out, "background_processes_after_parent_exit": len(survivors),
            "measurement": "sum of RSS of observed process tree and process group; 10 ms requested sampling interval"}
