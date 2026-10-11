"""Real Git regression tests for pre-existing changes in official launch images."""
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch as mock_patch

from patch_export import (export_prediction, tracked_image_baseline, untracked_fingerprints,
                          observed_worktree_tree, export_worktree_prediction)


class Shell:
    def __init__(self, directory):
        self.directory = directory
        self.bash = ("C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else shutil.which("bash"))

    def sh(self, command, timeout=60):
        p = subprocess.run([self.bash, "-c", "export PATH=/usr/bin:/bin:$PATH\n" + command], cwd=self.directory, timeout=timeout,
                           capture_output=True, text=True, encoding="utf-8")
        return p.returncode, p.stdout, p.stderr


class ImageBaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.shell = Shell(self.root)
        self.run_git("init", "-q")
        self.run_git("config", "user.name", "Baseline Test")
        self.run_git("config", "user.email", "baseline@example.invalid")
        self.run_git("config", "core.autocrlf", "false")
        self.write("package-lock.json", "first\npeer=true\nlast\n")
        self.write("source.txt", "original\n")
        self.run_git("add", ".")
        self.run_git("commit", "-qm", "fixture")
        self.base = self.run_git("rev-parse", "HEAD").strip()

    def run_git(self, *args):
        p = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def write(self, path, text):
        (self.root / path).write_text(text, encoding="utf-8")

    def test_capture_preserves_worktree_and_index_and_excludes_initial_changes(self):
        self.write("package-lock.json", "first\nlast\n")
        self.write("image-staged.txt", "installed\n")
        self.run_git("add", "image-staged.txt")
        (self.root / "source.txt").unlink()
        self.write("image-cache.txt", "cache\n")
        status = self.run_git("status", "--porcelain")
        index_before = (self.root / ".git/index").read_bytes()
        baseline = tracked_image_baseline(self.shell, self.base)
        self.assertEqual((self.root / ".git/index").read_bytes(), index_before)
        self.assertEqual(self.run_git("status", "--porcelain"), status)
        self.assertEqual(set(baseline["changed_paths"]), {"package-lock.json", "source.txt", "image-staged.txt"})
        self.assertEqual(export_prediction(self.shell, baseline["tree"], untracked_fingerprints(self.shell)), "")

    def test_committed_model_edits_apply_to_image_without_repeating_install_change(self):
        self.write("package-lock.json", "first\nlast\n")
        self.write("image-cache.txt", "cache\n")
        baseline = tracked_image_baseline(self.shell, self.base)
        initial = untracked_fingerprints(self.shell)
        self.write("source.txt", "fixed\n")
        self.write("package-lock.json", "first\nlast\nmodel entry\n")
        self.write("new-test.txt", "regression\n")
        self.run_git("add", "source.txt", "package-lock.json", "new-test.txt")
        self.run_git("commit", "-qm", "model commit")
        patch = export_prediction(self.shell, baseline["tree"], initial)
        self.assertIn("+fixed", patch)
        self.assertIn("+regression", patch)
        self.assertIn("+model entry", patch)
        self.assertNotIn("-peer=true", patch)
        self.assertNotIn("image-cache.txt", patch)
        # Reconstruct the exact initial image tree in this disposable fixture.
        self.run_git("read-tree", "--reset", "-u", baseline["tree"])
        applied = subprocess.run(["git", "apply", "--check", "-"], input=patch.encode("utf-8"), cwd=self.root,
                                 capture_output=True)
        self.assertEqual(applied.returncode, 0, applied.stderr)

    def test_modified_initial_untracked_file_is_retained(self):
        self.write("image-cache.txt", "cache\n")
        baseline = tracked_image_baseline(self.shell, self.base)
        initial = untracked_fingerprints(self.shell)
        self.write("image-cache.txt", "changed by model\n")
        patch = export_prediction(self.shell, baseline["tree"], initial)
        self.assertIn("+changed by model", patch)

    def test_full_baseline_patch_applies_to_dirty_image_and_preserves_real_index(self):
        self.write("package-lock.json", "image-installed\n")
        self.write("initial [cache].txt", "before\n")
        self.write("initial-deleted.txt", "delete me\n")
        self.write("initial-unchanged.txt", "leave me\n")
        initial = observed_worktree_tree(self.shell)
        index = (self.root / ".git/index").read_bytes()
        self.write("initial [cache].txt", "after\n")
        (self.root / "initial-deleted.txt").unlink()
        self.write("new-test.txt", "regression\n")
        (self.root / "bytes.txt").write_bytes(b"bare[\r] CRLF\r\n")
        (self.root / "binary.dat").write_bytes(bytes(range(256)))
        patch = export_worktree_prediction(self.shell, initial)
        self.assertEqual((self.root / ".git/index").read_bytes(), index)
        self.assertNotIn("package-lock.json", patch)
        self.assertNotIn("initial-unchanged.txt", patch)
        self.assertIn("-before", patch)
        self.assertIn("+after", patch)
        self.assertIn("deleted file mode", patch)
        # Reconstruct the initial image, including its original untracked files.
        for name in ("new-test.txt", "bytes.txt", "binary.dat"):
            (self.root / name).unlink()
        self.write("initial [cache].txt", "before\n")
        self.write("initial-deleted.txt", "delete me\n")
        applied = subprocess.run(["git", "apply", "--reject", "--whitespace=nowarn", "-"],
                                 cwd=self.root, input=patch.encode("utf-8"), capture_output=True)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual((self.root / "initial [cache].txt").read_text(), "after\n")
        self.assertFalse((self.root / "initial-deleted.txt").exists())
        self.assertEqual((self.root / "bytes.txt").read_bytes(), b"bare[\r] CRLF\r\n")
        self.assertEqual((self.root / "binary.dat").read_bytes(), bytes(range(256)))

    def test_full_baseline_handles_model_commit_without_exporting_ignored_files(self):
        self.write(".gitignore", "ignored.txt\n")
        self.write("ignored.txt", "not part of prediction\n")
        initial = observed_worktree_tree(self.shell)
        self.assertEqual(export_worktree_prediction(self.shell, initial), "")
        self.write("source.txt", "fixed\n")
        self.run_git("add", "source.txt")
        self.run_git("commit", "-qm", "model fixture")
        patch = export_worktree_prediction(self.shell, initial)
        self.assertIn("+fixed", patch)
        self.assertNotIn("ignored.txt", patch)

    def test_clean_image_has_original_commit_tree(self):
        baseline = tracked_image_baseline(self.shell, self.base)
        self.assertEqual(baseline["tree"], self.run_git("rev-parse", "HEAD^{tree}").strip())
        self.assertEqual(baseline["tracked_patch"], "")
        self.assertEqual(baseline["changed_paths"], [])

    def test_bare_cr_crlf_and_binary_files_survive_text_mode_shell(self):
        baseline = tracked_image_baseline(self.shell, self.base)
        (self.root / "log.txt").write_bytes(b"checking a bare CR [\r] OK\nWindows line\r\n")
        (self.root / "binary.dat").write_bytes(bytes(range(256)) * 4)
        patch = export_prediction(self.shell, baseline["tree"], {})
        direct = subprocess.run(["git", "diff", baseline["tree"], "--binary", "--no-ext-diff", "--no-textconv"],
                                cwd=self.root, capture_output=True, check=True).stdout
        self.assertEqual(patch.encode("utf-8"), direct)
        self.assertIn(b"[\r]", direct)
        self.assertIn(b"\r\n", direct)
        parsed = subprocess.run(["git", "apply", "--stat", "-"], input=patch.encode("utf-8"),
                                cwd=self.root, capture_output=True)
        self.assertEqual(parsed.returncode, 0, parsed.stderr)

    def test_initial_tracked_cr_is_recorded_exactly(self):
        (self.root / "source.txt").write_bytes(b"original\rprogress\n")
        baseline = tracked_image_baseline(self.shell, self.base)
        expected = subprocess.run(["git", "diff", self.base, baseline["tree"], "--binary", "--no-ext-diff", "--no-textconv"],
                                  cwd=self.root, capture_output=True, check=True).stdout
        self.assertEqual(baseline["tracked_patch"].encode("utf-8"), expected)

    def test_diff_failure_cannot_be_masked_by_base64_pipeline(self):
        with self.assertRaises(RuntimeError):
            export_prediction(self.shell, "missing-invalid-base", {})

    def test_many_image_files_use_bounded_batches_and_preserve_changes(self):
        for i in range(180):
            self.write(f"image file [{i}].txt", f"original {i}\n")
        initial = untracked_fingerprints(self.shell)
        baseline = tracked_image_baseline(self.shell, self.base)
        self.write("image file [1].txt", "changed\n")
        (self.root / "image file [2].txt").unlink()
        with mock_patch.object(self.shell, "sh", wraps=self.shell.sh) as shell:
            patch = export_prediction(self.shell, baseline["tree"], initial)
        self.assertLess(shell.call_count, 10)
        self.assertIn("+changed", patch)
        self.assertNotIn("image file [179].txt", patch)
        self.assertTrue(all(len(call.args[0]) < 13000 for call in shell.call_args_list))


if __name__ == "__main__":
    unittest.main()
