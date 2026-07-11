from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nilo.snapshot import (
    DEFAULT_SNAPSHOT_MAX_FILE_BYTES,
    UNCOMPUTED_DIFF_HASH,
    compact_snapshot,
    current_git_snapshot,
    current_git_snapshot_fast,
    current_git_snapshot_full,
    evidence_status,
    git_patch_hash,
    git_changed_content_hash,
    max_snapshot_file_bytes,
)


def run_git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)


def init_repo(root: Path) -> None:
    run_git(root, "init")
    run_git(root, "config", "user.email", "nilo@example.test")
    run_git(root, "config", "user.name", "Nilo Test")
    (root / "README.md").write_text("initial\n", encoding="utf-8")
    run_git(root, "add", "README.md")
    run_git(root, "commit", "-m", "initial")


class SnapshotPolicyTests(unittest.TestCase):
    def test_fast_dirty_verification_remains_current_after_unchanged_commit(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            (root / "README.md").write_text("verified\n", encoding="utf-8")
            verified = current_git_snapshot_fast(root)
            verification = {
                **verified,
                "cwd": str(root),
                "exit_code": 0,
                "timed_out": False,
                "metadata": {"snapshot_mode": "fast", "git_diff_hash_computed": False, "working_tree_patch_hash": git_patch_hash(root)},
            }
            run_git(root, "add", "README.md")
            run_git(root, "commit", "-m", "verified change")

            self.assertEqual(evidence_status(verification, current_git_snapshot(root)), "current")

    def test_fast_dirty_verification_becomes_stale_when_content_changes_before_commit(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            (root / "README.md").write_text("verified\n", encoding="utf-8")
            verified = current_git_snapshot_fast(root)
            verification = {
                **verified,
                "cwd": str(root),
                "exit_code": 0,
                "timed_out": False,
                "metadata": {"snapshot_mode": "fast", "git_diff_hash_computed": False, "working_tree_patch_hash": git_patch_hash(root)},
            }
            (root / "README.md").write_text("changed after verification\n", encoding="utf-8")
            run_git(root, "add", "README.md")
            run_git(root, "commit", "-m", "different change")

            self.assertEqual(evidence_status(verification, current_git_snapshot(root)), "stale")

    def test_fast_dirty_verification_with_untracked_file_remains_current_after_commit(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            (root / "new.txt").write_text("verified new file\n", encoding="utf-8")
            verified = current_git_snapshot(root)
            verification = {
                **verified,
                "cwd": str(root),
                "exit_code": 0,
                "timed_out": False,
                "metadata": {"snapshot_mode": "full", "working_tree_content_hash": git_changed_content_hash(root)},
            }
            run_git(root, "add", "new.txt")
            run_git(root, "commit", "-m", "add verified file")

            self.assertEqual(evidence_status(verification, current_git_snapshot(root)), "current")

    def test_deleted_untracked_file_prevents_false_current_after_commit(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            (root / "README.md").write_text("verified tracked\n", encoding="utf-8")
            (root / "new.txt").write_text("verified untracked\n", encoding="utf-8")
            verified = current_git_snapshot(root)
            verification = {
                **verified,
                "cwd": str(root),
                "exit_code": 0,
                "timed_out": False,
                "metadata": {
                    "snapshot_mode": "full",
                    "working_tree_patch_hash": git_patch_hash(root),
                    "working_tree_content_hash": git_changed_content_hash(root),
                },
            }
            (root / "new.txt").unlink()
            run_git(root, "add", "README.md")
            run_git(root, "commit", "-m", "commit only tracked change")

            self.assertEqual(evidence_status(verification, current_git_snapshot(root)), "stale")

    def test_small_text_file_is_hashed_and_content_change_changes_hash(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            source = root / "src.txt"
            source.write_text("first\n", encoding="utf-8")

            first = current_git_snapshot(root)
            source.write_text("second\n", encoding="utf-8")
            second = current_git_snapshot(root)

            self.assertIn("src.txt", first["snapshot_hashed_paths"])
            self.assertIn("src.txt", first["observed_paths"])
            self.assertNotEqual(first["git_diff_hash"], second["git_diff_hash"])

    def test_fast_snapshot_does_not_compute_diff_hash_or_file_content_hashes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            (root / "README.md").write_text("work\n", encoding="utf-8")
            (root / "untracked.txt").write_text("new\n", encoding="utf-8")

            with patch("nilo.snapshot._diff_hash", side_effect=AssertionError("full diff hash should not run")):
                snapshot = current_git_snapshot(root, mode="fast")

            self.assertEqual(snapshot["snapshot_mode"], "fast")
            self.assertEqual(snapshot["git_diff_hash"], UNCOMPUTED_DIFF_HASH)
            self.assertFalse(snapshot["git_diff_hash_computed"])
            self.assertEqual(snapshot["observed_paths"], ["README.md"])
            self.assertTrue(snapshot["working_tree_dirty"])
            self.assertNotIn("snapshot_hashed_paths", snapshot)

    def test_fast_snapshot_uses_no_untracked_and_never_runs_git_diff(self) -> None:
        calls = []

        def fake_git_output(args: list[str], cwd: Path) -> tuple[int, str, str]:
            calls.append(args)
            self.assertNotEqual(args[:1], ["diff"])
            if args == ["rev-parse", "--is-inside-work-tree"]:
                return 0, "true", ""
            if args == ["rev-parse", "HEAD"]:
                return 0, "abc123", ""
            if args[:4] == ["-c", "core.quotepath=false", "status", "--porcelain=v1"]:
                self.assertEqual(args[4], "--untracked-files=no")
                return 0, " M README.md", ""
            return 1, "", "unexpected command"

        with patch("nilo.snapshot.git_output", side_effect=fake_git_output), patch("nilo.snapshot.head_commit", return_value="abc123"):
            snapshot = current_git_snapshot_fast(Path.cwd())

        self.assertEqual(snapshot["git_head"], "abc123")
        self.assertEqual(snapshot["git_diff_hash"], UNCOMPUTED_DIFF_HASH)
        self.assertTrue(snapshot["working_tree_dirty"])
        self.assertIn(["-c", "core.quotepath=false", "status", "--porcelain=v1", "--untracked-files=no"], calls)

    def test_fast_snapshot_dirty_is_tracked_dirty_only(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            (root / "untracked.txt").write_text("new\n", encoding="utf-8")

            snapshot = current_git_snapshot_fast(root)

            self.assertFalse(snapshot["working_tree_dirty"])
            self.assertEqual(snapshot["observed_paths"], [])

    def test_full_snapshot_wrapper_uses_full_mode(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            (root / "README.md").write_text("work\n", encoding="utf-8")

            snapshot = current_git_snapshot_full(root)

            self.assertEqual(snapshot["snapshot_mode"], "full")
            self.assertTrue(snapshot["git_diff_hash_computed"])

    def test_fast_snapshot_evidence_is_recorded_or_present_not_stale(self) -> None:
        current = {
            "git_head": "abc",
            "git_diff_hash": UNCOMPUTED_DIFF_HASH,
            "working_tree_dirty": True,
            "git_diff_hash_computed": False,
        }
        run = {"git_head": "abc", "git_diff_hash": "old", "working_tree_dirty": True, "timed_out": False, "exit_code": 0}

        self.assertEqual(evidence_status(run, current), "recorded")
        self.assertEqual(evidence_status(run, current, strict=False), "present")

    def test_fast_verification_evidence_is_recorded_against_full_current_snapshot(self) -> None:
        current = {
            "git_head": "abc",
            "git_diff_hash": "full-hash",
            "working_tree_dirty": True,
            "git_diff_hash_computed": True,
            "observed_paths": ["src/nilo/app.py"],
        }
        run = {
            "git_head": "abc",
            "git_diff_hash": UNCOMPUTED_DIFF_HASH,
            "working_tree_dirty": True,
            "git_diff_hash_computed": False,
            "observed_paths": ["src/nilo/app.py"],
            "metadata": {"snapshot_mode": "fast", "git_diff_hash_computed": False},
            "timed_out": False,
            "exit_code": 0,
        }

        self.assertEqual(evidence_status(run, current), "recorded")
        self.assertEqual(evidence_status(run, current, strict=False), "present")

    def test_fast_verification_evidence_is_stale_when_new_code_path_is_dirty(self) -> None:
        current = {
            "git_head": "abc",
            "git_diff_hash": "full-hash",
            "working_tree_dirty": True,
            "git_diff_hash_computed": True,
            "observed_paths": ["src/nilo/app.py", "tests/test_app.py"],
        }
        run = {
            "git_head": "abc",
            "git_diff_hash": UNCOMPUTED_DIFF_HASH,
            "working_tree_dirty": True,
            "git_diff_hash_computed": False,
            "observed_paths": ["src/nilo/app.py"],
            "metadata": {"snapshot_mode": "fast", "git_diff_hash_computed": False},
            "timed_out": False,
            "exit_code": 0,
        }

        self.assertEqual(evidence_status(run, current), "stale")

    def test_fast_verification_evidence_ignores_new_docs_only_dirty_path(self) -> None:
        current = {
            "git_head": "abc",
            "git_diff_hash": "full-hash",
            "working_tree_dirty": True,
            "git_diff_hash_computed": True,
            "observed_paths": ["src/nilo/app.py", "docs/readme.md"],
        }
        run = {
            "git_head": "abc",
            "git_diff_hash": UNCOMPUTED_DIFF_HASH,
            "working_tree_dirty": True,
            "git_diff_hash_computed": False,
            "observed_paths": ["src/nilo/app.py"],
            "metadata": {"snapshot_mode": "fast", "git_diff_hash_computed": False},
            "timed_out": False,
            "exit_code": 0,
        }

        self.assertEqual(evidence_status(run, current, strict=False), "present")

    def test_fast_verification_evidence_is_stale_when_head_changes(self) -> None:
        current = {
            "git_head": "new",
            "git_diff_hash": "full-hash",
            "working_tree_dirty": True,
            "git_diff_hash_computed": True,
            "observed_paths": ["src/nilo/app.py"],
        }
        run = {
            "git_head": "old",
            "git_diff_hash": UNCOMPUTED_DIFF_HASH,
            "working_tree_dirty": True,
            "git_diff_hash_computed": False,
            "observed_paths": ["src/nilo/app.py"],
            "metadata": {"snapshot_mode": "fast", "git_diff_hash_computed": False},
            "timed_out": False,
            "exit_code": 0,
        }

        self.assertEqual(evidence_status(run, current), "stale")

    def test_none_snapshot_verification_is_not_fast_completion_evidence(self) -> None:
        current = {
            "git_head": "abc",
            "git_diff_hash": UNCOMPUTED_DIFF_HASH,
            "working_tree_dirty": False,
            "git_diff_hash_computed": False,
        }
        run = {
            "git_head": None,
            "git_diff_hash": "",
            "working_tree_dirty": False,
            "git_diff_hash_computed": False,
            "metadata": {"snapshot_mode": "none", "git_diff_hash_computed": False},
            "timed_out": False,
            "exit_code": 0,
        }

        self.assertEqual(evidence_status(run, current), "stale")
        self.assertEqual(evidence_status(run, current, strict=False), "stale")

    def test_none_snapshot_verification_is_not_current_in_non_git_workspace(self) -> None:
        current = {
            "git_head": None,
            "git_diff_hash": "",
            "working_tree_dirty": False,
            "git_available": False,
            "git_diff_hash_computed": False,
        }
        run = {
            "git_head": None,
            "git_diff_hash": "",
            "working_tree_dirty": False,
            "metadata": {"snapshot_mode": "none", "git_diff_hash_computed": False},
            "timed_out": False,
            "exit_code": 0,
        }

        self.assertEqual(evidence_status(run, current), "stale")

    def test_legacy_empty_diff_hash_is_not_treated_as_fast_evidence(self) -> None:
        current = {
            "git_head": "abc",
            "git_diff_hash": UNCOMPUTED_DIFF_HASH,
            "working_tree_dirty": True,
            "git_diff_hash_computed": False,
        }
        run = {"git_head": "abc", "git_diff_hash": "", "working_tree_dirty": True, "timed_out": False, "exit_code": 0}

        self.assertEqual(evidence_status(run, current), "stale")

    def test_non_git_snapshot_can_still_match_verification_evidence(self) -> None:
        current = {
            "git_head": None,
            "git_diff_hash": "",
            "working_tree_dirty": False,
            "git_available": False,
            "git_diff_hash_computed": False,
        }
        run = {"git_head": None, "git_diff_hash": "", "working_tree_dirty": False, "timed_out": False, "exit_code": 0}

        self.assertEqual(evidence_status(run, current), "current")

    def test_niloignore_excludes_content_but_keeps_path_and_reason(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            (root / ".niloignore").write_text("ignored/**\n", encoding="utf-8")
            ignored_dir = root / "ignored"
            ignored_dir.mkdir()
            (ignored_dir / "data.txt").write_text("ignored content\n", encoding="utf-8")

            snapshot = current_git_snapshot(root)

            self.assertIn("ignored/data.txt", snapshot["observed_paths"])
            self.assertNotIn("ignored/data.txt", snapshot["snapshot_hashed_paths"])
            self.assertIn(("ignored/data.txt", "ignored"), {(item["path"], item["reason"]) for item in snapshot["snapshot_excluded_paths"]})
            self.assertEqual(snapshot["snapshot_policy"]["ignore_file"], ".niloignore")

    def test_large_file_is_recorded_without_content_hashing(self) -> None:
        with TemporaryDirectory() as directory, patch.dict(os.environ, {"NILO_SNAPSHOT_MAX_FILE_BYTES": "4"}):
            root = Path(directory)
            init_repo(root)
            (root / "large.txt").write_text("too large\n", encoding="utf-8")

            snapshot = current_git_snapshot(root)

            self.assertIn("large.txt", snapshot["observed_paths"])
            self.assertIn("large.txt", snapshot["snapshot_large_paths"])
            self.assertNotIn("large.txt", snapshot["snapshot_hashed_paths"])
            self.assertIn("large_file", {item["reason"] for item in snapshot["snapshot_excluded_paths"]})
            self.assertEqual(snapshot["snapshot_policy"]["max_file_bytes"], 4)

    def test_max_snapshot_file_bytes_uses_default_for_invalid_or_negative_values(self) -> None:
        with patch.dict(os.environ, {"NILO_SNAPSHOT_MAX_FILE_BYTES": "not-an-int"}):
            self.assertEqual(max_snapshot_file_bytes(), DEFAULT_SNAPSHOT_MAX_FILE_BYTES)
        with patch.dict(os.environ, {"NILO_SNAPSHOT_MAX_FILE_BYTES": "-1"}):
            self.assertEqual(max_snapshot_file_bytes(), DEFAULT_SNAPSHOT_MAX_FILE_BYTES)

    def test_zero_max_snapshot_file_bytes_skips_non_empty_file_content(self) -> None:
        with TemporaryDirectory() as directory, patch.dict(os.environ, {"NILO_SNAPSHOT_MAX_FILE_BYTES": "0"}):
            root = Path(directory)
            init_repo(root)
            (root / "one.txt").write_text("x", encoding="utf-8")

            snapshot = current_git_snapshot(root)

            self.assertEqual(snapshot["snapshot_policy"]["max_file_bytes"], 0)
            self.assertIn("one.txt", snapshot["snapshot_large_paths"])
            self.assertNotIn("one.txt", snapshot["snapshot_hashed_paths"])

    def test_binary_file_is_recorded_without_content_hashing(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            (root / "binary.dat").write_bytes(b"text\0binary")

            snapshot = current_git_snapshot(root)

            self.assertIn("binary.dat", snapshot["observed_paths"])
            self.assertIn("binary.dat", snapshot["snapshot_binary_paths"])
            self.assertNotIn("binary.dat", snapshot["snapshot_hashed_paths"])
            self.assertIn("binary", {item["reason"] for item in snapshot["snapshot_excluded_paths"]})

    def test_skipped_file_mtime_change_changes_hash(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            (root / ".niloignore").write_text("ignored/**\n", encoding="utf-8")
            ignored_dir = root / "ignored"
            ignored_dir.mkdir()
            skipped = ignored_dir / "data.txt"
            skipped.write_text("ignored content\n", encoding="utf-8")

            first = current_git_snapshot(root)
            stat = skipped.stat()
            os.utime(skipped, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
            second = current_git_snapshot(root)

            self.assertNotEqual(first["git_diff_hash"], second["git_diff_hash"])
            self.assertIn(("ignored/data.txt", "ignored"), {(item["path"], item["reason"]) for item in second["snapshot_excluded_paths"]})

    def test_unavailable_file_is_recorded_as_excluded_when_stat_fails_after_status(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            flaky = root / "flaky.txt"
            flaky.write_text("content\n", encoding="utf-8")
            original_is_file = Path.is_file
            original_stat = Path.stat

            def patched_is_file(path: Path) -> bool:
                if path.name == "flaky.txt":
                    return True
                return original_is_file(path)

            def patched_stat(path: Path, *args: object, **kwargs: object) -> os.stat_result:
                if path.name == "flaky.txt":
                    raise OSError("stat failed")
                return original_stat(path, *args, **kwargs)

            with patch.object(Path, "is_file", patched_is_file), patch.object(Path, "stat", patched_stat):
                snapshot = current_git_snapshot(root)

            self.assertIn("flaky.txt", snapshot["observed_paths"])
            self.assertIn({"path": "flaky.txt", "reason": "unavailable"}, snapshot["snapshot_excluded_paths"])
            self.assertNotIn("flaky.txt", snapshot["snapshot_hashed_paths"])

    def test_compact_snapshot_keeps_legacy_shape_and_evidence_status_still_matches(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            (root / "src.txt").write_text("work\n", encoding="utf-8")

            snapshot = current_git_snapshot(root)
            compact = compact_snapshot(snapshot)
            run = {**compact, "timed_out": False, "exit_code": 0}

            self.assertEqual(set(compact), {"git_head", "git_diff_hash", "working_tree_dirty"})
            self.assertEqual(evidence_status(run, snapshot), "current")
            self.assertEqual(evidence_status({**run, "git_diff_hash": "stale"}, snapshot), "stale")

    def test_standard_nilo_review_outputs_are_ignored_but_agent_instructions_are_not(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            reviews = root / ".nilo" / "reviews"
            reviews.mkdir(parents=True)
            (reviews / "prompt.md").write_text("review prompt\n", encoding="utf-8")
            instructions = root / ".nilo" / "agent-instructions.md"
            instructions.write_text("canonical instructions\n", encoding="utf-8")

            snapshot = current_git_snapshot(root)

            self.assertIn(".nilo/reviews/prompt.md", snapshot["observed_paths"])
            self.assertIn(".nilo/agent-instructions.md", snapshot["observed_paths"])
            self.assertNotIn(".nilo/reviews/prompt.md", snapshot["snapshot_hashed_paths"])
            self.assertIn(".nilo/agent-instructions.md", snapshot["snapshot_hashed_paths"])
            self.assertIn(
                (".nilo/reviews/prompt.md", "ignored"),
                {(item["path"], item["reason"]) for item in snapshot["snapshot_excluded_paths"]},
            )


if __name__ == "__main__":
    unittest.main()
