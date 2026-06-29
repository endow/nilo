from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from nilo.version_advisor import advise_version_bump


class VersionAdvisorTests(unittest.TestCase):
    def write_pyproject_version(self, root: Path, version: str) -> None:
        root.joinpath("pyproject.toml").write_text(
            f'[project]\nname = "sample"\nversion = "{version}"\n',
            encoding="utf-8",
        )

    def init_git_with_tags(self, root: Path, tags: list[str]) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        root.joinpath("tracked.txt").write_text("tracked\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        subprocess.run(
            ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "initial"],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for tag in tags:
            subprocess.run(["git", "tag", tag], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def commit_file_change(self, root: Path, path: str, body: str, message: str) -> None:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        subprocess.run(["git", "add", path], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        subprocess.run(
            ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", message],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def test_version_advisor_patch_only_change_recommends_patch(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_pyproject_version(root, "0.1.9")
            self.init_git_with_tags(root, ["v0.1.9"])
            self.commit_file_change(root, "tests/test_upgrade.py", "def test_upgrade():\n    pass\n", "fix upgrade test")

            advice = advise_version_bump(root)

            self.assertEqual(advice["patch_candidate"], "0.1.10")
            self.assertEqual(advice["minor_candidate"], "0.2.0")
            self.assertEqual(advice["recommended_version"], "0.1.10")
            self.assertEqual(advice["recommended_bump_type"], "patch")
            self.assertIn(advice["confidence"], {"high", "medium"})

    def test_version_advisor_cli_change_recommends_minor(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_pyproject_version(root, "0.1.9")
            self.init_git_with_tags(root, ["v0.1.9"])
            self.commit_file_change(root, "src/nilo/cli_parsers/failure.py", "def register():\n    pass\n", "add failure cli")

            advice = advise_version_bump(root)

            self.assertEqual(advice["recommended_version"], "0.2.0")
            self.assertEqual(advice["recommended_bump_type"], "minor")
            self.assertTrue(any("CLI" in reason for reason in advice["reasons"]))

    def test_version_advisor_db_schema_change_recommends_minor(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_pyproject_version(root, "0.1.9")
            self.init_git_with_tags(root, ["v0.1.9"])
            self.commit_file_change(root, "src/nilo/store.py", "SQL = 'ALTER TABLE tasks ADD COLUMN x TEXT'\n", "add migration")

            advice = advise_version_bump(root)

            self.assertEqual(advice["recommended_bump_type"], "minor")
            self.assertTrue(any("DB schema" in reason for reason in advice["reasons"]))

    def test_version_advisor_recipe_change_recommends_minor(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_pyproject_version(root, "0.1.9")
            self.init_git_with_tags(root, ["v0.1.9"])
            self.commit_file_change(root, "src/nilo/cli_handlers/recipe.py", "def run_recipe():\n    pass\n", "change recipe behavior")

            advice = advise_version_bump(root)

            self.assertEqual(advice["recommended_bump_type"], "minor")
            self.assertTrue(any("Recipe" in reason for reason in advice["reasons"]))

    def test_version_advisor_docs_only_recommends_patch(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_pyproject_version(root, "0.1.9")
            self.init_git_with_tags(root, ["v0.1.9"])
            self.commit_file_change(root, "README.md", "docs\n", "docs update")

            advice = advise_version_bump(root)

            self.assertEqual(advice["recommended_bump_type"], "patch")

    def test_version_advisor_docs_plus_src_user_facing_change_recommends_minor(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_pyproject_version(root, "0.1.9")
            self.init_git_with_tags(root, ["v0.1.9"])
            self.commit_file_change(root, "README.md", "new workflow\n", "docs workflow")
            self.commit_file_change(root, "src/nilo/cli_handlers/workflow.py", "TEXT = 'status --ai runtime instruction'\n", "add ai workflow")

            advice = advise_version_bump(root)

            self.assertEqual(advice["recommended_bump_type"], "minor")
            self.assertTrue(any("Documentation" in reason for reason in advice["reasons"]))
            self.assertTrue(any("AI-facing" in reason for reason in advice["reasons"]))

    def test_version_advisor_current_version_and_latest_tag_mismatch_does_not_auto_resolve(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_pyproject_version(root, "0.1.9")
            self.init_git_with_tags(root, ["v0.1.8"])

            advice = advise_version_bump(root)

            self.assertEqual(advice["confidence"], "low")
            self.assertTrue(advice["requires_explicit_confirmation"])
            self.assertFalse(advice["resolved"])

    def test_version_advisor_existing_recommended_tag_blocks_auto_adoption(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_pyproject_version(root, "0.1.9")
            self.init_git_with_tags(root, ["v0.1.9"])
            self.commit_file_change(root, "src/nilo/cli_handlers/recipe.py", "def run_recipe():\n    pass\n", "change recipe behavior")
            with patch("nilo.version_advisor.existing_release_tag", side_effect=lambda _cwd, version: "v0.2.0" if version == "0.2.0" else ""):
                advice = advise_version_bump(root)

            self.assertIn("tag already exists: v0.2.0", advice["warnings"])
            self.assertFalse(advice["resolved"])


if __name__ == "__main__":
    unittest.main()
