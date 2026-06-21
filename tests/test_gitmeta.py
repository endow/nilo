from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from nilo.gitmeta import changed_files_since, porcelain_path, working_tree_state


class GitMetaTests(unittest.TestCase):
    def test_porcelain_path_preserves_normal_paths(self) -> None:
        self.assertEqual(porcelain_path(" M docs/design.md"), "docs/design.md")
        self.assertEqual(porcelain_path("M  src/nilo/gitmeta.py"), "src/nilo/gitmeta.py")
        self.assertEqual(porcelain_path("?? reports/task.md"), "reports/task.md")

    def test_porcelain_path_uses_renamed_target(self) -> None:
        self.assertEqual(porcelain_path("R  old.py -> new.py"), "new.py")

    def test_working_tree_state_parses_porcelain_files(self) -> None:
        def fake_git_output(args: list[str], cwd: Path) -> tuple[int, str, str]:
            if args == ["rev-parse", "--is-inside-work-tree"]:
                return 0, "true", ""
            if args == ["status", "--porcelain=v1", "--untracked-files=all"]:
                return 0, " M src/nilo/cli.py\nR  old.py -> new.py\n?? reports/task.md\n", ""
            raise AssertionError(args)

        with patch("nilo.gitmeta.git_output", side_effect=fake_git_output):
            state = working_tree_state(Path.cwd())

        self.assertTrue(state["working_tree_dirty"])
        self.assertEqual(state["working_tree_files"], ["new.py", "reports/task.md", "src/nilo/cli.py"])
        self.assertTrue(state["working_tree_available"])

    def test_changed_files_since_excludes_report_staging_files(self) -> None:
        def fake_git_output(args: list[str], cwd: Path) -> tuple[int, str, str]:
            if args == ["rev-parse", "--is-inside-work-tree"]:
                return 0, "true", ""
            if args == ["diff", "--name-only"]:
                return 0, "src/nilo/guard.py\n.nilo/reports/task_test.md\n", ""
            if args == ["diff", "--name-only", "--staged"]:
                return 0, ".nilo/reports/task_other.md\n", ""
            if args == ["diff", "--name-only", "abc123..HEAD"]:
                return 0, "", ""
            if args == ["ls-files", "--others", "--exclude-standard"]:
                return 0, ".nilo/reports/task_untracked.md\ndocs/design.md\n", ""
            raise AssertionError(args)

        with patch("nilo.gitmeta.git_output", side_effect=fake_git_output):
            files, warnings = changed_files_since("abc123", Path.cwd())

        self.assertEqual(files, {"docs/design.md", "src/nilo/guard.py"})
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
