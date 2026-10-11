"""Export changes relative to the task, excluding unchanged image artifacts."""
import shlex
import re
import base64


def git_diff_text(runtime, command):
    """Transport exact Git bytes through shell interfaces that normalize CR/LF.

    Base64 is only transport encoding. It neither filters files nor edits the
    diff. Fail explicitly on non-UTF-8 patches instead of replacing bytes.
    """
    rc, encoded, err = runtime.sh("set -o pipefail; " + command + " | base64", timeout=60)
    if rc:
        raise RuntimeError("Cannot export Git diff: " + err[:300])
    try:
        return base64.b64decode("".join(encoded.split()), validate=True).decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError("Git diff transport was not valid base64/UTF-8; bytes were not replaced") from exc


def tracked_image_baseline(runtime, base_commit):
    """Capture image-installed tracked changes without touching its worktree/index.

    Official evaluation starts from this image, which need not be clean relative
    to HEAD. A separate temporary Git index records the exact initial tracked
    tree so unchanged build/install edits cannot become model predictions.
    """
    script = '''set -eu
baseline_index=$(mktemp /tmp/personacode-baseline-index.XXXXXX)
trap 'rm -f -- "$baseline_index"' EXIT
cp -- "$(git rev-parse --git-path index)" "$baseline_index"
export GIT_INDEX_FILE="$baseline_index"
git add -u -- .
git write-tree
'''
    rc, out, err = runtime.sh(script, timeout=60)
    tree = out.strip()
    if rc or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tree):
        raise RuntimeError("Cannot capture initial tracked tree: " + err[:300])
    args = shlex.quote(base_commit) + " " + shlex.quote(tree)
    patch = git_diff_text(runtime, "git diff " + args + " --binary --no-ext-diff --no-textconv")
    rc, names, err = runtime.sh("git diff " + args + " --name-only -z", timeout=60)
    if rc:
        raise RuntimeError("Cannot record image baseline paths: " + err[:300])
    return {"base_commit": base_commit, "tree": tree, "tracked_patch": patch,
            "changed_paths": [name for name in names.split("\0") if name]}


def observed_worktree_tree(runtime):
    """Snapshot tracked and nonignored untracked files in an isolated index.

    An image may ship untracked build files. A change to one must be a modify
    hunk, not a new-file hunk that fails to apply on another copy of the image.
    Neither the model's index nor its worktree is changed by this snapshot.
    """
    script = '''set -eu
snapshot_index=$(mktemp /tmp/personacode-worktree-index.XXXXXX)
trap 'rm -f -- "$snapshot_index"' EXIT
cp -- "$(git rev-parse --git-path index)" "$snapshot_index"
export GIT_INDEX_FILE="$snapshot_index"
git add -A -- .
git write-tree
'''
    rc, out, err = runtime.sh(script, timeout=60)
    tree = out.strip()
    if rc or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tree):
        raise RuntimeError("Cannot snapshot observed worktree: " + err[:300])
    return tree


def export_worktree_prediction(runtime, initial_tree):
    final_tree = observed_worktree_tree(runtime)
    return git_diff_text(runtime, "git diff " + shlex.quote(initial_tree) + " "
                         + shlex.quote(final_tree) + " --binary --no-ext-diff --no-textconv")


def untracked_fingerprints(runtime):
    rc, output, err = runtime.sh(
        "git ls-files --others --exclude-standard -z | xargs -0 -r sha256sum --zero", timeout=60)
    if rc:
        raise RuntimeError("Cannot fingerprint pre-existing files: " + err[:300])
    files = {}
    for record in output.split("\0"):
        if record:
            fingerprint, path = record[:64], record[66:]
            if len(fingerprint) != 64 or not path:
                raise RuntimeError("Invalid untracked-file fingerprint record")
            files[path] = fingerprint
    return files


def path_batches(paths, limit=12000):
    """Keep docker exec below Windows command-line limits, with quoted paths."""
    batch, size = [], 0
    for path in paths:
        quoted = shlex.quote(path)
        if len(quoted) > limit:
            raise RuntimeError("File path exceeds safe command transport size")
        if batch and size + len(quoted) + 1 > limit:
            yield batch
            batch, size = [], 0
        batch.append(quoted)
        size += len(quoted) + 1
    if batch:
        yield batch


def export_prediction(runtime, base_commit, initial_untracked):
    # Include new files, even when the model did not git-add them. Using the
    # original commit (or captured image tree) also retains edits if the model
    # made its own commits. Reference studies pass the observed image tree.
    rc, _, err = runtime.sh("git add -N .", timeout=60)
    if rc:
        raise RuntimeError("Cannot prepare patch index: " + err[:300])
    unchanged = []
    for batch in path_batches(initial_untracked):
        # Missing files can make sha256sum return nonzero while other receipts
        # are valid. Exclude only individually matched, complete fingerprints.
        _, out, _ = runtime.sh("sha256sum --zero -- " + " ".join(batch), timeout=60)
        for record in out.split("\0"):
            if len(record) > 66 and re.fullmatch(r"[0-9a-f]{64}", record[:64]):
                path, fingerprint = record[66:], record[:64]
                if initial_untracked.get(path) == fingerprint:
                    unchanged.append(path)
    for batch in path_batches(unchanged):
        rc, _, err = runtime.sh("git --literal-pathspecs reset -q " + shlex.quote(base_commit)
                                + " -- " + " ".join(batch), timeout=60)
        if rc:
            raise RuntimeError("Cannot exclude unchanged image files: " + err[:300])
    return git_diff_text(runtime, "git diff " + shlex.quote(base_commit) + " --binary --no-ext-diff --no-textconv")
