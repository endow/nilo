from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from nilo.backup import BackupError
from nilo.ai_context import AI_CONTEXT_TEXT_MAX_CHARS, project_ai_context, render_ai_context_text
from nilo.cli import git_changed_files, handson_language, main
from nilo.cli_handlers.quality import parse_git_status_porcelain_z
from nilo.project_logic import project_tasks_and_statuses, selected_roadmap_commitment
from nilo.roadmap_render import render_human_roadmap_markdown
from nilo.review_dispatcher import find_executable
from nilo.store import Store
from nilo.task_logic import projected_task_status
from nilo.timeutil import now_iso
from nilo.version_advisor import advise_version_bump

LEGACY_LEARNING_TABLES = {
    "derived_rules",
    "active_instruction_rules",
    "failure_patterns",
    "task_failure_pattern_matches",
    "success_patterns",
}


REPORT = """# 完了報告

## 1. 実施内容
CLIフローを確認した。

## 2. 変更ファイル一覧
- src/nilo/cli.py

## 3. 実行した検証
### テストコマンド
python -m unittest
### テスト結果
3 passed
### 型チェック
未実行。型チェック設定がないため。
### lint
未実行。lint設定がないため。

## 4. 未実行の検証（理由を記載）
型チェックとlintは設定がないため未実行。

## 5. 既知の問題 / 仕様から外れた判断
なし。

## 6. 人間に確認してほしい点
追加確認は不要。
"""


def register_test_reviewer(db: Path, reviewer: str) -> None:
    store = Store(db)
    try:
        now = now_iso()
        store.insert(
            "review_reviewers",
            {
                "id": f"reviewer_{reviewer.replace('-', '_')}",
                "reviewer": reviewer,
                "status": "available",
                "capabilities": ["review"],
                "max_concurrent": 1,
                "metadata": {"test": True},
                "last_heartbeat_at": now,
                "created_at": now,
                "updated_at": now,
            },
        )
    finally:
        store.close()


class CliTests(unittest.TestCase):
    def table_names(self, db: Path) -> set[str]:
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            return {str(row[0]) for row in rows}
        finally:
            conn.close()

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

    def test_recipe_list_resolves_project_user_builtin_precedence(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            project = root / "project"
            project_recipe_dir = project / ".nilo" / "recipes"
            user_recipe_dir = home / ".nilo" / "recipes"
            project_recipe_dir.mkdir(parents=True)
            user_recipe_dir.mkdir(parents=True)
            user_recipe_dir.joinpath("basic-design.recipe.yml").write_text(
                """schema_version: 1
name: basic-design
title: User Design
summary: User override.
instruction: User instruction.
acceptance:
  - User acceptance
""",
                encoding="utf-8",
            )
            project_recipe_dir.joinpath("basic-design.recipe.yml").write_text(
                """schema_version: 1
name: basic-design
title: Project Design
summary: Project override.
instruction: Project instruction.
acceptance:
  - Project acceptance
""",
                encoding="utf-8",
            )

            with patch("nilo.recipe.Path.home", return_value=home):
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["recipe", "list", "--project", str(project), "--all"])

            body = output.getvalue()
            self.assertIn("- basic-design (project): Project Design", body)
            self.assertIn("- basic-design (user) [shadowed]: User Design", body)
            self.assertIn("- basic-design (builtin) [shadowed]: Basic Design Task", body)

    def test_recipe_show_prints_effective_recipe_details(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recipe_dir = root / ".nilo" / "recipes"
            recipe_dir.mkdir(parents=True)
            recipe_dir.joinpath("docs-update.recipe.yml").write_text(
                """schema_version: 1
name: docs-update
title: Docs Update
summary: Update docs.
instruction: |
  Update the requested docs.
acceptance:
  - Docs are accurate
variables:
  project_id:
    type: string
    required: true
completion_contract:
  evidence:
    - changed files are listed
""",
                encoding="utf-8",
            )

            output = io.StringIO()
            with redirect_stdout(output):
                main(["recipe", "show", "docs-update", "--project", str(root)])

            body = output.getvalue()
            self.assertIn("name: docs-update", body)
            self.assertIn("instruction:", body)
            self.assertIn("Update the requested docs.", body)
            self.assertIn("variables:", body)
            self.assertIn('"project_id"', body)
            self.assertIn("completion_contract:", body)
            self.assertIn('"evidence"', body)

    def test_recipe_parser_accepts_documented_verification_mapping_items(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recipe_dir = root / ".nilo" / "recipes"
            recipe_dir.mkdir(parents=True)
            recipe_dir.joinpath("verify-docs.recipe.yml").write_text(
                """schema_version: 1
name: verify-docs
title: Verify Docs
summary: Includes structured verification.
instruction: Check docs.
acceptance:
  - Docs checked
verification:
  - command: "nilo status --project {project_id}"
    reason: "Confirm Nilo state after documentation work."
review:
  required: false
completion_contract:
  evidence:
    - changed files are listed
""",
                encoding="utf-8",
            )

            output = io.StringIO()
            with redirect_stdout(output):
                main(["recipe", "show", "verify-docs", "--project", str(root)])

            body = output.getvalue()
            self.assertIn("verification:", body)
            self.assertIn('"command"', body)
            self.assertIn('"reason"', body)
            self.assertNotIn("parse_error", body)

    def test_recipe_doctor_reports_invalid_and_duplicate_recipes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recipe_dir = root / ".nilo" / "recipes"
            recipe_dir.mkdir(parents=True)
            recipe_dir.joinpath("one.recipe.yml").write_text(
                """schema_version: 1
name: duplicate
title: Duplicate One
summary: First.
instruction: Do work.
acceptance:
  - Done
""",
                encoding="utf-8",
            )
            recipe_dir.joinpath("two.recipe.yml").write_text(
                """schema_version: 1
name: duplicate
title: Duplicate Two
summary: Second.
instruction: Do work.
acceptance:
  - Done
""",
                encoding="utf-8",
            )
            recipe_dir.joinpath("invalid.recipe.yml").write_text(
                """schema_version: 1
name: invalid
title: Invalid
summary: Missing acceptance.
instruction: Do work.
""",
                encoding="utf-8",
            )

            output = io.StringIO()
            with self.assertRaises(SystemExit) as raised, redirect_stdout(output):
                main(["recipe", "doctor", "--project", str(root)])

            self.assertEqual(raised.exception.code, 1)
            body = output.getvalue()
            self.assertIn("error: duplicate_name", body)
            self.assertIn("error: missing_required_field", body)

    def test_recipe_doctor_json_exits_nonzero_for_invalid_recipe(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recipe_dir = root / ".nilo" / "recipes"
            recipe_dir.mkdir(parents=True)
            recipe_dir.joinpath("invalid.recipe.yml").write_text(
                """schema_version: 1
name: invalid
title: Invalid
summary: Invalid optional field shape.
instruction: Do work.
acceptance:
  - Done
verification: "oops"
""",
                encoding="utf-8",
            )

            output = io.StringIO()
            with self.assertRaises(SystemExit) as raised, redirect_stdout(output):
                main(["recipe", "doctor", "--project", str(root), "--format", "json"])

            self.assertEqual(raised.exception.code, 1)
            recipes = json.loads(output.getvalue())
            self.assertEqual(recipes[0]["diagnostics"][0]["code"], "invalid_verification")

    def test_recipe_doctor_reports_parse_error_and_unsupported_schema(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recipe_dir = root / ".nilo" / "recipes"
            recipe_dir.mkdir(parents=True)
            recipe_dir.joinpath("malformed.recipe.yml").write_text("schema_version 1\n", encoding="utf-8")
            recipe_dir.joinpath("future.recipe.yml").write_text(
                """schema_version: 2
name: future
title: Future
summary: Unsupported schema.
instruction: Do work.
acceptance:
  - Done
""",
                encoding="utf-8",
            )

            output = io.StringIO()
            with self.assertRaises(SystemExit), redirect_stdout(output):
                main(["recipe", "doctor", "--project", str(root)])

            body = output.getvalue()
            self.assertIn("error: parse_error", body)
            self.assertIn("error: unsupported_schema_version", body)

    def test_recipe_doctor_reports_unreadable_recipe(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recipe_dir = root / ".nilo" / "recipes"
            recipe_dir.mkdir(parents=True)
            recipe_dir.joinpath("unreadable.recipe.yml").write_text("", encoding="utf-8")

            output = io.StringIO()
            with patch("pathlib.Path.read_text", side_effect=OSError("denied")):
                with self.assertRaises(SystemExit), redirect_stdout(output):
                    main(["recipe", "doctor", "--project", str(root)])

            self.assertIn("error: unreadable", output.getvalue())

    def test_recipe_show_falls_back_to_invalid_source_diagnostics(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recipe_dir = root / ".nilo" / "recipes"
            recipe_dir.mkdir(parents=True)
            recipe_dir.joinpath("invalid.recipe.yml").write_text(
                """schema_version: 1
name: invalid
title: Invalid
summary: Missing acceptance.
instruction: Do work.
""",
                encoding="utf-8",
            )

            output = io.StringIO()
            with redirect_stdout(output):
                main(["recipe", "show", "invalid", "--project", str(root)])

            body = output.getvalue()
            self.assertIn("status: invalid", body)
            self.assertIn("missing_required_field", body)

    def test_recipe_json_lists_builtin_without_project_files(self) -> None:
        with TemporaryDirectory() as directory:
            output = io.StringIO()
            with redirect_stdout(output):
                main(["recipe", "list", "--project", directory, "--format", "json"])

            recipes = json.loads(output.getvalue())
            self.assertTrue(any(recipe["name"] == "basic-design" and recipe["layer"] == "builtin" for recipe in recipes))
            self.assertTrue(any(recipe["name"] == "docs-update" and recipe["layer"] == "builtin" for recipe in recipes))
            self.assertTrue(any(recipe["name"] == "focused-implementation" and recipe["layer"] == "builtin" for recipe in recipes))

    def test_recipe_doctor_accepts_all_builtin_recipes(self) -> None:
        with TemporaryDirectory() as directory:
            output = io.StringIO()
            with redirect_stdout(output):
                main(["recipe", "doctor", "--project", directory, "--format", "json"])

            recipes = json.loads(output.getvalue())
            builtin = [recipe for recipe in recipes if recipe["layer"] == "builtin"]
            self.assertGreaterEqual(len(builtin), 3)
            self.assertTrue(all(recipe["status"] == "valid" for recipe in builtin))
            self.assertFalse(any(diagnostic["severity"] == "error" for recipe in builtin for diagnostic in recipe["diagnostics"]))

    def test_focused_implementation_recipe_suggests_targeted_group_not_full_suite(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", root.name])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(
                        [
                            "--db",
                            str(db),
                            "recipe",
                            "run",
                            "focused-implementation",
                            "--project",
                            root.name,
                            "--var",
                            "change=task guidance",
                            "--var",
                            "verification_command=python tests/run_cli_group.py task",
                            "--dry-run",
                        ]
                    )
            finally:
                os.chdir(previous_cwd)

            body = output.getvalue()
            self.assertIn("python tests/run_cli_group.py task", body)
            self.assertIn("--mode targeted", body)
            self.assertNotIn("python -m unittest discover tests", body)

    def test_recipe_project_id_matching_current_directory_uses_cwd(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recipe_dir = root / ".nilo" / "recipes"
            recipe_dir.mkdir(parents=True)
            recipe_dir.joinpath("local.recipe.yml").write_text(
                """schema_version: 1
name: local
title: Local Recipe
summary: Uses cwd project id.
instruction: Do work.
acceptance:
  - Done
""",
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["recipe", "list", "--project", root.name])
            finally:
                os.chdir(previous_cwd)

            self.assertIn("- local (project): Local Recipe", output.getvalue())

    def test_recipe_unknown_project_path_fails(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            main(["recipe", "list", "--project", "__missing_recipe_project__"])
        self.assertIn("recipe project path not found", str(raised.exception))

    def test_recipe_run_dry_run_renders_task_without_creating_it(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            recipe_dir = root / ".nilo" / "recipes"
            recipe_dir.mkdir(parents=True)
            recipe_dir.joinpath("docs-update.recipe.yml").write_text(
                """schema_version: 1
name: docs-update
title: Docs for {topic}
summary: Update docs.
instruction: |
  Update docs for {topic} in project {project_id}.
acceptance:
  - Docs mention {topic}
variables:
  topic:
    type: string
    required: true
verification:
  - command: "nilo status --project {project_id}"
    reason: "Confirm state."
review:
  required: false
""",
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", root.name])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "recipe", "run", "docs-update", "--project", root.name, "--var", "topic=recipes", "--dry-run"])
            finally:
                os.chdir(previous_cwd)

            body = output.getvalue()
            self.assertIn("recipe_run: dry-run", body)
            self.assertIn("title: Docs for recipes", body)
            self.assertIn("Update docs for recipes", body)
            self.assertIn("Verification requirements:", body)
            store = Store(db)
            try:
                self.assertEqual(store.list_where("tasks", "project_id=?", (root.name,)), [])
            finally:
                store.close()

    def test_recipe_run_create_adds_single_plain_task(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            recipe_dir = root / ".nilo" / "recipes"
            recipe_dir.mkdir(parents=True)
            recipe_dir.joinpath("docs-update.recipe.yml").write_text(
                """schema_version: 1
name: docs-update
title: Docs for {topic}
summary: Update docs.
instruction: |
  Update docs for {topic} in project {project_id}.
acceptance:
  - Docs mention {topic}
variables:
  topic:
    type: string
    required: true
verification:
  - command: "nilo status --project {project_id}"
    reason: "Confirm state."
review:
  required: false
completion_contract:
  evidence:
    - changed files are listed
""",
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", root.name])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "recipe", "run", "docs-update", "--project", root.name, "--var", "topic=recipes", "--type", "documentation", "--risk", "low"])
            finally:
                os.chdir(previous_cwd)

            task_id = output.getvalue().strip()
            store = Store(db)
            try:
                task = store.get("tasks", task_id)
                self.assertIsNotNone(task)
                self.assertEqual(task["project_id"], root.name)
                self.assertEqual(task["title"], "Docs for recipes")
                self.assertEqual(task["task_type"], "documentation")
                self.assertEqual(task["risk_level"], "low")
                self.assertEqual(task["acceptance_criteria"], ["Docs mention recipes"])
                self.assertIn("Recipe: docs-update", task["description"])
                self.assertIn("Update docs for recipes", task["description"])
                self.assertIn("Verification requirements:", task["description"])
                self.assertIn("Review requirements:", task["description"])
                self.assertIn("Completion contract:", task["description"])
                self.assertEqual(store.list_where("review_requests", "task_id=?", (task_id,)), [])
                provenance = store.latest_for_task("recipe_task_provenance", task_id)
                self.assertIsNotNone(provenance)
                self.assertEqual(provenance["recipe_name"], "docs-update")
                self.assertEqual(provenance["source_layer"], "project")
                self.assertIn("docs-update.recipe.yml", provenance["source_id"])
                self.assertEqual(provenance["rendered_fields"]["title"], "Docs for recipes")
                self.assertEqual(provenance["rendered_fields"]["acceptance"], ["Docs mention recipes"])
                self.assertEqual(provenance["recipe_snapshot"]["data"]["title"], "Docs for {topic}")
                self.assertRegex(provenance["content_hash"], r"^[0-9a-f]{64}$")
            finally:
                store.close()

    def test_recipe_run_provenance_snapshot_survives_recipe_file_changes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            recipe_dir = root / ".nilo" / "recipes"
            recipe_dir.mkdir(parents=True)
            recipe_file = recipe_dir / "docs-update.recipe.yml"
            recipe_file.write_text(
                """schema_version: 1
name: docs-update
title: Docs for {topic}
summary: Update docs.
instruction: Update docs for {topic}.
acceptance:
  - Docs mention {topic}
variables:
  topic:
    type: string
    required: true
""",
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", root.name])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "recipe", "run", "docs-update", "--project", root.name, "--var", "topic=recipes"])
            finally:
                os.chdir(previous_cwd)

            task_id = output.getvalue().strip()
            store = Store(db)
            try:
                before = store.latest_for_task("recipe_task_provenance", task_id)
                self.assertIsNotNone(before)
                before_hash = before["content_hash"]
                self.assertEqual(before["recipe_snapshot"]["data"]["title"], "Docs for {topic}")
            finally:
                store.close()

            recipe_file.write_text(
                """schema_version: 1
name: docs-update
title: Changed Docs
summary: Changed recipe.
instruction: Changed instruction.
acceptance:
  - Changed acceptance
""",
                encoding="utf-8",
            )

            store = Store(db)
            try:
                after = store.latest_for_task("recipe_task_provenance", task_id)
                self.assertEqual(after["content_hash"], before_hash)
                self.assertEqual(after["rendered_fields"]["title"], "Docs for recipes")
                self.assertEqual(after["recipe_snapshot"]["data"]["title"], "Docs for {topic}")
            finally:
                store.close()

    def test_plain_task_status_remains_valid_without_recipe_provenance(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "nilo"])
            task_output = io.StringIO()
            with redirect_stdout(task_output):
                main(["--db", str(db), "task", "create", "--project", "nilo", "--title", "Plain task"])
            task_id = task_output.getvalue().strip()

            store = Store(db)
            try:
                self.assertIsNone(store.latest_for_task("recipe_task_provenance", task_id))
            finally:
                store.close()

            status_output = io.StringIO()
            with redirect_stdout(status_output):
                main(["--db", str(db), "task", "status", "--task", task_id])

            body = status_output.getvalue()
            self.assertIn("ID: " + task_id, body)
            self.assertNotIn("recipe:", body)

            project_output = io.StringIO()
            with redirect_stdout(project_output):
                main(["--db", str(db), "project", "status", "--project", "nilo", "--verbose"])
            self.assertNotIn("recipe:", project_output.getvalue())

    def test_recipe_provenance_appears_on_human_status_surfaces(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            recipe_dir = root / ".nilo" / "recipes"
            recipe_dir.mkdir(parents=True)
            recipe_dir.joinpath("docs-update.recipe.yml").write_text(
                """schema_version: 1
name: docs-update
title: Docs for {topic}
summary: Update docs.
instruction: Update docs for {topic}.
acceptance:
  - Docs mention {topic}
variables:
  topic:
    type: string
    required: true
""",
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", root.name])
                task_output = io.StringIO()
                with redirect_stdout(task_output):
                    main(["--db", str(db), "recipe", "run", "docs-update", "--project", root.name, "--var", "topic=recipes"])

                task_id = task_output.getvalue().strip()

                task_status_output = io.StringIO()
                with redirect_stdout(task_status_output):
                    main(["--db", str(db), "task", "status", "--task", task_id])

                project_status_output = io.StringIO()
                with redirect_stdout(project_status_output):
                    main(["--db", str(db), "project", "status", "--project", root.name, "--verbose"])

                project_summary_output = io.StringIO()
                with redirect_stdout(project_summary_output):
                    main(["--db", str(db), "project", "summary", "--project", root.name])

                project_json_output = io.StringIO()
                with redirect_stdout(project_json_output):
                    main(["--db", str(db), "project", "summary", "--project", root.name, "--format", "json"])

                roadmap_discuss_output = io.StringIO()
                with redirect_stdout(roadmap_discuss_output):
                    main(["--db", str(db), "roadmap", "discuss", "--project", root.name])

                roadmap_file = root / "ROADMAP.md"
                with redirect_stdout(io.StringIO()), patch("nilo.project_logic.locale.getlocale", return_value=("en_US", "UTF-8")):
                    main(["--db", str(db), "roadmap", "export", "--project", root.name, "--file", str(roadmap_file)])
            finally:
                os.chdir(previous_cwd)

            for body in [
                task_status_output.getvalue(),
                project_status_output.getvalue(),
                project_summary_output.getvalue(),
                roadmap_discuss_output.getvalue(),
                roadmap_file.read_text(encoding="utf-8"),
            ]:
                self.assertIn("docs-update (project layer)", body)
                self.assertNotIn("recipe_content_hash", body)

            summary = json.loads(project_json_output.getvalue())
            provenance = summary["active_tasks"][0]["recipe_provenance"]
            self.assertEqual(provenance["recipe_name"], "docs-update")
            self.assertEqual(provenance["source_layer"], "project")
            self.assertIn("docs-update.recipe.yml", provenance["source_id"])
            self.assertRegex(provenance["content_hash"], r"^[0-9a-f]{64}$")

    def test_recipe_completion_contract_warns_without_blocking_task_complete_and_done(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            recipe_dir = root / ".nilo" / "recipes"
            recipe_dir.mkdir(parents=True)
            recipe_dir.joinpath("docs-update.recipe.yml").write_text(
                """schema_version: 1
name: docs-update
title: Docs for {topic}
summary: Update docs.
instruction: Update docs for {topic}.
acceptance:
  - Docs mention {topic}
variables:
  topic:
    type: string
    required: true
completion_contract:
  evidence:
    - verification command output is recorded
""",
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", root.name])
                first_output = io.StringIO()
                with redirect_stdout(first_output):
                    main(["--db", str(db), "recipe", "run", "docs-update", "--project", root.name, "--var", "topic=recipes"])
                second_output = io.StringIO()
                with redirect_stdout(second_output):
                    main(["--db", str(db), "recipe", "run", "docs-update", "--project", root.name, "--var", "topic=guides"])

                first_task_id = first_output.getvalue().strip()
                second_task_id = second_output.getvalue().strip()

                status_output = io.StringIO()
                with redirect_stdout(status_output):
                    main(["--db", str(db), "task", "status", "--task", first_task_id])

                complete_output = io.StringIO()
                with redirect_stdout(complete_output):
                    main(["--db", str(db), "task", "complete", "--task", first_task_id, "--reason", "accepted despite warning"])

                done_output = io.StringIO()
                with redirect_stdout(done_output):
                    main(["--db", str(db), "done", "--task", second_task_id, "--reason", "daily accepted despite warning"])
            finally:
                os.chdir(previous_cwd)

            expected = "Recipe warning: missing completion_contract evidence: verification command output is recorded"
            self.assertIn(expected, status_output.getvalue())
            self.assertIn("status: completed_by_user", complete_output.getvalue())
            self.assertIn(expected, complete_output.getvalue())
            self.assertIn("status: completed_by_user", done_output.getvalue())
            self.assertIn(expected, done_output.getvalue())

    def test_recipe_completion_contract_warning_clears_when_report_contains_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            recipe_dir = root / ".nilo" / "recipes"
            recipe_dir.mkdir(parents=True)
            recipe_dir.joinpath("docs-update.recipe.yml").write_text(
                """schema_version: 1
name: docs-update
title: Docs for {topic}
summary: Update docs.
instruction: Update docs for {topic}.
acceptance:
  - Docs mention {topic}
variables:
  topic:
    type: string
    required: true
completion_contract:
  evidence:
    - verification command output is recorded
""",
                encoding="utf-8",
            )
            report = root / "report.md"
            report.write_text(REPORT.replace("CLIフローを確認した。", "verification command output is recorded"), encoding="utf-8")
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", root.name])
                task_output = io.StringIO()
                with redirect_stdout(task_output):
                    main(["--db", str(db), "recipe", "run", "docs-update", "--project", root.name, "--var", "topic=recipes"])
                task_id = task_output.getvalue().strip()
                with redirect_stdout(io.StringIO()), patch(
                    "nilo.cli_handlers.workflow.evaluate_evidence",
                    return_value=("evidence_submitted", [], {"ok": True}),
                ):
                    main(["--db", str(db), "report", "import", "--task", task_id, "--file", str(report)])

                status_output = io.StringIO()
                with redirect_stdout(status_output):
                    main(["--db", str(db), "task", "status", "--task", task_id])
            finally:
                os.chdir(previous_cwd)

            self.assertNotIn("completion_warnings:", status_output.getvalue())

    def test_project_recipe_handoff_export_import_preserves_provenance_with_missing_source_diagnostic(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "source"
            target_root = root / "target"
            source_root.mkdir()
            target_root.mkdir()
            source_db = root / "source.db"
            target_db = root / "target.db"
            handoff = root / "recipes.json"
            recipe_dir = source_root / ".nilo" / "recipes"
            recipe_dir.mkdir(parents=True)
            recipe_file = recipe_dir / "docs-update.recipe.yml"
            recipe_file.write_text(
                """schema_version: 1
name: docs-update
title: Docs for {topic}
summary: Update docs.
instruction: Update docs for {topic}.
acceptance:
  - Docs mention {topic}
variables:
  topic:
    type: string
    required: true
completion_contract:
  evidence:
    - verification command output is recorded
""",
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()
            try:
                os.chdir(source_root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(source_db), "project", "create", "Source", "--id", source_root.name])
                task_output = io.StringIO()
                with redirect_stdout(task_output):
                    main(["--db", str(source_db), "recipe", "run", "docs-update", "--project", source_root.name, "--var", "topic=recipes"])
                task_id = task_output.getvalue().strip()

                export_output = io.StringIO()
                with redirect_stdout(export_output):
                    main(["--db", str(source_db), "project", "export-recipes", "--project", source_root.name, "--file", str(handoff)])

                recipe_file.unlink()

                os.chdir(target_root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(target_db), "project", "create", "Target", "--id", "target_project"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(target_db), "project", "import-recipes", "--project", "target_project", "--file", str(handoff)])
                status_output = io.StringIO()
                with redirect_stdout(status_output):
                    main(["--db", str(target_db), "task", "status", "--task", task_id])
            finally:
                os.chdir(previous_cwd)

            exported = json.loads(handoff.read_text(encoding="utf-8"))
            self.assertEqual(exported["format"], "nilo.recipe_handoff")
            self.assertEqual(exported["recipe_task_provenance"][0]["recipe_name"], "docs-update")
            self.assertEqual(exported["recipe_task_provenance"][0]["recipe_snapshot"]["data"]["title"], "Docs for {topic}")
            self.assertIn("imported_tasks: 1", import_output.getvalue())
            self.assertIn("imported_provenance: 1", import_output.getvalue())
            self.assertIn("imported_recipe_files: 1", import_output.getvalue())
            self.assertIn("diagnostic: warning: missing_recipe_source_file", import_output.getvalue())
            self.assertIn("docs-update (project layer)", status_output.getvalue())
            self.assertTrue((target_root / ".nilo" / "recipes" / "docs-update.recipe.yml").exists())

            target_store = Store(target_db)
            try:
                task = target_store.get("tasks", task_id)
                provenance = target_store.latest_for_task("recipe_task_provenance", task_id)
                self.assertEqual(task["project_id"], "target_project")
                self.assertEqual(provenance["recipe_name"], "docs-update")
                self.assertEqual(provenance["rendered_fields"]["title"], "Docs for recipes")
                self.assertEqual(provenance["recipe_snapshot"]["data"]["title"], "Docs for {topic}")
            finally:
                target_store.close()

    def test_recipe_run_requires_declared_variables(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recipe_dir = root / ".nilo" / "recipes"
            recipe_dir.mkdir(parents=True)
            recipe_dir.joinpath("needs-topic.recipe.yml").write_text(
                """schema_version: 1
name: needs-topic
title: Needs Topic
summary: Needs a variable.
instruction: Work on {topic}.
acceptance:
  - Topic is {topic}
variables:
  topic:
    type: string
    required: true
""",
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with self.assertRaises(SystemExit) as raised:
                    main(["recipe", "run", "needs-topic", "--project", root.name, "--dry-run"])
            finally:
                os.chdir(previous_cwd)

            self.assertIn("missing required recipe variable: topic", str(raised.exception))

    def test_release_recipe_infers_target_version_when_current_matches_latest_tag(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_pyproject_version(root, "0.1.9")
            self.init_git_with_tags(root, ["v0.1.9"])
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["recipe", "run", "release", "--project", root.name, "--dry-run"])
            finally:
                os.chdir(previous_cwd)

            body = output.getvalue()
            self.assertIn("現在バージョン: 0.1.9", body)
            self.assertIn("最新タグ: v0.1.9", body)
            self.assertIn("推奨: 0.1.10 (patch)", body)
            self.assertIn("title: Release 0.1.10", body)
            self.assertNotIn("どの target_version", body)

    def test_release_recipe_requires_explicit_target_version_without_semver_tag(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_pyproject_version(root, "0.1.9")
            self.init_git_with_tags(root, [])
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with self.assertRaises(SystemExit) as raised:
                    main(["recipe", "run", "release", "--project", root.name, "--dry-run"])
            finally:
                os.chdir(previous_cwd)

            body = str(raised.exception)
            self.assertIn("最新タグ: なし", body)
            self.assertIn("推奨: 0.1.10 (patch)", body)
            self.assertIn("nilo recipe run release --project nilo --var target_version=0.1.10", body)

    def test_release_recipe_requires_explicit_target_version_when_current_and_tag_mismatch(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_pyproject_version(root, "0.1.9")
            self.init_git_with_tags(root, ["v0.1.8"])
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with self.assertRaises(SystemExit) as raised:
                    main(["recipe", "run", "release", "--project", root.name, "--dry-run"])
            finally:
                os.chdir(previous_cwd)

            body = str(raised.exception)
            self.assertIn("target_version を自動採用できませんでした。", body)
            self.assertIn("current version and latest tag do not match", body)
            self.assertIn("nilo recipe run release --project nilo --var target_version=0.1.10", body)
            self.assertNotIn("どの target_version", body)

    def test_release_recipe_explicit_target_version_is_not_overwritten(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_pyproject_version(root, "0.1.9")
            self.init_git_with_tags(root, ["v0.1.9"])
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["recipe", "run", "release", "--project", root.name, "--var", "target_version=0.2.0", "--dry-run"])
            finally:
                os.chdir(previous_cwd)

            body = output.getvalue()
            self.assertIn("title: Release 0.2.0", body)
            self.assertNotIn("推奨:", body)

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

    def test_release_recipe_minor_advice_output_does_not_ask_question(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_pyproject_version(root, "0.1.9")
            self.init_git_with_tags(root, ["v0.1.9"])
            self.commit_file_change(root, "src/nilo/cli_handlers/recipe.py", "def run_recipe():\n    pass\n", "change recipe behavior")
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with self.assertRaises(SystemExit) as raised:
                    main(["recipe", "run", "release", "--project", root.name, "--dry-run"])
            finally:
                os.chdir(previous_cwd)

            body = str(raised.exception)
            self.assertIn("推奨: 0.2.0 (minor)", body)
            self.assertIn("Recipe behavior changed", body)
            self.assertIn("nilo recipe run release --project nilo --var target_version=0.2.0", body)
            self.assertNotIn("どちらにしますか", body)
            self.assertNotIn("どの target_version", body)
            self.assertNotIn("進めますか？", body)

    def test_recipe_run_allows_declared_project_id_without_var(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recipe_dir = root / ".nilo" / "recipes"
            recipe_dir.mkdir(parents=True)
            recipe_dir.joinpath("uses-project.recipe.yml").write_text(
                """schema_version: 1
name: uses-project
title: Project {project_id}
summary: Uses auto project id.
instruction: Work in {project_id}.
acceptance:
  - Project is {project_id}
variables:
  project_id:
    type: string
    required: true
""",
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["recipe", "run", "uses-project", "--project", root.name, "--dry-run"])
            finally:
                os.chdir(previous_cwd)

            self.assertIn(f"title: Project {root.name}", output.getvalue())

    def test_recipe_run_interpolation_preserves_literal_braces(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            recipe_dir = root / ".nilo" / "recipes"
            recipe_dir.mkdir(parents=True)
            recipe_dir.joinpath("literal-braces.recipe.yml").write_text(
                """schema_version: 1
name: literal-braces
title: Literal Braces
summary: Keeps non-variable braces.
instruction: |
  Emit JSON {"key": "value"} and shell ${VAR} for {topic}.
acceptance:
  - Topic is {topic}
variables:
  topic:
    type: string
    required: true
""",
                encoding="utf-8",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                main(["recipe", "run", "literal-braces", "--project", str(root), "--var", "topic=recipes", "--dry-run"])

            body = output.getvalue()
            self.assertIn('Emit JSON {"key": "value"}', body)
            self.assertIn("shell ${VAR}", body)
            self.assertIn("for recipes", body)

    def test_handson_language_detects_japanese_locale(self) -> None:
        with patch("nilo.project_logic.locale.getlocale", return_value=("Japanese_Japan", "utf8")):
            self.assertEqual(handson_language(), "ja")
        with patch("nilo.project_logic.locale.getlocale", return_value=("en_US", "UTF-8")):
            self.assertEqual(handson_language(), "en")

    def test_daily_facade_start_status_next_check_done_and_reject(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            project_id = root.name
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", project_id])

                start_output = io.StringIO()
                with redirect_stdout(start_output):
                    main(["--db", str(db), "start", "日常タスク", "--acceptance", "facade が動く"])
                task_id = next(line.split(": ", 1)[1] for line in start_output.getvalue().splitlines() if line.startswith("task: "))

                status_output = io.StringIO()
                with redirect_stdout(status_output):
                    main(["--db", str(db), "status", "--verbose"])
                self.assertIn(f"プロジェクト: {project_id} (Nilo)", status_output.getvalue())
                self.assertIn(f"- {task_id} [計画済み] タスク種別: implementation 日常タスク", status_output.getvalue())

                next_output = io.StringIO()
                with redirect_stdout(next_output):
                    main(["--db", str(db), "next"])
                self.assertIn(f"タスク: {task_id}", next_output.getvalue())
                self.assertIn("作業指示を生成してください。", next_output.getvalue())

                check_output = io.StringIO()
                with redirect_stdout(check_output):
                    main(["--db", str(db), "check", "python --version"])
                self.assertIn("verification_run:", check_output.getvalue())
                self.assertIn("exit_code: 0", check_output.getvalue())

                done_output = io.StringIO()
                with redirect_stdout(done_output):
                    main(["--db", str(db), "done", "--reason", "facade smoke accepted"])
                self.assertIn("status: completed_by_user", done_output.getvalue())

                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "start", "差し戻し対象"])
                reject_output = io.StringIO()
                with redirect_stdout(reject_output):
                    main(["--db", str(db), "reject", "再作業が必要"])
                self.assertIn("status: rejected_by_user", reject_output.getvalue())
            finally:
                os.chdir(previous_cwd)

    def test_facade_next_with_active_task_skips_project_summary(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            project_id = root.name
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", project_id])
                    main(
                        [
                            "--db",
                            str(db),
                            "task",
                            "create",
                            "--project",
                            project_id,
                            "--id",
                            "task_fast_next",
                            "--title",
                            "Fast next",
                        ]
                    )

                next_output = io.StringIO()
                with (
                    patch("nilo.cli_handlers.facade.summary_for_project", side_effect=AssertionError("summary should not run")),
                    patch("nilo.cli_handlers.facade.active_tasks_for_project", side_effect=AssertionError("full active task scan should not run")),
                    redirect_stdout(next_output),
                ):
                    main(["--db", str(db), "next", "--project", project_id])

                self.assertIn("タスク: task_fast_next", next_output.getvalue())
                self.assertIn("作業指示を生成してください。", next_output.getvalue())
            finally:
                os.chdir(previous_cwd)

    def test_queue_lists_unfinished_tasks_and_actionable_todos_only(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_active", "--title", "Active task"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_done", "--title", "Done task"])
                main(["--db", str(db), "task", "complete", "--task", "task_done", "--reason", "done", "--actor", "human"])
                main(["--db", str(db), "todo", "add", "--project", "project_test", "--id", "todo_open", "Open todo"])
                main(["--db", str(db), "todo", "add", "--project", "project_test", "--id", "todo_ready", "Ready todo"])
                main(["--db", str(db), "todo", "triage", "--item", "todo_ready", "--status", "ready", "--reason", "ready"])
                main(["--db", str(db), "todo", "add", "--project", "project_test", "--id", "todo_rejected", "Rejected todo"])
                main(["--db", str(db), "todo", "triage", "--item", "todo_rejected", "--status", "rejected", "--reason", "rejected"])

            store = Store(db)
            try:
                store.insert(
                    "failure_logs",
                    {
                        "id": "failure_queue",
                        "project_id": "project_test",
                        "task_id": "task_done",
                        "report_id": "",
                        "category": "evidence_missing",
                        "message": "historical failure",
                        "severity": "high",
                        "source": "",
                        "actor": "",
                        "related_id": "",
                        "snapshot": {},
                        "status": "open",
                        "resolved_at": "",
                        "resolved_by": "",
                        "resolution_note": "",
                        "created_at": now_iso(),
                    },
                )
            finally:
                store.close()

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "queue", "--project", "project_test"])
            body = output.getvalue()

            self.assertIn("queue: total=3 tasks=1 todos=2", body)
            self.assertIn("task_active [計画済み] implementation medium Active task", body)
            self.assertIn("todo_open [未解決] normal Open todo", body)
            self.assertIn("todo_ready [着手可能] normal Ready todo", body)
            self.assertNotIn("task_done", body)
            self.assertNotIn("todo_rejected", body)
            self.assertNotIn("failure_queue", body)
            self.assertNotIn("historical failure", body)

    def test_queue_json_reports_empty_counts_without_active_task(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "queue", "--project", "project_test", "--json"])

            data = json.loads(output.getvalue())
            self.assertEqual(data["project_id"], "project_test")
            self.assertEqual(data["counts"], {"tasks": 0, "todos": 0, "total": 0})
            self.assertEqual(data["tasks"], [])
            self.assertEqual(data["todos"], [])

    def test_ai_context_surfaces_are_compact_and_json_serializable(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            project_id = root.name
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", project_id])
                    main(["--db", str(db), "task", "create", "--project", project_id, "--id", "task_ai", "--title", "AI compact"])
                store = Store(db)
                try:
                    store.insert(
                        "review_findings",
                        {
                            "id": "finding_ai",
                            "task_id": "task_ai",
                            "review_request_id": "",
                            "review_result_id": "",
                            "title": "Fix before completion",
                            "severity": "medium",
                            "status": "unresolved",
                            "file_path": "src/example.py",
                            "line": 12,
                            "blocking": 0,
                            "description": "",
                            "created_at": now_iso(),
                            "updated_at": now_iso(),
                        },
                    )
                finally:
                    store.close()

                status_text = io.StringIO()
                with redirect_stdout(status_text):
                    main(["--db", str(db), "status", "--ai"])
                body = status_text.getvalue()
                self.assertIn("タスク: task_ai AI compact", body)
                self.assertIn("状態: 計画済み (planned)", body)
                self.assertIn("証跡: 未提出 (missing)", body)
                self.assertIn("未解決レビュー指摘数: 1", body)
                self.assertIn("現在タスク完了診断: 条件未充足 (completion_blocked)", body)
                self.assertNotIn("完了可否", body)
                self.assertLess(len(body), 700)

                task_status_text = io.StringIO()
                with redirect_stdout(task_status_text):
                    main(["--db", str(db), "task", "status", "--task", "task_ai", "--ai"])
                task_status_body = task_status_text.getvalue()
                self.assertIn("現在タスク完了診断: 条件未充足 (completion_blocked)", task_status_body)
                self.assertNotIn("完了可否", task_status_body)

                status_json = io.StringIO()
                with redirect_stdout(status_json):
                    main(["--db", str(db), "status", "--ai", "--json"])
                data = json.loads(status_json.getvalue())
                self.assertEqual(data["current_task"]["task"]["id"], "task_ai")
                self.assertEqual(data["current_task"]["evidence"]["status"], "missing")
                self.assertEqual(data["current_task"]["review"]["unresolved_count"], 1)
                self.assertFalse(data["current_task"]["completion"]["allowed"])

                store = Store(db)
                try:
                    store.insert(
                        "agent_reports",
                        {
                            "id": "report_ai",
                            "task_id": "task_ai",
                            "agent": "codex",
                            "claimed_status": "reported",
                            "changed_files": [],
                            "body_md": "reported",
                            "created_at": now_iso(),
                        },
                    )
                finally:
                    store.close()
                status_with_report = io.StringIO()
                with redirect_stdout(status_with_report):
                    main(["--db", str(db), "status", "--ai"])
                status_body = status_with_report.getvalue()
                self.assertIn("証跡: 提出あり (present)", status_body)
                self.assertIn("作業規模の判定:", status_body)
                self.assertIn("複数ファイルだけでは roadmap 扱いにせず", status_body)
                self.assertIn("複数機能・複数実装トラック", status_body)
                self.assertIn("CLI", status_body)
                self.assertIn("roadmap", status_body)

                for command in (
                    ["task", "show", "--task", "task_ai", "--ai"],
                    ["review", "show", "--task", "task_ai", "--ai"],
                    ["evidence", "show", "--task", "task_ai", "--ai"],
                    ["doctor", "ai-context", "--project", project_id],
                    ["help", "ai"],
                ):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        main(["--db", str(db), *command])
                    self.assertTrue(output.getvalue().strip())

                help_output = io.StringIO()
                with redirect_stdout(help_output):
                    main(["--db", str(db), "help", "ai"])
                help_body = help_output.getvalue()
                self.assertIn("Start with `nilo status --ai`", help_body)
                self.assertIn("Follow the first action shown by `nilo next`", help_body)
                self.assertIn("record it with `nilo check --mode quick|targeted|full`", help_body)
                self.assertIn("Use quick for narrow smoke checks", help_body)
                self.assertIn("Treat timeouts as guardrails", help_body)
                self.assertIn("Work size:", help_body)
                self.assertIn("recommend roadmap planning to the human", help_body)
                self.assertIn("Wait for human approval before creating a roadmap.", help_body)
                self.assertIn("nilo roadmap discuss", help_body)
                self.assertIn("nilo roadmap task-plan", help_body)
                self.assertIn("MCP is not the normal entrypoint", help_body)
                self.assertIn("prefer MCP `dispatch_review`", help_body)
                self.assertIn("`register_reviewer` -> `claim_next_review` -> `import_review_result`", help_body)
                self.assertIn("CLI reviewer process fallback reason", help_body)
                self.assertIn("identity matches the current repository", help_body)
                self.assertIn("CLI fallback", help_body)
                self.assertNotIn("MCP lazy loading", help_body)
                self.assertNotIn("MCP が使えなければ CLI", help_body)

                doctor_output = io.StringIO()
                with redirect_stdout(doctor_output):
                    main(["--db", str(db), "doctor", "ai-context", "--project", project_id])
                doctor_body = doctor_output.getvalue()
                self.assertIn("mcp_default_tool_count: 13", doctor_body)
                self.assertIn("mcp_review_handoff_tool_count:", doctor_body)
                self.assertIn("dispatch_review", doctor_body)
                self.assertIn("register_reviewer", doctor_body)
                self.assertIn(f"status_ai_max_chars: {AI_CONTEXT_TEXT_MAX_CHARS}", doctor_body)
                self.assertIn("status_ai_within_budget: True", doctor_body)
            finally:
                os.chdir(previous_cwd)

    def test_ai_status_without_active_task_does_not_show_completion_diagnosis(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            project_id = root.name
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", project_id])

                status_text = io.StringIO()
                with redirect_stdout(status_text):
                    main(["--db", str(db), "status", "--ai"])

                body = status_text.getvalue()
                self.assertIn("状態: 作業中のタスクなし (no_active_task)", body)
                self.assertIn("現在のタスク: なし", body)
                self.assertNotIn("現在タスク完了診断", body)
                self.assertNotIn("完了可否", body)
            finally:
                os.chdir(previous_cwd)

    def test_status_ai_compacts_large_work_failure_summary_without_truncating_json(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            project_id = root.name
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", project_id])
                    main(
                        [
                            "--db",
                            str(db),
                            "task",
                            "create",
                            "--project",
                            project_id,
                            "--id",
                            "task_compact_budget",
                            "--title",
                            "CLI status next failure docs review roadmap compact budget",
                            "--description",
                            "Update CLI status --ai next actions, docs, tests, review, failure log output, and roadmap guidance together.",
                        ]
                    )
                store = Store(db)
                try:
                    for index in range(3):
                        store.insert(
                            "failure_logs",
                            {
                                "id": f"failure_budget_{index}",
                                "project_id": project_id,
                                "task_id": "task_compact_budget",
                                "report_id": "",
                                "category": "metadata_mismatch",
                                "message": "metadata_mismatch message " * 20,
                                "severity": "high",
                                "source": "test",
                                "actor": "nilo",
                                "related_id": "",
                                "snapshot": {},
                                "status": "open",
                                "resolved_at": "",
                                "resolved_by": "",
                                "resolution_note": "",
                                "created_at": now_iso(),
                            },
                        )
                finally:
                    store.close()

                status_text = io.StringIO()
                with redirect_stdout(status_text):
                    main(["--db", str(db), "status", "--ai"])
                body = status_text.getvalue()
                compact_body = body.split("\n\n", 1)[-1]
                self.assertLessEqual(len(compact_body), AI_CONTEXT_TEXT_MAX_CHARS)
                self.assertIn("タスク: task_compact_budget", compact_body)
                self.assertIn("次の作業:", compact_body)

                status_json = io.StringIO()
                with redirect_stdout(status_json):
                    main(["--db", str(db), "status", "--ai", "--json"])
                data = json.loads(status_json.getvalue())
                self.assertEqual(data["current_task"]["task"]["title"], "CLI status next failure docs review roadmap compact budget")
                self.assertEqual(data["failure_summary"]["open_failures"], 3)
                self.assertGreaterEqual(len(data["next_required_actions"]), 2)
            finally:
                os.chdir(previous_cwd)

    def test_status_ai_compact_keeps_taskization_rules_without_active_task(self) -> None:
        body = render_ai_context_text(
            {
                "project_id": "project_test",
                "project_name": "Project Test",
                "current_task": None,
                "next_required_actions": [
                    'no active task; create or select a Nilo task before implementation; if the user already gave a concrete implementation request, run `nilo start "<short title>" --project project_test` before code edits; ask the user for the next concrete task or design direction'
                ],
                "failure_summary": {
                    "open_failures": 0,
                    "high_open_failures": 0,
                    "latest_open_failure": None,
                },
            },
            max_chars=AI_CONTEXT_TEXT_MAX_CHARS,
        )

        self.assertLessEqual(len(body), AI_CONTEXT_TEXT_MAX_CHARS)
        self.assertIn("語彙ルール", body)
        self.assertIn("Todo ではなく Task 作成を優先する", body)
        self.assertIn("create_todo=受付だけ", body)

    def test_ai_status_missing_project_exits_cleanly(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with self.assertRaises(SystemExit) as raised:
                main(["--db", str(db), "status", "--ai", "--project", "missing_project"])

        self.assertIn("project not found: missing_project", str(raised.exception))

    def test_facade_start_does_not_require_commitment_when_multiple_notes_exist(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            project_id = root.name
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", project_id])
                store = Store(db)
                try:
                    for index in range(2):
                        store.insert(
                            "roadmap_commitments",
                            {
                                "id": f"commitment_{index}",
                                "project_id": project_id,
                                "title": f"Commitment {index}",
                                "intent": "test",
                                "success_criteria": [f"criterion {index}"],
                                "non_goals": [],
                                "autonomy_scope": [],
                                "review_gates": [],
                                "evidence_policy": [],
                                "status": "accepted",
                                "accepted_by": "human",
                                "accepted_at": "2026-06-19T00:00:00+09:00",
                                "created_at": "2026-06-19T00:00:00+09:00",
                            },
                        )
                finally:
                    store.close()

                start_without_commitment = io.StringIO()
                with redirect_stdout(start_without_commitment):
                    main(["--db", str(db), "start", "単発依頼タスク"])
                self.assertIn("task: ", start_without_commitment.getvalue())

                start_output = io.StringIO()
                with redirect_stdout(start_output):
                    main(["--db", str(db), "start", "明示タスク", "--commitment", "commitment_1"])
                self.assertIn("task: ", start_output.getvalue())
            finally:
                os.chdir(previous_cwd)

    def test_overdrive_mode_records_run_cursor_and_gate_events(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            project_id = "project_test"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", project_id])
                store = Store(db)
                try:
                    store.insert(
                        "roadmap_commitments",
                        {
                            "id": "commitment_test",
                            "project_id": project_id,
                            "title": "Overdrive Commitment",
                            "intent": "test overdrive execution",
                            "success_criteria": ["overdrive can select cursor"],
                            "non_goals": [],
                            "autonomy_scope": ["AI may continue through approval gates"],
                            "review_gates": ["Final human review is required"],
                            "evidence_policy": ["Record detailed Overdrive evidence"],
                            "status": "accepted",
                            "accepted_by": "human",
                            "accepted_at": "2026-06-20T00:00:00+09:00",
                            "created_at": "2026-06-20T00:00:00+09:00",
                        },
                    )
                finally:
                    store.close()

                start_output = io.StringIO()
                with redirect_stdout(start_output):
                    main(
                        [
                            "--db",
                            str(db),
                            "task",
                            "start",
                            "Overdrive task",
                            "--project",
                            project_id,
                            "--commitment",
                            "commitment_test",
                            "--mode",
                            "overdrive",
                        ]
                    )
                task_id = next(line.split(": ", 1)[1] for line in start_output.getvalue().splitlines() if line.startswith("task: "))

                status_output = io.StringIO()
                with redirect_stdout(status_output):
                    main(["--db", str(db), "task", "status", "--task", task_id])
                self.assertIn("モード: overdrive", status_output.getvalue())

                run_output = io.StringIO()
                with redirect_stdout(run_output):
                    main(["--db", str(db), "run", "--project", project_id, "--overdrive"])
                body = run_output.getvalue()
                self.assertIn("mode: overdrive", body)
                self.assertIn(f"cursor_task_id: {task_id}", body)
                self.assertIn("approval_gates: bypassed", body)
                self.assertIn("safety_gates: retained", body)
                self.assertIn("final_human_review_checkpoint: required", body)

                store = Store(db)
                try:
                    runs = store.list_where("overdrive_runs", "project_id=?", (project_id,))
                    self.assertEqual(len(runs), 1)
                    self.assertEqual(runs[0]["cursor_task_id"], task_id)
                    self.assertEqual(runs[0]["summary_json"]["human_review_points"][0], "Review the final Overdrive report before closing the roadmap commitment.")
                    events = store.list_where("overdrive_events", "run_id=?", (runs[0]["id"],))
                    event_types = {event["event_type"] for event in events}
                    self.assertIn("approval_gate_bypassed", event_types)
                    self.assertIn("safety_gate_retained", event_types)
                    self.assertIn("cursor_selected", event_types)
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

    def test_overdrive_summary_json_does_not_decode_other_summary_columns(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            store = Store(db)
            try:
                store.insert(
                    "quality_reviews",
                    {
                        "id": "quality_test",
                        "task_id": "task_test",
                        "reviewer": "human",
                        "scores": {},
                        "summary": "42",
                        "issues": [],
                        "created_at": "2026-06-20T00:00:00+09:00",
                    },
                )
                store.insert(
                    "review_results",
                    {
                        "id": "review_result_test",
                        "task_id": "task_test",
                        "review_request_id": "review_request_test",
                        "reviewer": "claude-code",
                        "verdict": "approved",
                        "summary": "true",
                        "body_md": "# Review\n",
                        "created_at": "2026-06-20T00:00:00+09:00",
                    },
                )

                quality = store.get("quality_reviews", "quality_test")
                result = store.get("review_results", "review_result_test")
                self.assertEqual(quality["summary"], "42")
                self.assertIsInstance(quality["summary"], str)
                self.assertEqual(result["summary"], "true")
                self.assertIsInstance(result["summary"], str)
            finally:
                store.close()

    def test_report_facade_keeps_import_subcommand_compatibility(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            project_id = root.name
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", project_id])
                start_output = io.StringIO()
                with redirect_stdout(start_output):
                    main(["--db", str(db), "start", "報告タスク"])
                task_id = next(line.split(": ", 1)[1] for line in start_output.getvalue().splitlines() if line.startswith("task: "))

                report_body = REPORT.replace("- src/nilo/cli.py", "- none")
                facade_output = io.StringIO()
                with patch("sys.stdin", io.StringIO(report_body)), redirect_stdout(facade_output):
                    main(["--db", str(db), "report"])
                self.assertIn("report_form_status:", facade_output.getvalue())

                import_output = io.StringIO()
                with patch("sys.stdin", io.StringIO(report_body)), redirect_stdout(import_output):
                    main(["--db", str(db), "report", "import", "--task", task_id])
                self.assertIn("report_form_status:", import_output.getvalue())
            finally:
                os.chdir(previous_cwd)

    def test_todo_add_list_show_and_triage(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            project_id = "project_test"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", project_id])
                store = Store(db)
                try:
                    store.insert(
                        "roadmap_commitments",
                        {
                            "id": "commitment_test",
                            "project_id": project_id,
                            "title": "Commitment",
                            "intent": "test",
                            "success_criteria": ["todo can become ready"],
                            "non_goals": [],
                            "autonomy_scope": [],
                            "review_gates": [],
                            "evidence_policy": [],
                            "status": "accepted",
                            "accepted_by": "human",
                            "accepted_at": "2026-06-20T00:00:00+09:00",
                            "created_at": "2026-06-20T00:00:00+09:00",
                        },
                    )
                    store.insert(
                        "roadmap_commitments",
                        {
                            "id": "commitment_pending",
                            "project_id": project_id,
                            "title": "Pending Commitment",
                            "intent": "test",
                            "success_criteria": ["pending must not start todo work"],
                            "non_goals": [],
                            "autonomy_scope": [],
                            "review_gates": [],
                            "evidence_policy": [],
                            "status": "pending",
                            "accepted_by": "",
                            "accepted_at": "",
                            "created_at": "2026-06-20T00:00:00+09:00",
                        },
                    )
                finally:
                    store.close()

                add_output = io.StringIO()
                with redirect_stdout(add_output):
                    main(
                        [
                            "--db",
                            str(db),
                            "todo",
                            "add",
                            "--project",
                            project_id,
                            "--kind",
                            "follow_up",
                            "--description",
                            "後続で扱う",
                            "--acceptance-hint",
                            "list/show で見える",
                            "README の導線を見直す",
                        ]
                    )
                todo_id = next(line.split(": ", 1)[1] for line in add_output.getvalue().splitlines() if line.startswith("todo: "))
                self.assertIn("status: open", add_output.getvalue())

                list_output = io.StringIO()
                with redirect_stdout(list_output):
                    main(["--db", str(db), "todo", "list", "--project", project_id])
                self.assertIn(todo_id, list_output.getvalue())
                self.assertIn("follow_up", list_output.getvalue())

                show_output = io.StringIO()
                with redirect_stdout(show_output):
                    main(["--db", str(db), "todo", "show", "--item", todo_id])
                self.assertIn("status: open", show_output.getvalue())
                self.assertIn("acceptance_hint:", show_output.getvalue())

                no_commitment_output = io.StringIO()
                with redirect_stdout(no_commitment_output):
                    main(["--db", str(db), "todo", "triage", "--item", todo_id, "--status", "ready", "--reason", "単発依頼として実行対象にする"])
                self.assertIn("status: ready", no_commitment_output.getvalue())

                with self.assertRaises(SystemExit) as terminal:
                    main(
                        [
                            "--db",
                            str(db),
                            "todo",
                            "triage",
                            "--item",
                            todo_id,
                            "--status",
                            "converted_to_task",
                            "--reason",
                            "terminal status は start 経由のみ",
                        ]
                    )
                self.assertIn("todo status is not triage-settable: converted_to_task", str(terminal.exception))

                triage_output = io.StringIO()
                with redirect_stdout(triage_output):
                    main(
                        [
                            "--db",
                            str(db),
                            "todo",
                            "triage",
                            "--item",
                            todo_id,
                            "--status",
                            "ready",
                            "--commitment",
                            "commitment_test",
                            "--reason",
                            "参照メモとして紐づける",
                        ]
                    )
                self.assertIn("status: ready", triage_output.getvalue())
                self.assertIn("roadmap_commitment_id: commitment_test", triage_output.getvalue())

                ready_output = io.StringIO()
                with redirect_stdout(ready_output):
                    main(["--db", str(db), "todo", "list", "--project", project_id, "--status", "ready"])
                self.assertIn(todo_id, ready_output.getvalue())
            finally:
                os.chdir(previous_cwd)

    def test_todo_start_converts_ready_item_to_task(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            project_id = "project_test"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", project_id])
                store = Store(db)
                try:
                    store.insert(
                        "roadmap_commitments",
                        {
                            "id": "commitment_test",
                            "project_id": project_id,
                            "title": "Commitment",
                            "intent": "test",
                            "success_criteria": ["ready todo can start"],
                            "non_goals": [],
                            "autonomy_scope": [],
                            "review_gates": [],
                            "evidence_policy": [],
                            "status": "accepted",
                            "accepted_by": "human",
                            "accepted_at": "2026-06-20T00:00:00+09:00",
                            "created_at": "2026-06-20T00:00:00+09:00",
                        },
                    )
                    store.insert(
                        "roadmap_commitments",
                        {
                            "id": "commitment_pending",
                            "project_id": project_id,
                            "title": "Pending Commitment",
                            "intent": "test",
                            "success_criteria": ["pending must not start todo work"],
                            "non_goals": [],
                            "autonomy_scope": [],
                            "review_gates": [],
                            "evidence_policy": [],
                            "status": "pending",
                            "accepted_by": "",
                            "accepted_at": "",
                            "created_at": "2026-06-20T00:00:00+09:00",
                        },
                    )
                    store.insert(
                        "todos",
                        {
                            "id": "todo_ready",
                            "project_id": project_id,
                            "title": "Ready work",
                            "kind": "follow_up",
                            "status": "ready",
                            "description": "Build the ready work.",
                            "acceptance_hint": "Task contains acceptance.",
                            "priority": "normal",
                            "source_type": "user_message",
                            "source_task_id": "",
                            "roadmap_commitment_id": "commitment_test",
                            "roadmap_revision_id": "",
                            "converted_task_id": "",
                            "created_at": "2026-06-20T00:00:00+09:00",
                            "triaged_at": "2026-06-20T00:00:00+09:00",
                            "triage_reason": "accepted commitment の範囲内",
                        },
                    )
                    store.insert(
                        "todos",
                        {
                            "id": "todo_pending_commitment",
                            "project_id": project_id,
                            "title": "Pending commitment work",
                            "kind": "follow_up",
                            "status": "ready",
                            "description": "This must not start.",
                            "acceptance_hint": "",
                            "priority": "normal",
                            "source_type": "user_message",
                            "source_task_id": "",
                            "roadmap_commitment_id": "commitment_pending",
                            "roadmap_revision_id": "",
                            "converted_task_id": "",
                            "created_at": "2026-06-20T00:00:00+09:00",
                            "triaged_at": "2026-06-20T00:00:00+09:00",
                            "triage_reason": "pending commitment",
                        },
                    )
                    store.insert(
                        "todos",
                        {
                            "id": "todo_open",
                            "project_id": project_id,
                            "title": "Open work",
                            "kind": "follow_up",
                            "status": "open",
                            "description": "",
                            "acceptance_hint": "",
                            "priority": "normal",
                            "source_type": "user_message",
                            "source_task_id": "",
                            "roadmap_commitment_id": "",
                            "roadmap_revision_id": "",
                            "converted_task_id": "",
                            "created_at": "2026-06-20T00:00:00+09:00",
                            "triaged_at": "",
                            "triage_reason": "",
                        },
                    )
                finally:
                    store.close()

                with self.assertRaises(SystemExit) as raised:
                    main(["--db", str(db), "todo", "start", "--item", "todo_open"])
                self.assertIn("todo is not startable: open", str(raised.exception))

                pending_output = io.StringIO()
                with redirect_stdout(pending_output):
                    main(["--db", str(db), "todo", "start", "--item", "todo_pending_commitment"])
                self.assertIn("status: converted_to_task", pending_output.getvalue())

                start_output = io.StringIO()
                with redirect_stdout(start_output):
                    main(["--db", str(db), "todo", "start", "--item", "todo_ready", "--type", "documentation", "--risk", "low"])
                body = start_output.getvalue()
                task_id = next(line.split(": ", 1)[1] for line in body.splitlines() if line.startswith("task: "))
                self.assertIn("status: converted_to_task", body)
                self.assertIn(f"instruct: nilo instruct --task {task_id}", body)

                store = Store(db)
                try:
                    todo = store.get("todos", "todo_ready")
                    task = store.get("tasks", task_id)
                finally:
                    store.close()
                self.assertEqual(todo["status"], "converted_to_task")
                self.assertEqual(todo["converted_task_id"], task_id)
                self.assertEqual(task["title"], "Ready work")
                self.assertEqual(task["description"], "Build the ready work.")
                self.assertEqual(task["acceptance_criteria"], ["Task contains acceptance."])
                self.assertEqual(task["task_type"], "documentation")
                self.assertEqual(task["risk_level"], "low")
                self.assertEqual(task["roadmap_commitment_id"], "commitment_test")
            finally:
                os.chdir(previous_cwd)

    def test_todo_promote_converts_requires_roadmap_item_to_pending_revision(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            project_id = "project_test"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", project_id])
                store = Store(db)
                try:
                    store.insert(
                        "todos",
                        {
                            "id": "todo_requires_roadmap",
                            "project_id": project_id,
                            "title": "Large follow-up",
                            "kind": "roadmap_candidate",
                            "status": "requires_roadmap",
                            "description": "This needs policy and ordering.",
                            "acceptance_hint": "Roadmap proposal captures the follow-up.",
                            "priority": "normal",
                            "source_type": "user_message",
                            "source_task_id": "",
                            "roadmap_commitment_id": "",
                            "roadmap_revision_id": "",
                            "converted_task_id": "",
                            "created_at": "2026-06-20T00:00:00+09:00",
                            "triaged_at": "2026-06-20T00:00:00+09:00",
                            "triage_reason": "複数 task と成功条件定義が必要",
                        },
                    )
                    store.insert(
                        "todos",
                        {
                            "id": "todo_ready",
                            "project_id": project_id,
                            "title": "Ready work",
                            "kind": "follow_up",
                            "status": "ready",
                            "description": "",
                            "acceptance_hint": "",
                            "priority": "normal",
                            "source_type": "user_message",
                            "source_task_id": "",
                            "roadmap_commitment_id": "commitment_test",
                            "roadmap_revision_id": "",
                            "converted_task_id": "",
                            "created_at": "2026-06-20T00:00:00+09:00",
                            "triaged_at": "",
                            "triage_reason": "",
                        },
                    )
                finally:
                    store.close()

                with self.assertRaises(SystemExit) as raised:
                    main(["--db", str(db), "todo", "promote", "--item", "todo_ready", "--to", "roadmap-proposal", "--reason", "needs roadmap"])
                self.assertIn("todo is not promotable: ready", str(raised.exception))

                promote_output = io.StringIO()
                with redirect_stdout(promote_output):
                    main(
                        [
                            "--db",
                            str(db),
                            "todo",
                            "promote",
                            "--item",
                            "todo_requires_roadmap",
                            "--to",
                            "roadmap-proposal",
                            "--reason",
                            "needs accepted roadmap scope",
                        ]
                    )
                body = promote_output.getvalue()
                revision_id = next(line.split(": ", 1)[1] for line in body.splitlines() if line.startswith("roadmap_revision: "))
                commitment_id = next(line.split(": ", 1)[1] for line in body.splitlines() if line.startswith("proposed_commitment: "))
                self.assertIn("status: superseded", body)

                store = Store(db)
                try:
                    todo = store.get("todos", "todo_requires_roadmap")
                    revision = store.get("roadmap_revisions", revision_id)
                    commitment = store.get("roadmap_commitments", commitment_id)
                finally:
                    store.close()
                self.assertEqual(todo["status"], "superseded")
                self.assertEqual(todo["roadmap_revision_id"], revision_id)
                self.assertEqual(revision["status"], "pending")
                self.assertEqual(revision["source_path"], "todo:todo_requires_roadmap")
                self.assertIn("# Large follow-up", revision["body_md"])
                self.assertIn("focused test group first", revision["body_md"])
                self.assertEqual(commitment["status"], "pending")
                self.assertEqual(commitment["title"], "Large follow-up")
                self.assertEqual(commitment["success_criteria"], ["Roadmap proposal captures the follow-up."])
                self.assertEqual(
                    commitment["evidence_policy"],
                    [
                        "Record targeted verification for the changed module or focused test group first; "
                        "use full verification only for release, broad-risk, or shared-core changes; "
                        "if full verification is skipped, document the scope reason instead of treating the skip as a failure."
                    ],
                )
            finally:
                os.chdir(previous_cwd)

    def test_status_and_next_include_todo_summary_when_no_task_is_active(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            project_id = "project_test"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", project_id])
                store = Store(db)
                try:
                    store.insert(
                        "roadmap_commitments",
                        {
                            "id": "commitment_test",
                            "project_id": project_id,
                            "title": "Commitment",
                            "intent": "test",
                            "success_criteria": ["todo next works"],
                            "non_goals": [],
                            "autonomy_scope": [],
                            "review_gates": [],
                            "evidence_policy": [],
                            "status": "accepted",
                            "accepted_by": "human",
                            "accepted_at": "2026-06-20T00:00:00+09:00",
                            "created_at": "2026-06-20T00:00:00+09:00",
                        },
                    )
                    for todo_id, status in (
                        ("todo_ready", "ready"),
                        ("todo_requires_roadmap", "requires_roadmap"),
                        ("todo_open", "open"),
                        ("todo_deferred", "deferred"),
                    ):
                        store.insert(
                            "todos",
                            {
                                "id": todo_id,
                                "project_id": project_id,
                                "title": todo_id,
                                "kind": "follow_up",
                                "status": status,
                                "description": "",
                                "acceptance_hint": "",
                                "priority": "normal",
                                "source_type": "user_message",
                                "source_task_id": "",
                                "roadmap_commitment_id": "commitment_test" if status == "ready" else "",
                                "roadmap_revision_id": "",
                                "converted_task_id": "",
                                "created_at": "2026-06-20T00:00:00+09:00",
                                "triaged_at": "2026-06-20T00:00:00+09:00",
                                "triage_reason": "test",
                            },
                        )
                finally:
                    store.close()

                status_output = io.StringIO()
                with redirect_stdout(status_output):
                    main(["--db", str(db), "status", "--project", project_id, "--verbose"])
                status_body = status_output.getvalue()
                self.assertIn("TODO:", status_body)
                self.assertIn("- 着手可能: 1", status_body)
                self.assertIn("- ロードマップ確認待ち: 1", status_body)

                next_output = io.StringIO()
                with redirect_stdout(next_output):
                    main(["--db", str(db), "next", "--project", project_id])
                self.assertIn("実行できる依頼を具体的な Task にします。", next_output.getvalue())

                store = Store(db)
                try:
                    store.update("todos", "todo_ready", {"status": "converted_to_task"})
                finally:
                    store.close()
                promote_next_output = io.StringIO()
                with redirect_stdout(promote_next_output):
                    main(["--db", str(db), "next", "--project", project_id])
                self.assertIn("この依頼は大きめ", promote_next_output.getvalue())

                store = Store(db)
                try:
                    store.update("todos", "todo_requires_roadmap", {"status": "superseded"})
                finally:
                    store.close()
                roadmap_next_output = io.StringIO()
                with redirect_stdout(roadmap_next_output):
                    main(["--db", str(db), "next", "--project", project_id])
                self.assertIn(
                    "作業中のタスクはありません。次に扱う具体的な作業を人間が決めてください。",
                    roadmap_next_output.getvalue(),
                )
                self.assertNotIn("todo_open", roadmap_next_output.getvalue())
            finally:
                os.chdir(previous_cwd)

    def test_agent_install_updates_codex_override_and_leaves_tracked_agents_unchanged(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            agents = root / "AGENTS.md"
            agents.write_text("# Existing\n\nKeep this.", encoding="utf-8")
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "agent", "install", "--project", "project_test", "--target", "codex"])
            finally:
                os.chdir(previous_cwd)

            self.assertEqual("# Existing\n\nKeep this.", agents.read_text(encoding="utf-8"))
            body = (root / "AGENTS.override.md").read_text(encoding="utf-8")
            runtime = (root / ".nilo" / "agent-instructions.md").read_text(encoding="utf-8")
            self.assertIn("<!-- Generated by Nilo. Do not edit manually. -->", body)
            self.assertIn("<!-- Generated by Nilo. Do not edit manually. -->", runtime)
            self.assertIn("<!-- BEGIN NILO MANAGED BLOCK -->", body)
            self.assertIn("nilo status --ai --project project_test", body)
            self.assertIn("nilo next --project project_test", body)
            self.assertIn("evidence が stale / missing / failed の場合は完了扱いしない", body)
            self.assertIn("unresolved review finding がある場合は完了扱いしない", body)
            self.assertIn("nilo check", body)
            self.assertIn("MCP は通常入口ではない", body)
            self.assertIn("nilo review status --task <task_id> --format json", body)
            self.assertIn("`review status` に `--project` は付けない", body)
            self.assertIn("MCP identity guard", body)
            self.assertIn("repository / project / git_root / db_path", body)
            self.assertIn("CLI fallback", body)
            self.assertIn("大きな作業の扱い", body)
            self.assertIn("nilo roadmap discuss", body)
            self.assertIn("nilo roadmap task-plan", body)
            self.assertNotIn("MCP lazy loading", body)
            self.assertNotIn("MCP が使えなければ CLI", body)
            self.assertIn("最終完了判断、commit、force、roadmap close は人間が行う", body)
            self.assertIn("nilo help ai", body)
            self.assertLess(len(body), 2400)
            self.assertNotIn("## 全コマンド一覧", body)
            self.assertNotIn("nilo project export-handson --project project_test --file HANDOFF.md", body)
            self.assertNotIn("nilo task create --project project_test", body)
            self.assertNotIn("nilo roadmap import --project project_test --file docs/roadmap_proposal.md", body)
            self.assertNotIn("nilo roadmap assess", body)
            self.assertNotIn("nilo quality autoscore import", body)
            self.assertNotIn("nilo rules derive import", body)
            self.assertNotIn("--file reports/<task_id>.md", body)

    def test_agent_install_block_is_minimal_protocol_not_command_reference(self) -> None:
        verbose_reference_only_commands = [
            "nilo project export-handson",
            "nilo roadmap assess",
            "nilo outcome accept-with-concerns",
            "nilo quality autoscore import",
            "nilo rules derive import",
        ]

        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "agent", "install", "--project", "project_test", "--target", "codex"])
            finally:
                os.chdir(previous_cwd)

            body = (root / "AGENTS.override.md").read_text(encoding="utf-8")
            self.assertIn("## Nilo 必須プロトコル", body)
            self.assertIn("nilo status --ai --project project_test", body)
            self.assertIn("nilo next --project project_test", body)
            self.assertIn("evidence が stale / missing / failed の場合は完了扱いしない", body)
            self.assertIn("unresolved review finding がある場合は完了扱いしない", body)
            self.assertIn("nilo check", body)
            self.assertIn("Review handoff", body)
            self.assertIn("nilo review status --task <task_id> --format json", body)
            self.assertIn("`review status` に `--project` は付けない", body)
            self.assertIn("大きな作業の扱い", body)
            self.assertIn("commit、force、roadmap close は人間が行う", body)
            self.assertIn("roadmap close", body)
            present = [command for command in verbose_reference_only_commands if command in body]
            self.assertEqual([], present)

    def test_agent_install_all_updates_codex_and_claude_code(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "agent", "install", "--project", "project_test", "--target", "all"])
            finally:
                os.chdir(previous_cwd)

            agents = (root / "AGENTS.override.md").read_text(encoding="utf-8")
            claude = (root / "CLAUDE.local.md").read_text(encoding="utf-8")
            runtime = (root / ".nilo" / "agent-instructions.md").read_text(encoding="utf-8")
            self.assertIn("nilo status --ai --project project_test", agents)
            self.assertIn("@.nilo/agent-instructions.md", claude)
            self.assertIn("nilo help ai", runtime)
            self.assertIn("MCP identity guard", runtime)
            self.assertIn("必ず high-level `dispatch_review` を第一候補にする", runtime)
            self.assertIn("`register_reviewer` -> `claim_next_review` -> `import_review_result`", runtime)
            self.assertIn("`claude` / `codex` CLI の直接起動", runtime)
            self.assertIn("CLI reviewer process fallback", runtime)
            self.assertIn("nilo review status --task <task_id> --format json", runtime)
            self.assertIn("`review status` に `--project` は付けない", runtime)
            self.assertIn("repository / project / git_root / db_path", runtime)
            self.assertIn("CLI fallback", runtime)
            self.assertIn("unresolved review finding", agents)
            self.assertNotIn("When acting as the `codex` reviewer through Nilo MCP", runtime)
            self.assertNotIn("When acting as the `claude-code` reviewer through Nilo MCP", runtime)

    def test_agent_install_claude_code_includes_reviewer_protocol(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "agent", "install", "--project", "project_test", "--target", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            body = (root / "CLAUDE.local.md").read_text(encoding="utf-8")
            self.assertIn("<!-- Generated by Nilo. Do not edit manually. -->", body)
            self.assertIn("@.nilo/agent-instructions.md", body)
            runtime = (root / ".nilo" / "agent-instructions.md").read_text(encoding="utf-8")
            self.assertNotIn("## Nilo MCP Reviewer Protocol", body)
            self.assertIn("nilo status --ai --project project_test", runtime)
            self.assertNotIn('"worker_path": "claude-code-mcp-session"', runtime)

    def test_agent_install_codex_includes_reviewer_protocol(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "agent", "install", "--project", "project_test", "--target", "codex"])
            finally:
                os.chdir(previous_cwd)

            body = (root / "AGENTS.override.md").read_text(encoding="utf-8")
            self.assertNotIn("## Nilo MCP Reviewer Protocol", body)
            self.assertNotIn("When acting as the `codex` reviewer through Nilo MCP", body)
            self.assertIn("nilo status --ai --project project_test", body)

    def test_agent_install_claude_code_reviewer_protocol_does_not_duplicate_existing_section(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            claude = root / "CLAUDE.local.md"
            claude.write_text(
                "# Existing\n\n"
                "## Nilo MCP Reviewer Protocol\n\n"
                "old reviewer protocol\n\n"
                "## Keep\n\n"
                "Keep this section.\n",
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "agent", "install", "--project", "project_test", "--target", "claude-code"])
                    main(["--db", str(db), "agent", "install", "--project", "project_test", "--target", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            body = claude.read_text(encoding="utf-8")
            self.assertEqual("# Existing\n\n## Nilo MCP Reviewer Protocol\n\nold reviewer protocol\n\n## Keep\n\nKeep this section.\n", body)

    def test_agent_install_replaces_existing_nilo_block_only(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            agents = root / "AGENTS.md"
            agents.write_text(
                "before\n\n"
                "<!-- BEGIN NILO MANAGED BLOCK -->\nold block\n<!-- END NILO MANAGED BLOCK -->\n\n"
                "after\n",
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "agent", "install", "--project", "project_test", "--target", "codex"])
            finally:
                os.chdir(previous_cwd)

            body = agents.read_text(encoding="utf-8")
            self.assertIn("before", body)
            self.assertIn("after", body)
            self.assertIn("old block", body)
            self.assertEqual(body.count("<!-- BEGIN NILO MANAGED BLOCK -->"), 1)
            override = (root / "AGENTS.override.md").read_text(encoding="utf-8")
            self.assertIn("<!-- BEGIN NILO MANAGED BLOCK -->", override)

    def test_init_creates_project_from_current_folder_and_installs_agents(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "sample_project"
            root.mkdir()
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "init"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            project = store.get("projects", "sample_project")
            store.close()
            self.assertIsNotNone(project)
            self.assertEqual(project["name"], "sample_project")
            self.assertIn("created project: sample_project", output.getvalue())
            self.assertIn("updated: AGENTS.override.md", output.getvalue())
            self.assertIn("updated: CLAUDE.local.md", output.getvalue())
            self.assertIn("updated: .git/info/exclude", output.getvalue())
            agents = (root / "AGENTS.override.md").read_text(encoding="utf-8")
            claude = (root / "CLAUDE.local.md").read_text(encoding="utf-8")
            runtime = (root / ".nilo" / "agent-instructions.md").read_text(encoding="utf-8")
            exclude = (root / ".git" / "info" / "exclude").read_text(encoding="utf-8")
            self.assertIn("nilo status --ai --project sample_project", agents)
            self.assertIn("@.nilo/agent-instructions.md", claude)
            self.assertIn("nilo status --ai --project sample_project", runtime)
            self.assertIn("unresolved review finding", agents)
            self.assertIn("nilo help ai", agents)
            self.assertIn(".nilo/", exclude)
            self.assertIn("CLAUDE.local.md", exclude)
            self.assertIn("AGENTS.override.md", exclude)
            self.assertFalse((root / "AGENTS.md").exists())
            self.assertFalse((root / "CLAUDE.md").exists())

    def test_init_is_repeatable_for_existing_project(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "sample_project"
            root.mkdir()
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "init"])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "init"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            projects = store.list_where("projects", "id=?", ("sample_project",))
            store.close()
            agents = (root / "AGENTS.override.md").read_text(encoding="utf-8")
            claude = (root / "CLAUDE.local.md").read_text(encoding="utf-8")
            exclude_lines = (root / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(projects), 1)
            self.assertIn("project exists: sample_project", output.getvalue())
            self.assertEqual(agents.count("<!-- BEGIN NILO MANAGED BLOCK -->"), 1)
            self.assertEqual(claude.count("@.nilo/agent-instructions.md"), 1)
            self.assertEqual(exclude_lines.count(".nilo/"), 1)
            self.assertEqual(exclude_lines.count("CLAUDE.local.md"), 1)
            self.assertEqual(exclude_lines.count("AGENTS.override.md"), 1)

    def test_init_keeps_existing_nilo_gitignore_entry(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "sample_project"
            root.mkdir()
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            exclude = root / ".git" / "info" / "exclude"
            exclude.write_text("build/\n.nilo/\nCLAUDE.local.md\nAGENTS.override.md\n", encoding="utf-8")
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "init"])
            finally:
                os.chdir(previous_cwd)

            exclude_body = exclude.read_text(encoding="utf-8")
            self.assertNotIn("updated: .git/info/exclude", output.getvalue())
            self.assertEqual(exclude_body.splitlines().count(".nilo/"), 1)
            self.assertEqual(exclude_body.splitlines().count("CLAUDE.local.md"), 1)
            self.assertEqual(exclude_body.splitlines().count("AGENTS.override.md"), 1)

    def test_init_uses_git_resolved_exclude_path_for_worktree(self) -> None:
        with TemporaryDirectory() as directory:
            main_root = Path(directory) / "main"
            worktree_root = Path(directory) / "worktree_project"
            main_root.mkdir()
            subprocess.run(["git", "init"], cwd=main_root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(
                ["git", "-c", "user.name=Nilo Test", "-c", "user.email=nilo@example.test", "commit", "--allow-empty", "-m", "init"],
                cwd=main_root,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            subprocess.run(["git", "worktree", "add", str(worktree_root)], cwd=main_root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            db = worktree_root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(worktree_root)
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "init"])
                exclude_path = subprocess.run(
                    ["git", "rev-parse", "--git-path", "info/exclude"],
                    cwd=worktree_root,
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ).stdout.strip()
            finally:
                os.chdir(previous_cwd)

            resolved_exclude = Path(exclude_path)
            if not resolved_exclude.is_absolute():
                resolved_exclude = worktree_root / resolved_exclude
            self.assertIn("updated: .git/info/exclude", output.getvalue())
            self.assertIn(".nilo/", resolved_exclude.read_text(encoding="utf-8"))

    def test_agent_install_does_not_overwrite_unmanaged_local_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            (root / "CLAUDE.local.md").write_text("human claude notes", encoding="utf-8")
            (root / "AGENTS.override.md").write_text("human codex notes", encoding="utf-8")
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "agent", "install", "--project", "project_test", "--target", "all"])
            finally:
                os.chdir(previous_cwd)

            self.assertEqual("human claude notes", (root / "CLAUDE.local.md").read_text(encoding="utf-8"))
            self.assertEqual("human codex notes", (root / "AGENTS.override.md").read_text(encoding="utf-8"))
            self.assertIn("warning: not overwriting unmanaged local file: CLAUDE.local.md", output.getvalue())
            self.assertIn("warning: not overwriting unmanaged local file: AGENTS.override.md", output.getvalue())
            self.assertTrue((root / ".nilo" / "agent-instructions.md").exists())

    def test_doctor_warns_about_legacy_tracked_agent_block_without_removing_it(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            legacy = root / "AGENTS.md"
            legacy.write_text(
                "before\n\n"
                "<!-- BEGIN NILO MANAGED BLOCK -->\nold block\n<!-- END NILO MANAGED BLOCK -->\n",
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "doctor", "--fix-local-instructions"])
            finally:
                os.chdir(previous_cwd)

            self.assertIn("警告: deprecated Nilo managed block remains in tracked agent file: AGENTS.md", output.getvalue())
            self.assertIn("old block", legacy.read_text(encoding="utf-8"))
            self.assertTrue((root / "AGENTS.override.md").exists())
            self.assertTrue((root / "CLAUDE.local.md").exists())

    def test_doctor_fix_reports_unmanaged_local_file_warning_once(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            (root / "AGENTS.override.md").write_text("human codex notes", encoding="utf-8")
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "doctor", "--fix-local-instructions"])
            finally:
                os.chdir(previous_cwd)

            self.assertNotIn("not overwriting unmanaged local file: AGENTS.override.md", output.getvalue())
            self.assertEqual(output.getvalue().count("警告: unmanaged local instruction file: AGENTS.override.md"), 1)
            self.assertEqual("human codex notes", (root / "AGENTS.override.md").read_text(encoding="utf-8"))

    def test_init_warns_about_legacy_tracked_agent_block_and_does_not_remove_it(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "sample_project"
            root.mkdir()
            (root / "AGENTS.md").write_text(
                "before\n\n"
                "<!-- BEGIN NILO MANAGED BLOCK -->\nold block\n<!-- END NILO MANAGED BLOCK -->\n\n"
                "after\n",
                encoding="utf-8",
            )
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "init"])
            finally:
                os.chdir(previous_cwd)

            self.assertIn("warning: deprecated Nilo managed block remains", output.getvalue())
            self.assertIn("nilo migrate --apply", output.getvalue())
            self.assertIn("old block", (root / "AGENTS.md").read_text(encoding="utf-8"))
            self.assertTrue((root / "AGENTS.override.md").exists())
            self.assertTrue((root / "CLAUDE.local.md").exists())

    def test_migrate_reports_legacy_blocks_without_apply(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            legacy = root / "AGENTS.md"
            legacy.write_text(
                "before\n\n"
                "<!-- BEGIN NILO MANAGED BLOCK -->\nold block\n<!-- END NILO MANAGED BLOCK -->\n\n"
                "after\n",
                encoding="utf-8",
            )
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "migrate"])
            finally:
                os.chdir(previous_cwd)

            self.assertIn("deprecated Nilo managed blocks found", output.getvalue())
            self.assertIn("- AGENTS.md", output.getvalue())
            self.assertIn("Run `nilo migrate --apply`", output.getvalue())
            self.assertIn("old block", legacy.read_text(encoding="utf-8"))
            self.assertFalse((root / "AGENTS.override.md").exists())

    def test_migrate_apply_removes_legacy_blocks_and_refreshes_local_runtime(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "sample_project"
            root.mkdir()
            subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            agents = root / "AGENTS.md"
            claude = root / "CLAUDE.md"
            agents.write_text(
                "before\n\n"
                "<!-- BEGIN NILO MANAGED BLOCK -->\nold agents block\n<!-- END NILO MANAGED BLOCK -->\n\n"
                "after\n",
                encoding="utf-8",
            )
            claude.write_text(
                "<!-- BEGIN NILO MANAGED BLOCK -->\nold claude block\n<!-- END NILO MANAGED BLOCK -->\n\n"
                "keep\n",
                encoding="utf-8",
            )
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "migrate", "--apply"])
            finally:
                os.chdir(previous_cwd)

            self.assertEqual("before\n\nafter\n", agents.read_text(encoding="utf-8"))
            self.assertEqual("keep\n", claude.read_text(encoding="utf-8"))
            self.assertIn("updated: AGENTS.md", output.getvalue())
            self.assertIn("updated: CLAUDE.md", output.getvalue())
            self.assertIn("updated: AGENTS.override.md", output.getvalue())
            self.assertIn("updated: CLAUDE.local.md", output.getvalue())
            self.assertIn("updated: .nilo/agent-instructions.md", output.getvalue())
            self.assertIn(".nilo/", (root / ".git" / "info" / "exclude").read_text(encoding="utf-8"))

    def test_report_import_records_report_without_evidence_check_or_rules(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            report.write_text(REPORT.replace("- src/nilo/cli.py", "- docs/roadmap_proposal.md"), encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                main(["--db", str(db), "instruct", "--task", "task_test"])
                main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(report)])

            store = Store(db)
            checks = store.list_where("evidence_checks", "task_id=?", ("task_test",))
            reports = store.list_where("agent_reports", "task_id=?", ("task_test",))
            failures = store.list_where("failure_logs", "task_id=?", ("task_test",))
            store.close()

            self.assertEqual(checks, [])
            self.assertEqual(len(reports), 1)
            self.assertTrue(failures)
            self.assertTrue(LEGACY_LEARNING_TABLES.isdisjoint(self.table_names(db)))

    def test_report_import_keeps_evidence_submitted_status(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            report.write_text(REPORT.replace("- src/nilo/cli.py", "- docs/roadmap_proposal.md"), encoding="utf-8")

            with redirect_stdout(io.StringIO()), patch("nilo.cli_handlers.workflow.evaluate_evidence", return_value=("evidence_submitted", [], {"ok": True})):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(report)])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "task", "status", "--task", "task_test"])

            self.assertIn("証跡: 未提出", output.getvalue())

    def test_task_list_shows_project_tasks_with_projected_status(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            report.write_text(REPORT, encoding="utf-8")

            with redirect_stdout(io.StringIO()), patch("nilo.cli_handlers.workflow.evaluate_evidence", return_value=("evidence_submitted", [], {"ok": True})):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "project", "create", "Other", "--id", "project_other"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_other",
                        "--id",
                        "task_other",
                        "--title",
                        "別プロジェクトのタスク",
                    ]
                )
                main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(report)])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "task", "list", "--project", "project_test"])

            lines = output.getvalue().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIn("task_test\tagent_reported\timplementation\tmedium\tCLIフローを確認する\t", lines[0])
            self.assertNotIn("task_other", output.getvalue())

    def test_project_status_shows_active_tasks_next_actions_and_verifications(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            db = Path(directory) / "nilo.db"
            script = root / "verify.py"
            script.write_text("print('ok')\n", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_verified",
                        "--title",
                        "検証済みタスク",
                        "--type",
                        "implementation",
                        "--risk",
                        "medium",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_unverified",
                        "--title",
                        "未検証タスク",
                        "--type",
                        "design",
                        "--risk",
                        "low",
                    ]
                )
                main(["--db", str(db), "instruct", "--task", "task_unverified"])
                with patch(
                    "nilo.verification.current_git_snapshot",
                    return_value={"git_head": "head", "git_diff_hash": "hash", "working_tree_dirty": False, "git_status_porcelain": "", "observed_paths": [], "git_available": True},
                ):
                    main(["--db", str(db), "verification", "run", "--task", "task_verified", "--command", f'"{sys.executable}" "{script}"'])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])

            body = output.getvalue()
            self.assertIn("project_id: project_test", body)
            self.assertIn("roadmap_position:", body)
            self.assertIn("work_state: 人間の完了判断待ちです。", body)
            self.assertIn("current_phase: implementation", body)
            self.assertIn("active_tasks:", body)
            self.assertIn("task_verified [verification_passed] implementation medium 検証済みタスク", body)
            self.assertIn("task_unverified [instruction_generated] design low 未検証タスク", body)
            self.assertIn("latest_verification_run: verification_", body)
            self.assertIn("next_actions:", body)
            self.assertIn("review the diff, reported changed files, verification output, and unresolved caveats", body)
            self.assertIn("nilo task complete --task task_verified --reason \"...\" --actor ai", body)
            self.assertIn("add --commit only when you want Nilo to commit the accepted changes", body)
            self.assertIn("unexecuted_verifications:", body)
            self.assertIn("task_unverified: verification run not recorded", body)

            store = Store(db)
            self.assertIsNone(store.latest_for_task("task_completions", "task_verified"))
            store.close()

    def test_next_guides_high_risk_task_through_roadmap_without_blocking_completion(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_large",
                        "--title",
                        "Change status --ai output",
                        "--description",
                        "Update CLI output, README, docs, and tests.",
                        "--acceptance",
                        "status --ai is updated",
                        "--acceptance",
                        "nilo next is updated",
                        "--acceptance",
                        "tests cover the behavior",
                        "--risk",
                        "high",
                    ]
                )

            next_output = io.StringIO()
            with redirect_stdout(next_output):
                main(["--db", str(db), "next", "--project", "project_test"])
            body = next_output.getvalue()
            self.assertIn("この依頼は大きめ", body)
            self.assertIn("作業計画", body)
            self.assertIn("推奨", body)
            self.assertIn("承認", body)
            self.assertIn("Task 化", body)

            report = Path(directory) / "report.md"
            report.write_text(REPORT, encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "report", "import", "--task", "task_large", "--file", str(report)])

            after_report_output = io.StringIO()
            with redirect_stdout(after_report_output):
                main(["--db", str(db), "next", "--project", "project_test"])
            after_report_body = after_report_output.getvalue()
            self.assertIn("検証コマンドを実行して結果を記録してください。", after_report_body)
            self.assertNotIn("大きな作業の可能性", after_report_body)

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "task", "complete", "--task", "task_large", "--reason", "human accepted", "--actor", "human"])
            store = Store(db)
            try:
                self.assertIsNotNone(store.latest_for_task("task_completions", "task_large"))
            finally:
                store.close()

    def test_next_does_not_over_route_small_readme_typo_task(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_small",
                        "--title",
                        "README typo fix",
                        "--acceptance",
                        "Typo is fixed",
                        "--risk",
                        "low",
                    ]
                )

            next_output = io.StringIO()
            with redirect_stdout(next_output):
                main(["--db", str(db), "next", "--project", "project_test"])
            body = next_output.getvalue()
            self.assertIn("作業指示を生成してください。", body)
            self.assertNotIn("roadmap discuss", body)
            self.assertNotIn("大きな作業の可能性", body)

    def test_next_large_work_keywords_do_not_match_substrings(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_substring",
                        "--title",
                        "Fix client latest contest label",
                        "--acceptance",
                        "Label is fixed",
                        "--risk",
                        "low",
                    ]
                )

            next_output = io.StringIO()
            with redirect_stdout(next_output):
                main(["--db", str(db), "next", "--project", "project_test"])
            body = next_output.getvalue()
            self.assertIn("作業指示を生成してください。", body)
            self.assertNotIn("roadmap discuss", body)
            self.assertNotIn("大きな作業の可能性", body)

    def test_next_common_nilo_terms_do_not_route_small_tasks_by_themselves(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_status_typo",
                        "--title",
                        "Fix status output typo",
                        "--acceptance",
                        "Typo is fixed",
                        "--risk",
                        "low",
                    ]
                )

            next_output = io.StringIO()
            with redirect_stdout(next_output):
                main(["--db", str(db), "next", "--project", "project_test"])
            body = next_output.getvalue()
            self.assertIn("作業指示を生成してください。", body)
            self.assertNotIn("roadmap discuss", body)
            self.assertNotIn("大きな作業の可能性", body)

    def test_next_focuses_newly_created_task_after_roadmap_task_plan(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_original",
                        "--title",
                        "Investigate missing task creation after implementation request",
                        "--type",
                        "research",
                        "--risk",
                        "low",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_generated",
                        "--title",
                        "Implement Missing Task Creation After Implementation Request",
                        "--type",
                        "implementation",
                        "--risk",
                        "medium",
                    ]
                )

            next_output = io.StringIO()
            with redirect_stdout(next_output):
                main(["--db", str(db), "next", "--project", "project_test"])
            body = next_output.getvalue()
            self.assertIn("タスク: task_generated", body)
            self.assertNotIn("タスク: task_original", body)

    def test_next_allows_coherent_bug_fix_with_multiple_file_references(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_bug_fix",
                        "--title",
                        "Fix report import path handling",
                        "--description",
                        "Fix one coherent bug where report import normalizes paths inconsistently across parser, handler, and tests.",
                        "--acceptance",
                        "Report import accepts the same path in parser and handler flows",
                        "--acceptance",
                        "Focused tests cover the regression",
                        "--risk",
                        "medium",
                    ]
                )

            next_output = io.StringIO()
            with redirect_stdout(next_output):
                main(["--db", str(db), "next", "--project", "project_test"])
            body = next_output.getvalue()
            self.assertIn("作業指示を生成してください。", body)
            self.assertNotIn("roadmap discuss", body)
            self.assertNotIn("大きな作業の可能性", body)

    def test_next_routes_multi_feature_work_with_broad_roadmap_signals(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_multi_feature",
                        "--title",
                        "Implement CLI roadmap MCP workflow updates",
                        "--description",
                        "Add a multi-feature workflow that changes CLI routing, roadmap state transitions, MCP identity output, and operator guidance.",
                        "--acceptance",
                        "CLI commands expose the new workflow",
                        "--acceptance",
                        "Roadmap state and next actions reflect the workflow",
                        "--acceptance",
                        "MCP identity responses expose the same state",
                        "--risk",
                        "medium",
                    ]
                )

            next_output = io.StringIO()
            with redirect_stdout(next_output):
                main(["--db", str(db), "next", "--project", "project_test"])
            body = next_output.getvalue()
            self.assertIn("この依頼は大きめ", body)
            self.assertIn("作業計画", body)
            self.assertIn("推奨", body)
            self.assertIn("承認", body)

    def test_help_ai_describes_higher_roadmap_threshold(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            main(["help", "ai"])
        body = output.getvalue()
        self.assertIn("A coherent bug fix can proceed as a normal task even when it touches several files.", body)
        self.assertIn("changes DB schema or migrations with broad data or compatibility impact", body)
        self.assertIn("adds or changes CLI commands together with broader workflow behavior", body)

    def test_project_status_allows_clean_verification_task_completion_without_human_prompt(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            script = root / "verify.py"
            report.write_text(REPORT.replace("- src/nilo/cli.py", "- 変更ファイルなし"), encoding="utf-8")
            script.write_text("print('ok')\n", encoding="utf-8")

            with redirect_stdout(io.StringIO()), patch(
                "nilo.cli_handlers.workflow.evaluate_evidence",
                return_value=("evidence_submitted", [], {"ok": True}),
            ), patch(
                "nilo.verification.current_git_snapshot",
                return_value={"git_head": "head", "git_diff_hash": "hash", "working_tree_dirty": False, "git_status_porcelain": "", "observed_paths": [], "git_available": True},
            ):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_verify",
                        "--title",
                        "検証タスク",
                        "--type",
                        "verification",
                    ]
                )
                main(["--db", str(db), "verification", "run", "--task", "task_verify", "--command", f'"{sys.executable}" "{script}"'])
                main(["--db", str(db), "report", "import", "--task", "task_verify", "--file", str(report)])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])

            body = output.getvalue()
            self.assertIn("task_verify [agent_reported] verification medium 検証タスク", body)
            self.assertIn("review the diff, reported changed files, verification output, and unresolved caveats", body)

    def test_project_status_guides_roadmap_setup_when_no_work_is_active(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            handoff = root / "generated_handoff.md"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])

            status_output = io.StringIO()
            with redirect_stdout(status_output):
                    main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])

            status_body = status_output.getvalue()
            expected_guardrail = (
                "no active task; create or select a Nilo task before implementation; "
                'if the user already gave a concrete implementation request, run `nilo start "<short title>" --project project_test` before code edits; '
                "ask the user for the next concrete task or design direction"
            )
            self.assertIn("roadmap_position: roadmap not configured; no open design residue detected", status_body)
            self.assertIn("next_actions:", status_body)
            self.assertIn(expected_guardrail, status_body)
            self.assertNotIn("nilo roadmap discuss --project project_test", status_body)
            self.assertNotIn("nilo roadmap import --project project_test", status_body)
            self.assertNotIn("nilo roadmap accept", status_body)
            self.assertNotIn("nilo roadmap export", status_body)
            self.assertNotIn(".nilo/roadmap/project_test/roadmap_discussion.md", status_body)
            self.assertNotIn(".nilo/roadmap/project_test/roadmap_proposal.md", status_body)
            self.assertNotIn("write a fresh RoadmapProposal to docs/roadmap_proposal.md", status_body)

            summary_output = io.StringIO()
            with redirect_stdout(summary_output):
                main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])

            summary = json.loads(summary_output.getvalue())
            self.assertIn("next_actions", summary)
            self.assertEqual(summary["next_actions"], [expected_guardrail])
            self.assertEqual(summary["roadmap_agent_next_actions"][0]["action_id"], "wait_for_user_direction")
            self.assertNotIn("nilo roadmap", summary["roadmap_agent_next_actions"][0]["command_hint"])

            human_status_output = io.StringIO()
            with redirect_stdout(human_status_output):
                main(["--db", str(db), "project", "status", "--project", "project_test"])
            self.assertIn("作業中のタスクはありません。次に扱う具体的な作業を人間が決めてください。", human_status_output.getvalue())

            with redirect_stdout(io.StringIO()), patch("nilo.project_logic.handson_language", return_value="ja"):
                main(["--db", str(db), "project", "export-handson", "--project", "project_test", "--file", str(handoff)])

            handoff_body = handoff.read_text(encoding="utf-8")
            self.assertIn("## 次のステップ", handoff_body)
            self.assertIn("作業中のタスクはありません。次に扱う具体的な作業を人間が決める", handoff_body)
            self.assertNotIn("nilo roadmap discuss --project project_test", handoff_body)
            self.assertNotIn("nilo roadmap import --project project_test", handoff_body)
            self.assertNotIn("nilo roadmap accept", handoff_body)
            self.assertNotIn("nilo roadmap export", handoff_body)
            self.assertNotIn("edit the roadmap proposal, import it, then accept with nilo roadmap accept", handoff_body)
            self.assertNotIn(".nilo/roadmap/project_test/roadmap_proposal.md", handoff_body)
            self.assertNotIn("docs/roadmap_proposal.md", handoff_body)

    def test_project_summary_shows_counts_history_and_minimal_placeholders(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            script = root / "verify.py"
            script.write_text("print('ok')\n", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_verified",
                        "--title",
                        "検証済みタスク",
                        "--type",
                        "implementation",
                        "--risk",
                        "medium",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_unverified",
                        "--title",
                        "未検証タスク",
                        "--type",
                        "design",
                        "--risk",
                        "low",
                    ]
                )
                main(["--db", str(db), "instruct", "--task", "task_unverified"])
                main(["--db", str(db), "verification", "run", "--task", "task_verified", "--command", f'"{sys.executable}" "{script}"'])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "project", "summary", "--project", "project_test"])

            body = output.getvalue()
            self.assertIn("roadmap_position:", body)
            self.assertIn("work_state: 人間の完了判断待ちです。", body)
            self.assertIn("current_phase: implementation", body)
            self.assertIn("task_status_counts:", body)
            self.assertIn("- instruction_generated: 1", body)
            self.assertIn("- verification_passed: 1", body)
            self.assertIn("recent_history:", body)
            self.assertIn("task_unverified instruction", body)
            self.assertIn("active_tasks:", body)
            self.assertIn("task_verified [verification_passed] implementation medium 検証済みタスク", body)
            self.assertIn("unexecuted_verifications:", body)
            self.assertIn("task_unverified: verification run not recorded", body)
            self.assertIn("commit_mapping:", body)
            self.assertIn("task_verified [insufficient_git_metadata]", body)
            self.assertIn("task_unverified [unmapped]", body)
            self.assertIn("design_residue:", body)
            self.assertNotIn("docs/design.md 18.9 [resolved] implementation:", body)
            self.assertNotIn("project status の最小実装を追加する", body)
            self.assertNotIn("docs/design.md 18.5 [resolved] review:", body)
            self.assertNotIn("quality quick interactive review is implemented", body)
            self.assertNotIn("review import advanced natural language parsing is implemented", body)

            store = Store(db)
            self.assertIsNone(store.latest_for_task("task_completions", "task_verified"))
            store.close()

    def test_project_summary_json_outputs_structured_summary(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            script = root / "verify.py"
            script.write_text("print('ok')\n", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_verified",
                        "--title",
                        "検証済みタスク",
                    ]
                )
                main(["--db", str(db), "verification", "run", "--task", "task_verified", "--command", f'"{sys.executable}" "{script}"'])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])

            summary = json.loads(output.getvalue())
            self.assertEqual(summary["project_id"], "project_test")
            self.assertIn("roadmap_position", summary)
            self.assertIn("work_state", summary)
            self.assertEqual(summary["work_state"], "人間の完了判断待ちです。")
            self.assertIn("human_next_actions", summary)
            self.assertIn("current_phase", summary)
            self.assertIn("task_status_counts", summary)
            self.assertIn("recent_history", summary)
            self.assertIn("event_id", summary["recent_history"][0])
            self.assertIn("active_tasks", summary)
            self.assertIn("unexecuted_verifications", summary)
            self.assertIn("commit_mapping", summary)
            self.assertIn("design_residue", summary)
            self.assertEqual(summary["task_status_counts"]["verification_passed"], 1)
            self.assertEqual(summary["active_tasks"][0]["id"], "task_verified")
            self.assertEqual(summary["active_tasks"][0]["human_status"]["machine_status"], "verification_passed")
            self.assertEqual(summary["commit_mapping"][0]["task_id"], "task_verified")
            self.assertIn(summary["commit_mapping"][0]["status"], {"insufficient_git_metadata", "mapped_candidate", "same_head"})
            self.assertIn("base_commit", summary["commit_mapping"][0])
            self.assertIn("latest_verification_head", summary["commit_mapping"][0])
            self.assertIn("commits", summary["commit_mapping"][0])
            self.assertEqual(summary["design_residue"], [])

    def test_project_summary_design_residue_reads_design_document(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])

            summary = json.loads(output.getvalue())
            self.assertEqual(summary["design_residue"], [])

            store = Store(db)
            self.assertIsNone(store.latest_for_task("task_completions", "task_verified"))
            store.close()

    def test_roadmap_import_accept_status_and_project_summary_position(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """# Phase 2.5 Roadmap Projection

## Intent
project status からロードマップ現在地を読めるようにする。

## Success Criteria
- roadmap_position が accepted commitment を表示する
- work_state と current_phase を分離する

## Non Goals
- ガントチャートは扱わない

## Autonomy Scope
- task 分解
- テスト追加

## Review Gates
- success criteria の変更

## Evidence Policy
- VerificationRun を記録する
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])

            revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
            store = Store(db)
            revision = store.get("roadmap_revisions", revision_id)
            commitment = store.get("roadmap_commitments", revision["proposed_commitment_id"])
            self.assertEqual(revision["status"], "pending")
            self.assertEqual(revision["source_path"], str(proposal))
            self.assertEqual(commitment["status"], "pending")
            self.assertEqual(commitment["title"], "Phase 2.5 Roadmap Projection")
            self.assertEqual(
                commitment["success_criteria"],
                ["roadmap_position が accepted commitment を表示する", "work_state と current_phase を分離する"],
            )
            store.close()

            status_output = io.StringIO()
            with redirect_stdout(status_output):
                main(["--db", str(db), "roadmap", "status", "--project", "project_test"])
            self.assertIn("accepted_commitments:", status_output.getvalue())
            self.assertIn("- none", status_output.getvalue())
            self.assertIn("pending_revisions:", status_output.getvalue())
            self.assertIn(revision_id, status_output.getvalue())
            self.assertIn(f"source_path: {proposal}", status_output.getvalue())

            project_status_output = io.StringIO()
            with redirect_stdout(project_status_output):
                main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])
            project_status_body = project_status_output.getvalue()
            self.assertIn(
                f"roadmap update pending ({revision_id} -> {commitment['id']} Phase 2.5 Roadmap Projection; "
                f"source_path: {proposal}); ask the user whether to adopt or reject the direction",
                project_status_body,
            )
            self.assertNotIn(f"nilo roadmap accept --revision {revision_id}", project_status_body)
            self.assertIn(str(proposal), project_status_body)

            accept_output = io.StringIO()
            with redirect_stdout(accept_output):
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "ロードマップ現在地を先に扱うため", "--actor", "ai"])
            self.assertIn("accepted_by: ai", accept_output.getvalue())

            status_output = io.StringIO()
            with redirect_stdout(status_output):
                main(["--db", str(db), "roadmap", "status", "--project", "project_test"])
            self.assertIn("accepted_commitments:", status_output.getvalue())
            self.assertIn("Phase 2.5 Roadmap Projection", status_output.getvalue())
            self.assertIn("pending_revisions:\n- none", status_output.getvalue())

            summary_output = io.StringIO()
            with redirect_stdout(summary_output):
                main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])
            summary = json.loads(summary_output.getvalue())
            self.assertEqual(summary["roadmap_position"], "accepted commitment: Phase 2.5 Roadmap Projection")
            self.assertEqual(summary["roadmap_commitments"][0]["title"], "Phase 2.5 Roadmap Projection")
            self.assertEqual(summary["roadmap_commitments"][0]["accepted_by"], "ai")
            self.assertEqual(summary["pending_roadmap_revisions"], [])

            roadmap_file = root / "ROADMAP.md"
            export_output = io.StringIO()
            with redirect_stdout(export_output), patch("nilo.project_logic.locale.getlocale", return_value=("en_US", "UTF-8")):
                main(["--db", str(db), "roadmap", "export", "--project", "project_test", "--file", str(roadmap_file)])
            self.assertIn(f"written: {roadmap_file}", export_output.getvalue())
            roadmap_body = roadmap_file.read_text(encoding="utf-8")
            self.assertNotIn(b"\r\n", roadmap_file.read_bytes())
            self.assertIn("# Roadmap", roadmap_body)
            self.assertIn("- Project: Nilo", roadmap_body)
            self.assertIn("### Phase 2.5 Roadmap Projection", roadmap_body)
            self.assertIn("- roadmap_position が accepted commitment を表示する", roadmap_body)
            self.assertNotIn("project_id:", roadmap_body)
            self.assertNotIn("commitment_", roadmap_body)
            self.assertNotIn("roadmap_rev_", roadmap_body)

            japanese_roadmap_file = root / "roadmap_ja.md"
            japanese_export_output = io.StringIO()
            with redirect_stdout(japanese_export_output), patch("nilo.project_logic.locale.getlocale", return_value=("Japanese_Japan", "utf8")):
                main(["--db", str(db), "roadmap", "export", "--project", "project_test", "--file", str(japanese_roadmap_file)])
            self.assertIn(f"written: {japanese_roadmap_file}", japanese_export_output.getvalue())
            japanese_body = japanese_roadmap_file.read_text(encoding="utf-8")
            self.assertIn("# ロードマップ", japanese_body)
            self.assertIn("- 今の方向: 採用済みのロードマップ項目: Phase 2.5 Roadmap Projection", japanese_body)
            self.assertIn("## 現在のロードマップ項目", japanese_body)
            self.assertIn("#### 成功条件", japanese_body)
            self.assertIn("## 次に確認すること", japanese_body)
            self.assertNotIn("# Roadmap", japanese_body)
            self.assertNotIn("project_id:", japanese_body)
            self.assertNotIn("RoadmapCommitment", japanese_body)
            self.assertNotIn("commitment_", japanese_body)
            self.assertNotIn("roadmap_rev_", japanese_body)

            handoff = root / "generated_handoff.md"
            with redirect_stdout(io.StringIO()), patch("nilo.project_logic.handson_language", return_value="ja"):
                main(["--db", str(db), "project", "export-handson", "--project", "project_test", "--file", str(handoff)])
            handoff_body = handoff.read_text(encoding="utf-8")
            self.assertIn("承認済み RoadmapCommitment: Phase 2.5 Roadmap Projection", handoff_body)
            self.assertNotIn("accepted commitment: Phase 2.5 Roadmap Projection", handoff_body)

            with redirect_stdout(io.StringIO()), patch("nilo.project_logic.handson_language", return_value="en"):
                main(["--db", str(db), "project", "export-handson", "--project", "project_test", "--file", str(handoff)])
            english_handoff = handoff.read_text(encoding="utf-8")
            self.assertIn("## Roadmap Position", english_handoff)
            self.assertIn("accepted commitment: Phase 2.5 Roadmap Projection", english_handoff)
            self.assertNotIn("承認済み RoadmapCommitment: Phase 2.5 Roadmap Projection", english_handoff)

    def test_pending_roadmap_revision_blocks_todo_next_actions_with_identity(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """# ID Aware Roadmap

## Success Criteria
- pending revision identity is visible
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
                revision_id = next(
                    line.split(": ", 1)[1]
                    for line in import_output.getvalue().splitlines()
                    if line.startswith("roadmap_revision: ")
                )
                commitment_id = next(
                    line.split(": ", 1)[1]
                    for line in import_output.getvalue().splitlines()
                    if line.startswith("proposed_commitment: ")
                )
                main(["--db", str(db), "todo", "add", "--project", "project_test", "Another broad change"])
                store = Store(db)
                store.update(
                    "todos",
                    store.list_where("todos", "project_id=?", ("project_test",))[0]["id"],
                    {"status": "requires_roadmap"},
                )
                store.close()

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])
            summary = json.loads(output.getvalue())

            self.assertTrue(
                summary["next_actions"][0].startswith(
                    f"roadmap update pending ({revision_id} -> {commitment_id} ID Aware Roadmap; "
                    f"source_path: {proposal})"
                )
            )
            self.assertIn("ask the user whether to adopt or reject the direction", summary["next_actions"][0])
            self.assertIn("requires_roadmap todo", summary["next_actions"][1])

    def test_pending_roadmap_revision_surfaces_human_work_plan_guidance(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """# Human Pending Plan

## Intent
承認待ちの計画を人間が判断できるようにする。

## Autonomy Scope
- CLI 表示を人間向けにする
- AI context に応答方針を入れる

## Success Criteria
- 作業計画が表示される
- 承認後に Task 化することが分かる

## Non Goals
- 内部データモデルは変えない

## Review Gates
- 承認なしで実装を進めない

## Evidence Policy
- focused tests を実行する
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
                main(["--db", str(db), "todo", "add", "--project", "project_test", "Another broad change"])
                store = Store(db)
                try:
                    store.update(
                        "todos",
                        store.list_where("todos", "project_id=?", ("project_test",))[0]["id"],
                        {"status": "requires_roadmap"},
                    )
                finally:
                    store.close()

            status_output = io.StringIO()
            with redirect_stdout(status_output):
                main(["--db", str(db), "roadmap", "status", "--project", "project_test"])
            status_body = status_output.getvalue()
            self.assertIn("この作業は少し大きいので", status_body)
            self.assertIn("作業計画", status_body)
            self.assertIn("確認", status_body)
            self.assertIn("承認", status_body)
            self.assertIn("Task 化", status_body)
            self.assertIn("Human Pending Plan", status_body)
            self.assertIn("作業計画本文", status_body)

            discuss_output = io.StringIO()
            with redirect_stdout(discuss_output):
                main(["--db", str(db), "roadmap", "discuss", "--project", "project_test"])
            discuss_body = discuss_output.getvalue()
            self.assertIn("作業計画: Human Pending Plan", discuss_body)
            self.assertIn("承認すると、この計画をもとに具体的な Nilo Task を作成します。", discuss_body)

            summary_output = io.StringIO()
            with redirect_stdout(summary_output):
                main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])
            summary = json.loads(summary_output.getvalue())
            human_next = summary["human_next_actions"][0]
            self.assertIn("作業計画", human_next)
            self.assertIn("確認", human_next)
            self.assertIn("承認", human_next)
            self.assertIn("Task 化", human_next)
            self.assertIn("requires_roadmap todo", summary["next_actions"][1])

            store = Store(db)
            try:
                ai_context = render_ai_context_text(project_ai_context(store, "project_test", cwd=root))
            finally:
                store.close()
            self.assertIn("ロードマップ承認待ちの応答ルール", ai_context)
            self.assertIn("作業が大きいので、先に作業計画を作った", ai_context)
            self.assertIn("この計画をもとに Task 化します", ai_context)

    def test_task_complete_auto_closes_ready_roadmap_when_all_tasks_completed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """# Auto Close Roadmap

## Intent
全タスク完了時にロードマップも完了にする。

## Success Criteria
- linked task is complete
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
                revision_id = next(
                    line.split(": ", 1)[1]
                    for line in import_output.getvalue().splitlines()
                    if line.startswith("roadmap_revision: ")
                )
                commitment_id = next(
                    line.split(": ", 1)[1]
                    for line in import_output.getvalue().splitlines()
                    if line.startswith("proposed_commitment: ")
                )
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_auto_close",
                        "--title",
                        "Implement auto close",
                        "--commitment",
                        commitment_id,
                    ]
                )

            now = now_iso()
            store = Store(db)
            try:
                store.insert(
                    "roadmap_commitments",
                    {
                        "id": "commitment_unrelated_ready",
                        "project_id": "project_test",
                        "title": "Unrelated Ready Roadmap",
                        "intent": "別の完了候補ロードマップ。",
                        "success_criteria": ["unrelated task is complete"],
                        "non_goals": [],
                        "autonomy_scope": [],
                        "review_gates": [],
                        "evidence_policy": [],
                        "status": "accepted",
                        "accepted_by": "human",
                        "accepted_at": now,
                        "created_at": now,
                    },
                )
                store.insert(
                    "tasks",
                    {
                        "id": "task_unrelated_ready",
                        "project_id": "project_test",
                        "title": "Unrelated ready task",
                        "description": "",
                        "acceptance_criteria": [],
                        "parent_task_id": None,
                        "split_index": None,
                        "task_type": "implementation",
                        "risk_level": "medium",
                        "requires_understanding_check": False,
                        "roadmap_commitment_id": "commitment_unrelated_ready",
                        "roadmap_item_id": "",
                        "status": "planned",
                        "assigned_model_profile": "",
                        "degradation_mode": "normal",
                        "mode": "normal",
                        "base_commit": None,
                        "created_at": now,
                    },
                )
                store.insert(
                    "agent_reports",
                    {
                        "id": "report_unrelated_ready",
                        "task_id": "task_unrelated_ready",
                        "agent": "codex",
                        "claimed_status": "done",
                        "changed_files": [],
                        "body_md": REPORT,
                        "created_at": now,
                    },
                )
                store.insert(
                    "verification_runs",
                    {
                        "id": "verification_unrelated_ready",
                        "task_id": "task_unrelated_ready",
                        "evidence_check_id": None,
                        "source": "nilo_executed",
                        "command": "python -m unittest tests.test_cli",
                        "cwd": str(root),
                        "stdout": "ok",
                        "stderr": "",
                        "exit_code": 0,
                        "timed_out": False,
                        "timeout_seconds": 30,
                        "git_head": "",
                        "git_status_porcelain": "",
                        "git_diff_hash": "",
                        "working_tree_dirty": False,
                        "observed_paths": [],
                        "metadata": {},
                        "started_at": now,
                        "finished_at": now,
                        "created_at": now,
                    },
                )
                store.insert(
                    "task_completions",
                    {
                        "id": "completion_unrelated_ready",
                        "task_id": "task_unrelated_ready",
                        "actor": "human",
                        "completed_by": "human",
                        "completed_snapshot": {},
                        "completion_note": "already accepted",
                        "accepted_verification_run_ids": ["verification_unrelated_ready"],
                        "accepted_review_result_ids": [],
                        "human_decision_note": "already accepted",
                        "completed_with_reservations": False,
                        "completed_at": now,
                        "reason": "already accepted",
                        "created_at": now,
                    },
                )
                store.insert(
                    "agent_reports",
                    {
                        "id": "report_auto_close",
                        "task_id": "task_auto_close",
                        "agent": "codex",
                        "claimed_status": "done",
                        "changed_files": [],
                        "body_md": REPORT,
                        "created_at": now,
                    },
                )
                store.insert(
                    "verification_runs",
                    {
                        "id": "verification_auto_close",
                        "task_id": "task_auto_close",
                        "evidence_check_id": None,
                        "source": "nilo_executed",
                        "command": "python -m unittest tests.test_cli",
                        "cwd": str(root),
                        "stdout": "ok",
                        "stderr": "",
                        "exit_code": 0,
                        "timed_out": False,
                        "timeout_seconds": 30,
                        "git_head": "",
                        "git_status_porcelain": "",
                        "git_diff_hash": "",
                        "working_tree_dirty": False,
                        "observed_paths": [],
                        "metadata": {},
                        "started_at": now,
                        "finished_at": now,
                        "created_at": now,
                    },
                )
            finally:
                store.close()

            complete_output = io.StringIO()
            with redirect_stdout(complete_output):
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "complete",
                        "--task",
                        "task_auto_close",
                        "--reason",
                        "verified",
                        "--actor",
                        "human",
                    ]
                )
            self.assertIn("closed_roadmap_commitments:", complete_output.getvalue())
            self.assertIn(commitment_id, complete_output.getvalue())

            store = Store(db)
            try:
                commitment = store.get("roadmap_commitments", commitment_id)
                unrelated = store.get("roadmap_commitments", "commitment_unrelated_ready")
            finally:
                store.close()
            self.assertEqual(commitment["status"], "closed")
            self.assertEqual(commitment["closed_by"], "human")
            self.assertIn("All linked tasks completed", commitment["closure_reason"])
            self.assertEqual(unrelated["status"], "accepted")

    def test_multiple_accepted_roadmap_commitments_select_incomplete_commitment(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            complete_proposal = root / "complete.md"
            incomplete_proposal = root / "incomplete.md"
            report = root / "report.md"
            script = root / "verify.py"
            complete_proposal.write_text(
                """# Completed Commitment

## Success Criteria
- completed criterion
""",
                encoding="utf-8",
            )
            incomplete_proposal.write_text(
                """# Incomplete Commitment

## Success Criteria
- incomplete criterion
""",
                encoding="utf-8",
            )
            report.write_text(
                """# 完了報告

## 1. 実施内容
completed criterion を満たした。

## 2. 変更ファイル一覧
変更ファイルなし
""",
                encoding="utf-8",
            )
            script.write_text("print('ok')\n", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                first = io.StringIO()
                with redirect_stdout(first):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(complete_proposal)])
                first_revision = next(
                    line.split(": ", 1)[1]
                    for line in first.getvalue().splitlines()
                    if line.startswith("roadmap_revision: ")
                )
                first_commitment = next(
                    line.split(": ", 1)[1]
                    for line in first.getvalue().splitlines()
                    if line.startswith("proposed_commitment: ")
                )
                main(["--db", str(db), "roadmap", "accept", "--revision", first_revision, "--reason", "first"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_complete",
                        "--title",
                        "Implement completed",
                        "--commitment",
                        first_commitment,
                    ]
                )
                main(["--db", str(db), "instruct", "--task", "task_complete"])
                main(["--db", str(db), "report", "import", "--task", "task_complete", "--file", str(report)])
                main(
                    [
                        "--db",
                        str(db),
                        "verification",
                        "run",
                        "--task",
                        "task_complete",
                        "--command",
                        f'"{sys.executable}" "{script}"',
                    ]
                )
                main(["--db", str(db), "task", "complete", "--task", "task_complete", "--reason", "human accepted evidence"])
                second = io.StringIO()
                with redirect_stdout(second):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(incomplete_proposal)])
                second_revision = next(
                    line.split(": ", 1)[1]
                    for line in second.getvalue().splitlines()
                    if line.startswith("roadmap_revision: ")
                )
                second_commitment = next(
                    line.split(": ", 1)[1]
                    for line in second.getvalue().splitlines()
                    if line.startswith("proposed_commitment: ")
                )
                main(["--db", str(db), "roadmap", "accept", "--revision", second_revision, "--reason", "second"])

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])
            summary = json.loads(output.getvalue())

            self.assertEqual(summary["roadmap_agent_state"]["commitment_id"], second_commitment)
            self.assertEqual(summary["roadmap_agent_state"]["work_status"], "task_plan_required")
            self.assertEqual(summary["roadmap_position"], "accepted commitment: Incomplete Commitment")
            self.assertIn(f"roadmap commitment {second_commitment}", summary["next_actions"][0])
            self.assertEqual(summary["roadmap_commitments"][0]["id"], second_commitment)
            self.assertEqual({item["id"] for item in summary["roadmap_commitments"]}, {second_commitment})
            self.assertEqual({item["id"] for item in summary["closed_roadmap_commitments"]}, {first_commitment})

            store = Store(db)
            tasks, statuses = project_tasks_and_statuses(store, "project_test")
            self.assertEqual(
                selected_roadmap_commitment(
                    store,
                    [
                        store.get("roadmap_commitments", first_commitment),
                        store.get("roadmap_commitments", second_commitment),
                    ],
                    tasks,
                    statuses,
                )["id"],
                second_commitment,
            )
            store.close()

    def test_roadmap_agent_state_ignores_unrelated_active_task_for_selected_commitment(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "incomplete.md"
            proposal.write_text(
                """# Incomplete Commitment

## Success Criteria
- incomplete criterion
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                imported = io.StringIO()
                with redirect_stdout(imported):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
                revision_id = next(
                    line.split(": ", 1)[1]
                    for line in imported.getvalue().splitlines()
                    if line.startswith("roadmap_revision: ")
                )
                commitment_id = next(
                    line.split(": ", 1)[1]
                    for line in imported.getvalue().splitlines()
                    if line.startswith("proposed_commitment: ")
                )
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "accepted"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_unrelated", "--title", "Unrelated active task"])

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])
            summary = json.loads(output.getvalue())

            self.assertEqual(summary["roadmap_agent_state"]["commitment_id"], commitment_id)
            self.assertEqual(summary["roadmap_agent_state"]["work_status"], "task_plan_required")
            self.assertEqual(summary["active_tasks"][0]["id"], "task_unrelated")

    def test_roadmap_discuss_warning_uses_revision_source_path_status(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            default_proposal = root / ".nilo" / "roadmap" / "project_test" / "roadmap_proposal.md"
            discussion = root / "discussion.md"
            default_proposal.parent.mkdir(parents=True)
            default_proposal.write_text(
                """# Pending Proposal

## Success Criteria
- source path is linked
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(default_proposal)])
                revision_id = next(
                    line.split(": ", 1)[1]
                    for line in import_output.getvalue().splitlines()
                    if line.startswith("roadmap_revision: ")
                )
                commitment_id = next(
                    line.split(": ", 1)[1]
                    for line in import_output.getvalue().splitlines()
                    if line.startswith("proposed_commitment: ")
                )
                store = Store(db)
                store.update(
                    "roadmap_revisions",
                    revision_id,
                    {"source_path": os.path.relpath(default_proposal, Path.cwd())},
                )
                store.close()

            linked_output = io.StringIO()
            with redirect_stdout(linked_output), patch(
                "nilo.cli.roadmap_proposal_path_for_commitment",
                return_value=str(default_proposal),
            ):
                main(["--db", str(db), "roadmap", "discuss", "--project", "project_test", "--file", str(discussion)])
            self.assertIn(
                f"notice: {default_proposal} already exists and is linked to pending roadmap revision "
                f"{revision_id} for {commitment_id} Pending Proposal",
                linked_output.getvalue(),
            )
            self.assertNotIn("warning:", linked_output.getvalue())

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "accepted"])

            stale_output = io.StringIO()
            with redirect_stdout(stale_output), patch(
                "nilo.cli.roadmap_proposal_path_for_commitment",
                return_value=str(default_proposal),
            ):
                main(["--db", str(db), "roadmap", "discuss", "--project", "project_test", "--file", str(discussion)])
            self.assertIn(f"matching revisions: {revision_id}:accepted", stale_output.getvalue())
            self.assertIn("not linked to a pending roadmap revision", stale_output.getvalue())

    def test_human_roadmap_markdown_masks_internal_ids_without_free_text_rewrites(self) -> None:
        summary = {
            "project_id": "project_test",
            "project_name": "Nilo",
            "roadmap_position": "active task focus: Refactor task_scheduler commitment_123",
            "work_state": "人間の確認待ちです。",
            "current_phase": "implementation",
            "roadmap_commitments": [],
            "pending_roadmap_revisions": [
                {"id": "roadmap_rev_123", "status": "pending", "proposed_commitment_id": "commitment_123"}
            ],
            "active_tasks": [
                {
                    "id": "task_123",
                    "status": "evidence_submitted",
                    "task_type": "implementation",
                    "title": "Refactor task_scheduler commitment_123",
                }
            ],
            "next_actions": [
                "review pending roadmap revision roadmap_rev_123 for commitment_123",
                "task_123: review dirty-tree verification metadata before accepting this task",
            ],
        }

        body = render_human_roadmap_markdown(summary, "ja")

        self.assertIn("- 今の方向: 進行中の作業: 実装", body)
        self.assertIn("- 実装の作業 (作業報告済み)", body)
        self.assertIn("未コミット差分を含む検証記録を確認してから作業を完了する", body)
        self.assertNotIn("task_123", body)
        self.assertNotIn("commitment_123", body)
        self.assertNotIn("roadmap_rev_123", body)
        self.assertNotIn("Refactor 作業_scheduler", body)

    def test_roadmap_adopt_imports_accepts_and_exports_in_one_step(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap_adopt.md"
            roadmap_file = root / "ROADMAP.md"
            proposal.write_text(
                """# One Step Roadmap Adoption

## Intent
Reduce roadmap transaction friction.

## Success Criteria
- adopt creates an accepted commitment
- adopt exports the human roadmap

## Review Gates
- close remains separate
- commit remains separate
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])

            output = io.StringIO()
            with redirect_stdout(output):
                main(
                    [
                        "--db",
                        str(db),
                        "roadmap",
                        "adopt",
                        "--project",
                        "project_test",
                        "--file",
                        str(proposal),
                        "--reason",
                        "human chose this direction",
                        "--roadmap-file",
                        str(roadmap_file),
                    ]
                )

            body = output.getvalue()
            revision_id = next(line.split(": ", 1)[1] for line in body.splitlines() if line.startswith("accepted_revision: "))
            commitment_id = next(line.split(": ", 1)[1] for line in body.splitlines() if line.startswith("accepted_commitment: "))
            self.assertIn("accepted_by: human", body)
            self.assertIn(f"written: {roadmap_file}", body)
            self.assertTrue(roadmap_file.exists())
            self.assertIn("One Step Roadmap Adoption", roadmap_file.read_text(encoding="utf-8"))

            store = Store(db)
            try:
                revision = store.get("roadmap_revisions", revision_id)
                commitment = store.get("roadmap_commitments", commitment_id)
                pending = store.list_where("roadmap_revisions", "project_id=? AND status='pending'", ("project_test",))
            finally:
                store.close()

            self.assertEqual(revision["status"], "accepted")
            self.assertEqual(revision["reason"], "human chose this direction")
            self.assertEqual(revision["source_path"], str(proposal))
            self.assertEqual(commitment["status"], "accepted")
            self.assertEqual(commitment["accepted_by"], "human")
            self.assertEqual(pending, [])

            status_output = io.StringIO()
            with redirect_stdout(status_output):
                main(["--db", str(db), "status", "--project", "project_test", "--verbose"])
            self.assertIn("ロードマップ: accepted commitment: One Step Roadmap Adoption", status_output.getvalue())

    def test_roadmap_adopt_rejects_discussion_context(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            discussion = root / "roadmap_discussion.md"
            roadmap_file = root / "ROADMAP.md"
            discussion.write_text(
                """# Roadmap Discussion Context

## Project

- project_id: project_test
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(
                        [
                            "--db",
                            str(db),
                            "roadmap",
                            "adopt",
                            "--project",
                            "project_test",
                            "--file",
                            str(discussion),
                            "--reason",
                            "human chose this direction",
                            "--roadmap-file",
                            str(roadmap_file),
                        ]
                    )

            store = Store(db)
            try:
                revisions = store.list_where("roadmap_revisions", "project_id=?", ("project_test",))
                commitments = store.list_where("roadmap_commitments", "project_id=?", ("project_test",))
            finally:
                store.close()

            self.assertIn("roadmap adopt rejected discussion context", str(raised.exception))
            self.assertEqual(revisions, [])
            self.assertEqual(commitments, [])
            self.assertFalse(roadmap_file.exists())

    def test_roadmap_import_rejects_discussion_context(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            discussion = root / "roadmap_discussion.md"
            discussion.write_text(
                """# Roadmap Discussion Context

## Project

- project_id: project_test
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(discussion)])

            self.assertIn("roadmap import rejected discussion context", str(raised.exception))

    def test_roadmap_import_reads_proposal_from_stdin_without_source_path(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = """# Optional Artifact Roadmap

## Intent
標準入力から RoadmapProposal を取り込めるようにする。

## Success Criteria
- stdin import が pending revision を作成する
- source_path は空になる

## Non Goals
- ROADMAP.md を廃止しない

## Autonomy Scope
- CLI import の検証

## Review Gates
- file workflow を壊さない

## Evidence Policy
- unit test を追加する
"""

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])

            import_output = io.StringIO()
            with redirect_stdout(import_output), patch("sys.stdin", io.StringIO(proposal)):
                main(["--db", str(db), "roadmap", "import", "--project", "project_test"])

            revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
            store = Store(db)
            revision = store.get("roadmap_revisions", revision_id)
            commitment = store.get("roadmap_commitments", revision["proposed_commitment_id"])
            store.close()

            self.assertEqual(revision["status"], "pending")
            self.assertEqual(revision["source_path"], "")
            self.assertEqual(revision["body_md"], proposal)
            self.assertEqual(commitment["title"], "Optional Artifact Roadmap")
            self.assertEqual(
                commitment["success_criteria"],
                ["stdin import が pending revision を作成する", "source_path は空になる"],
            )

    def test_roadmap_import_rejects_h2_only_proposal_before_pending_revision(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """## Summary

This malformed proposal has no top-level title.

## Success Criteria
- should not be imported
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])

            store = Store(db)
            revisions = store.list_where("roadmap_revisions", "project_id=?", ("project_test",))
            commitments = store.list_where("roadmap_commitments", "project_id=?", ("project_test",))
            store.close()
            self.assertIn("missing top-level # title", str(raised.exception))
            self.assertEqual(revisions, [])
            self.assertEqual(commitments, [])

    def test_roadmap_import_rejects_duplicate_accepted_commitment_before_pending_revision(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            first = root / "first.md"
            second = root / "second.md"
            first.write_text(
                """# Duplicate Roadmap

## Intent
最初の提案。

## Success Criteria
- 同じ成功条件
""",
                encoding="utf-8",
            )
            second.write_text(
                """# Follow-up Title

## Intent
同じ範囲の重複提案。

## Success Criteria
- 同じ成功条件
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                first_output = io.StringIO()
                with redirect_stdout(first_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(first)])
                first_revision = next(line.split(": ", 1)[1] for line in first_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
                main(["--db", str(db), "roadmap", "accept", "--revision", first_revision, "--reason", "最初の承認"])

            with self.assertRaises(SystemExit) as raised:
                second_output = io.StringIO()
                with redirect_stdout(second_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(second)])

            store = Store(db)
            pending_revisions = store.list_where("roadmap_revisions", "project_id=? AND status='pending'", ("project_test",))
            store.close()
            self.assertEqual([], pending_revisions)
            self.assertIn("duplicate roadmap commitment detected before import", str(raised.exception))
            self.assertIn("[accepted]", str(raised.exception))

    def test_roadmap_import_rejects_duplicate_closed_commitment_before_pending_revision(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            first = root / "first.md"
            second = root / "second.md"
            first.write_text(
                """# Closed Roadmap

## Intent
完了済みの提案。

## Success Criteria
- 完了済みの成功条件
""",
                encoding="utf-8",
            )
            second.write_text(
                """# Another Title

## Intent
古い提案の再 import。

## Success Criteria
- 完了済みの成功条件
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                first_output = io.StringIO()
                with redirect_stdout(first_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(first)])
                first_revision = next(line.split(": ", 1)[1] for line in first_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
                main(["--db", str(db), "roadmap", "accept", "--revision", first_revision, "--reason", "最初の承認"])

            store = Store(db)
            accepted = store.list_where("roadmap_commitments", "project_id=? AND status='accepted'", ("project_test",))[0]
            store.update("roadmap_commitments", accepted["id"], {"status": "closed", "closed_by": "ai", "closed_at": "2099-01-01T00:00:00+09:00", "closure_reason": "test"})
            store.close()

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(second)])

            store = Store(db)
            pending_revisions = store.list_where("roadmap_revisions", "project_id=? AND status='pending'", ("project_test",))
            store.close()
            self.assertEqual([], pending_revisions)
            self.assertIn("duplicate roadmap commitment detected before import", str(raised.exception))
            self.assertIn("[closed]", str(raised.exception))

    def test_roadmap_import_rejects_duplicate_rejected_commitment_before_pending_revision(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            first = root / "first.md"
            second = root / "second.md"
            first.write_text(
                """# Rejected Roadmap

## Intent
却下した提案。

## Success Criteria
- 却下済みの成功条件
""",
                encoding="utf-8",
            )
            second.write_text(
                """# Fresh Looking Title

## Intent
却下済み proposal の再 import。

## Success Criteria
- 却下済みの成功条件
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                first_output = io.StringIO()
                with redirect_stdout(first_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(first)])
                first_revision = next(line.split(": ", 1)[1] for line in first_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
                main(["--db", str(db), "roadmap", "reject", "--revision", first_revision, "--reason", "却下する", "--actor", "ai"])

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(second)])

            store = Store(db)
            pending_revisions = store.list_where("roadmap_revisions", "project_id=? AND status='pending'", ("project_test",))
            store.close()
            self.assertEqual([], pending_revisions)
            self.assertIn("duplicate roadmap commitment detected before import", str(raised.exception))
            self.assertIn("[rejected]", str(raised.exception))

    def test_roadmap_discuss_warns_when_default_proposal_file_exists(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            draft_dir = root / ".nilo" / "roadmap" / "project_test"
            draft_dir.mkdir(parents=True)
            (draft_dir / "roadmap_proposal.md").write_text("# Draft Proposal\n", encoding="utf-8")
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "roadmap", "discuss", "--project", "project_test", "--file", ".nilo/roadmap/project_test/roadmap_discussion.md"])
            finally:
                os.chdir(previous_cwd)

            self.assertIn("written: .nilo", output.getvalue())
            self.assertIn("warning: .nilo", output.getvalue())
            self.assertIn("roadmap_proposal.md already exists", output.getvalue())
            self.assertIn("fresh internal RoadmapProposal draft", output.getvalue())

    def test_roadmap_reject_removes_pending_revision_from_status(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """# Roadmap Revision Proposal: Rejectable Proposal

## Summary
誤 import した proposal。

## Proposed Changes
- reject される

## Rationale
不要な提案を取り下げるため。

## Success Criteria
- pending から消える

## Non Goals
- 削除しない

## Autonomy Scope
- CLI 更新

## Review Gates
- pending 表示に残らない

## Evidence Policy
- テストで確認する
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
            revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))

            reject_output = io.StringIO()
            with redirect_stdout(reject_output):
                main(["--db", str(db), "roadmap", "reject", "--revision", revision_id, "--reason", "誤 import のため", "--actor", "ai"])
            self.assertIn(f"rejected_revision: {revision_id}", reject_output.getvalue())
            self.assertIn("rejected_by: ai", reject_output.getvalue())

            store = Store(db)
            revision = store.get("roadmap_revisions", revision_id)
            commitment = store.get("roadmap_commitments", revision["proposed_commitment_id"])
            store.close()
            self.assertEqual(revision["status"], "rejected")
            self.assertEqual(revision["reason"], "誤 import のため")
            self.assertEqual(revision["decided_by"], "ai")
            self.assertTrue(revision["accepted_at"])
            self.assertEqual(commitment["status"], "rejected")
            self.assertEqual(commitment["accepted_by"], "ai")
            self.assertTrue(commitment["accepted_at"])

            roadmap_status = io.StringIO()
            with redirect_stdout(roadmap_status):
                main(["--db", str(db), "roadmap", "status", "--project", "project_test"])
            self.assertIn("pending_revisions:\n- none", roadmap_status.getvalue())
            self.assertNotIn(revision_id, roadmap_status.getvalue())

            project_status = io.StringIO()
            with redirect_stdout(project_status):
                main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])
            self.assertNotIn(f"review pending roadmap revision {revision_id}", project_status.getvalue())

    def test_roadmap_reject_rejects_non_pending_revision(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """# Roadmap Revision Proposal: Accepted Proposal

## Summary
承認済みにする proposal。

## Proposed Changes
- accept される

## Rationale
non-pending reject を確認するため。

## Success Criteria
- reject できない

## Non Goals
- 取り消さない

## Autonomy Scope
- CLI 更新

## Review Gates
- accepted は reject できない

## Evidence Policy
- テストで確認する
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
            revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "承認する", "--actor", "ai"])

            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    main(["--db", str(db), "roadmap", "reject", "--revision", revision_id, "--reason", "取り下げる", "--actor", "ai"])

    def test_store_migrates_roadmap_revision_source_path_column(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE roadmap_revisions (
                  id TEXT PRIMARY KEY,
                  project_id TEXT NOT NULL,
                  proposed_commitment_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  body_md TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  accepted_at TEXT NOT NULL,
                  created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO roadmap_revisions
                (id, project_id, proposed_commitment_id, status, body_md, reason, accepted_at, created_at)
                VALUES ('roadmap_rev_old', 'project_test', 'commitment_old', 'pending', '# Old', '', '', '2026-01-01T00:00:00+00:00')
                """
            )
            conn.commit()
            conn.close()

            store = Store(db)
            revision = store.get("roadmap_revisions", "roadmap_rev_old")
            store.close()

            self.assertEqual(revision["source_path"], "")
            self.assertEqual(revision["decided_by"], "")

    def test_store_backs_up_database_before_schema_column_migration(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE roadmap_revisions (
                  id TEXT PRIMARY KEY,
                  project_id TEXT NOT NULL,
                  proposed_commitment_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  body_md TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  accepted_at TEXT NOT NULL,
                  created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO roadmap_revisions
                (id, project_id, proposed_commitment_id, status, body_md, reason, accepted_at, created_at)
                VALUES ('roadmap_rev_old', 'project_test', 'commitment_old', 'pending', '# Old', '', '', '2026-01-01T00:00:00+00:00')
                """
            )
            conn.commit()
            conn.close()

            store = Store(db)
            store.close()

            backups = list((root / "backups").glob("nilo-*.db"))
            metas = list((root / "backups").glob("nilo-*.db.meta.json"))
            meta = json.loads(metas[0].read_text(encoding="utf-8"))
            backup_conn = sqlite3.connect(backups[0])
            try:
                backup_columns = [row[1] for row in backup_conn.execute("PRAGMA table_info(roadmap_revisions)").fetchall()]
                backup_rows = backup_conn.execute("SELECT id, body_md FROM roadmap_revisions").fetchall()
            finally:
                backup_conn.close()

        self.assertEqual(len(backups), 1)
        self.assertEqual(len(metas), 1)
        self.assertEqual(meta["reason"], "before-migration")
        self.assertNotIn("source_path", backup_columns)
        self.assertEqual(backup_rows, [("roadmap_rev_old", "# Old")])

    def test_store_does_not_create_migration_backup_for_new_or_current_schema_database(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"

            store = Store(db)
            store.close()
            store = Store(db)
            store.close()

            self.assertFalse((root / "backups").exists())

    def test_store_fails_closed_when_pre_migration_backup_fails(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE roadmap_revisions (
                  id TEXT PRIMARY KEY,
                  project_id TEXT NOT NULL,
                  proposed_commitment_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  body_md TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  accepted_at TEXT NOT NULL,
                  created_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
            conn.close()

            with patch("nilo.backup.create_backup", side_effect=BackupError("backup unavailable")):
                with self.assertRaisesRegex(BackupError, "backup unavailable"):
                    Store(db)

    def test_store_migrates_verification_run_source_column(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            conn = sqlite3.connect(db)
            try:
                conn.executescript(
                    """
                    CREATE TABLE projects (
                      id TEXT PRIMARY KEY,
                      name TEXT NOT NULL,
                      tech_stack TEXT NOT NULL,
                      rules TEXT NOT NULL,
                      default_completion_criteria TEXT NOT NULL,
                      available_models TEXT NOT NULL,
                      fallback_models TEXT NOT NULL,
                      requires_local_execution INTEGER NOT NULL,
                      created_at TEXT NOT NULL
                    );
                    CREATE TABLE verification_runs (
                      id TEXT PRIMARY KEY,
                      task_id TEXT NOT NULL,
                      evidence_check_id TEXT,
                      command TEXT NOT NULL,
                      cwd TEXT NOT NULL,
                      stdout TEXT NOT NULL,
                      stderr TEXT NOT NULL,
                      exit_code INTEGER,
                      timed_out INTEGER NOT NULL,
                      timeout_seconds REAL NOT NULL,
                      git_head TEXT,
                      metadata TEXT NOT NULL,
                      started_at TEXT NOT NULL,
                      finished_at TEXT NOT NULL,
                      created_at TEXT NOT NULL
                    );
                    INSERT INTO verification_runs
                    (id, task_id, evidence_check_id, command, cwd, stdout, stderr, exit_code, timed_out, timeout_seconds, git_head, metadata, started_at, finished_at, created_at)
                    VALUES ('verification_old', 'task_old', NULL, 'python -m unittest', '.', 'ok', '', 0, 0, 300.0, 'abc', '{}', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:01+00:00', '2026-01-01T00:00:01+00:00')
                    """
                )
                conn.commit()
            finally:
                conn.close()

            store = Store(db)
            try:
                run = store.get("verification_runs", "verification_old")
            finally:
                store.close()

            self.assertEqual(run["source"], "nilo_executed")

    def test_roadmap_discuss_outputs_and_writes_context(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            output_file = root / "roadmap_context.md"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "ロードマップ表示を改善する",
                    ]
                )
                stdout_output = io.StringIO()
                with redirect_stdout(stdout_output):
                    main(["--db", str(db), "roadmap", "discuss", "--project", "project_test"])
                file_output = io.StringIO()
                with redirect_stdout(file_output):
                    main(["--db", str(db), "roadmap", "discuss", "--project", "project_test", "--file", str(output_file)])

            body = stdout_output.getvalue()
            self.assertIn("# Roadmap Discussion Context", body)
            self.assertIn("roadmap_position:", body)
            self.assertIn("work_state:", body)
            self.assertIn("## Accepted Commitments", body)
            self.assertIn("## Pending Revisions", body)
            self.assertIn("## Active Tasks", body)
            self.assertIn("task_test [planned] implementation medium ロードマップ表示を改善する", body)
            self.assertIn("## Unexecuted Verifications", body)
            self.assertIn("task_test: verification run not recorded", body)
            self.assertIn("## Design Residue", body)
            self.assertIn("## Requested Output", body)
            self.assertEqual(output_file.read_text(encoding="utf-8"), body)
            self.assertIn(f"written: {output_file}", file_output.getvalue())

    def test_roadmap_task_plan_requires_accepted_commitment_and_writes_candidates(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            output_file = root / "task_plan.md"
            proposal.write_text(
                """# Phase 2.5 Roadmap Projection

## Intent
project status からロードマップ現在地を読めるようにする。

## Success Criteria
- roadmap_position が accepted commitment を表示する
- work_state と current_phase を分離する

## Review Gates
- success criteria の変更

## Evidence Policy
- VerificationRun を記録する
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])

            revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
            store = Store(db)
            revision = store.get("roadmap_revisions", revision_id)
            commitment_id = revision["proposed_commitment_id"]
            store.close()

            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "roadmap", "task-plan", "--commitment", commitment_id])

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "タスク候補へ分解するため"])

            stdout_output = io.StringIO()
            with redirect_stdout(stdout_output):
                main(["--db", str(db), "roadmap", "task-plan", "--commitment", commitment_id])
            file_output = io.StringIO()
            with redirect_stdout(file_output):
                main(["--db", str(db), "roadmap", "task-plan", "--commitment", commitment_id, "--file", str(output_file)])

            body = stdout_output.getvalue()
            self.assertIn("# Roadmap Task Plan", body)
            self.assertIn("## Commitment", body)
            self.assertIn("## Review Boundaries", body)
            self.assertIn("success criteria の変更", body)
            self.assertIn("## Task Candidates", body)
            self.assertIn("### 1. Implement Phase 2.5 Roadmap Projection", body)
            self.assertIn("- type: implementation", body)
            self.assertIn("- risk: medium", body)
            self.assertIn("- description: project status からロードマップ現在地を読めるようにする。", body)
            self.assertIn("roadmap_position が accepted commitment を表示する", body)
            self.assertIn("### 2. Verify Phase 2.5 Roadmap Projection", body)
            self.assertIn("- type: verification", body)
            self.assertIn("- VerificationRun を記録する", body)
            self.assertIn("nilo task create --project \"project_test\" --title \"Implement Phase 2.5 Roadmap Projection\"", body)
            self.assertIn(f"--commitment {commitment_id}", body)
            self.assertEqual(output_file.read_text(encoding="utf-8"), body)
            self.assertIn(f"written: {output_file}", file_output.getvalue())

    def test_roadmap_assess_summarizes_commitment_tasks_and_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            self.init_git_with_tags(repo, [])
            repo.joinpath("dirty.txt").write_text("dirty\n", encoding="utf-8")
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            report = root / "report.md"
            script = root / "verify.py"
            proposal.write_text(
                """# Phase 2.5 Roadmap Projection

## Intent
ロードマップ評価を確認する。

## Success Criteria
- accepted commitment の達成状況を確認できる
- 検証証跡を表示できる

## Evidence Policy
- VerificationRun を記録する
""",
                encoding="utf-8",
            )
            report.write_text(REPORT.replace("- src/nilo/cli.py", "- docs/roadmap_proposal.md"), encoding="utf-8")
            script.write_text("print('ok')\n", encoding="utf-8")

            previous_cwd = Path.cwd()
            with redirect_stdout(io.StringIO()):
                try:
                    os.chdir(repo)
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    import_output = io.StringIO()
                    with redirect_stdout(import_output):
                        main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
                    revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
                    store = Store(db)
                    revision = store.get("roadmap_revisions", revision_id)
                    commitment_id = revision["proposed_commitment_id"]
                    store.close()
                    main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "評価するため"])
                    main(
                        [
                            "--db",
                            str(db),
                            "task",
                            "create",
                            "--project",
                            "project_test",
                            "--id",
                            "task_assess",
                            "--title",
                            "Implement Phase 2.5 Roadmap Projection",
                            "--commitment",
                            commitment_id,
                        ]
                    )
                    main(["--db", str(db), "instruct", "--task", "task_assess"])
                    main(["--db", str(db), "report", "import", "--task", "task_assess", "--file", str(report)])
                    main(["--db", str(db), "verification", "run", "--task", "task_assess", "--command", f'"{sys.executable}" "{script}"'])
                finally:
                    os.chdir(previous_cwd)

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "roadmap", "assess", "--project", "project_test"])
            body = output.getvalue()
            self.assertIn("# Roadmap Assessment", body)
            self.assertIn(f"### {commitment_id} Phase 2.5 Roadmap Projection", body)
            self.assertIn("- status: evidence_present", body)
            self.assertIn("- closure_ready: true", body)
            self.assertIn("- [evidence_present] accepted commitment の達成状況を確認できる", body)
            self.assertIn("related_tasks: task_assess", body)
            self.assertIn("latest_verification:", body)
            self.assertIn("(passed)", body)

            summary_output = io.StringIO()
            with redirect_stdout(summary_output):
                main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])
            summary = json.loads(summary_output.getvalue())
            self.assertEqual(summary["roadmap_assessments"][0]["status"], "evidence_present")
            self.assertTrue(summary["roadmap_assessments"][0]["closure_ready"])
            self.assertEqual(summary["roadmap_assessments"][0]["related_tasks"][0]["task_id"], "task_assess")
            self.assertEqual(summary["roadmap_agent_state"]["commitment_id"], commitment_id)
            self.assertEqual(summary["roadmap_agent_state"]["work_status"], "active")
            self.assertEqual(summary["roadmap_agent_state"]["evidence_status"], "complete")
            self.assertEqual(summary["roadmap_agent_state"]["verification_status"], "complete")
            self.assertEqual(summary["roadmap_agent_state"]["closure_status"], "not_ready")
            self.assertNotIn("close_roadmap_commitment", summary["roadmap_agent_state"]["ai_allowed_actions"])
            self.assertNotIn("draft_next_roadmap_proposal", summary["roadmap_agent_state"]["ai_allowed_actions"])
            self.assertIn("continue_active_task", summary["roadmap_agent_state"]["ai_allowed_actions"])
            self.assertEqual(summary["roadmap_agent_state"]["ai_blocked_actions"], [])
            self.assertEqual(summary["roadmap_agent_state"]["recommended_next_action"], "continue_active_task")
            self.assertEqual(summary["roadmap_agent_next_actions"][0]["action_id"], "continue_active_task")
            self.assertEqual(summary["roadmap_agent_next_actions"][0]["actor"], "ai")
            self.assertEqual(summary["roadmap_agent_next_actions"][0]["status"], "allowed")
            self.assertNotIn("nilo roadmap", summary["roadmap_agent_next_actions"][0]["command_hint"])
            self.assertIn("summarize_current_commitment", [item["action_id"] for item in summary["roadmap_agent_next_actions"]])
            self.assertEqual(len(summary["next_actions"]), 1)
            self.assertTrue(summary["next_actions"][0].startswith("task_assess: "))
            self.assertIn("dirty-tree verification metadata", summary["next_actions"][0])
            self.assertNotIn("close commitment", summary["next_actions"][0])
            self.assertNotIn("--actor ai", summary["next_actions"][0])
            self.assertNotIn("nilo roadmap discuss --project project_test", summary["next_actions"][0])
            self.assertNotIn("nilo roadmap import --project project_test", summary["next_actions"][0])
            self.assertNotIn(".nilo/roadmap/project_test/roadmap_proposal.md", summary["next_actions"][0])
            self.assertNotIn(str(proposal), summary["next_actions"][0])
            self.assertNotIn("--file roadmap_proposal.md", summary["next_actions"][0])
            self.assertNotIn("run nilo roadmap assess", summary["next_actions"][0])

            text_summary_output = io.StringIO()
            with redirect_stdout(text_summary_output):
                main(["--db", str(db), "project", "summary", "--project", "project_test"])
            text_summary_body = text_summary_output.getvalue()
            self.assertIn("closure_ready: true", text_summary_body)
            self.assertNotIn("roadmap_agent_state:", text_summary_body)
            self.assertNotIn("roadmap_agent_next_actions:", text_summary_body)
            self.assertNotIn("action_id: close_roadmap_commitment", text_summary_body)
            self.assertNotIn("--actor ai", text_summary_body)
            self.assertIn("task_assess:", text_summary_body)
            self.assertIn("dirty-tree verification metadata", text_summary_body)

            status_output = io.StringIO()
            with redirect_stdout(status_output):
                main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])
            status_body = status_output.getvalue()
            self.assertNotIn("roadmap_agent_state:", status_body)
            self.assertNotIn("roadmap_agent_next_actions:", status_body)
            self.assertNotIn("action_id: close_roadmap_commitment", status_body)
            self.assertNotIn("nilo roadmap discuss --project project_test", status_body)
            self.assertNotIn("nilo roadmap import --project project_test", status_body)
            self.assertNotIn(".nilo/roadmap/project_test/roadmap_proposal.md", status_body)
            self.assertNotIn(str(proposal), status_body)
            self.assertNotIn("close commitment", status_body)
            self.assertIn("dirty-tree verification metadata", status_body)
            self.assertNotIn("run nilo roadmap assess --project project_test for final human review", status_body)

            roadmap_status_output = io.StringIO()
            with redirect_stdout(roadmap_status_output):
                main(["--db", str(db), "roadmap", "status", "--project", "project_test"])
            roadmap_status_body = roadmap_status_output.getvalue()
            self.assertIn("roadmap_agent_state:", roadmap_status_body)
            self.assertIn("roadmap_agent_next_actions:", roadmap_status_body)
            self.assertIn("closure_status: not_ready", roadmap_status_body)
            self.assertNotIn("action_id: close_roadmap_commitment", roadmap_status_body)
            self.assertNotIn("--actor ai", roadmap_status_body)
            self.assertIn("action_id: continue_active_task", roadmap_status_body)

    def test_project_status_guides_task_plan_for_accepted_commitment_without_tasks(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """# Phase 2.5 Roadmap Projection

## Success Criteria
- task 候補を作れる
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
                revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
                store = Store(db)
                revision = store.get("roadmap_revisions", revision_id)
                commitment_id = revision["proposed_commitment_id"]
                store.close()
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "次へ進めるため"])

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])
            body = output.getvalue()
            self.assertIn("roadmap_position: accepted commitment: Phase 2.5 Roadmap Projection", body)
            self.assertNotIn("roadmap_agent_state:", body)
            self.assertNotIn("roadmap_agent_next_actions:", body)
            self.assertNotIn("action_id: create_tasks_from_commitment", body)
            self.assertNotIn(f"nilo roadmap task-plan --commitment {commitment_id}", body)
            self.assertNotIn(f"create tasks from accepted commitment {commitment_id}", body)
            self.assertIn(
                "no active task; create or select a Nilo task before implementation; "
                'if the user already gave a concrete implementation request, run `nilo start "<short title>" --project project_test` before code edits; '
                f"ask the user for the next concrete task within roadmap commitment {commitment_id}",
                body,
            )

    def test_roadmap_close_marks_closure_ready_commitment_closed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            report = root / "report.md"
            script = root / "verify.py"
            proposal.write_text(
                """# Phase 3.4 Human Roadmap Closure Command

## Success Criteria
- close 済み commitment を表示できる
""",
                encoding="utf-8",
            )
            report.write_text(
                """# 完了報告

## 1. 実施内容
close 済み commitment を表示できるようにした。

## 2. 変更ファイル一覧
変更ファイルなし
""",
                encoding="utf-8",
            )
            script.write_text("print('ok')\n", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
                revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
                store = Store(db)
                revision = store.get("roadmap_revisions", revision_id)
                commitment_id = revision["proposed_commitment_id"]
                store.close()
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "closure を確認するため"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_close", "--title", "Implement closure", "--commitment", commitment_id])
                main(["--db", str(db), "instruct", "--task", "task_close"])
                main(["--db", str(db), "report", "import", "--task", "task_close", "--file", str(report)])
                main(["--db", str(db), "verification", "run", "--task", "task_close", "--command", f'"{sys.executable}" "{script}"'])

            close_output = io.StringIO()
            with redirect_stdout(close_output):
                main(["--db", str(db), "roadmap", "close", "--commitment", commitment_id, "--reason", "ai closed roadmap commitment", "--actor", "ai"])
            self.assertIn(f"closed_commitment: {commitment_id}", close_output.getvalue())
            self.assertIn("closed_by: ai", close_output.getvalue())

            roadmap_output = io.StringIO()
            with redirect_stdout(roadmap_output):
                main(["--db", str(db), "roadmap", "status", "--project", "project_test"])
            roadmap_body = roadmap_output.getvalue()
            self.assertIn("accepted_commitments:", roadmap_body)
            self.assertIn("- none", roadmap_body)
            self.assertIn("closed_commitments:", roadmap_body)
            self.assertIn(f"- {commitment_id} Phase 3.4 Human Roadmap Closure Command", roadmap_body)
            self.assertIn("closure_reason: ai closed roadmap commitment", roadmap_body)

            summary_output = io.StringIO()
            with redirect_stdout(summary_output):
                main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])
            summary = json.loads(summary_output.getvalue())
            self.assertEqual(summary["roadmap_commitments"], [])
            self.assertEqual(summary["closed_roadmap_commitments"][0]["id"], commitment_id)
            self.assertEqual(summary["closed_roadmap_commitments"][0]["closed_by"], "ai")
            self.assertEqual(summary["closed_roadmap_commitments"][0]["closure_reason"], "ai closed roadmap commitment")
            self.assertTrue(summary["closed_roadmap_commitments"][0]["closed_at"])
            self.assertIsNone(summary["roadmap_agent_state"])

            status_output = io.StringIO()
            with redirect_stdout(status_output):
                main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])
            status_body = status_output.getvalue()
            self.assertNotIn("roadmap_agent_state:", status_body)
            self.assertIn("- none", status_body)
            self.assertNotIn("accepted commitment: Phase 3.4 Human Roadmap Closure Command", status_body)

    def test_roadmap_close_rejects_non_closure_ready_commitment(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """# Phase 3.4 Human Roadmap Closure Command

## Success Criteria
- close は closure-ready だけ許可する
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
                revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
                store = Store(db)
                revision = store.get("roadmap_revisions", revision_id)
                commitment_id = revision["proposed_commitment_id"]
                store.close()
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "closure rejection を確認するため"])

            with self.assertRaises(SystemExit) as raised:
                main(["--db", str(db), "roadmap", "close", "--commitment", commitment_id, "--reason", "too early"])
            self.assertIn("not closure-ready", str(raised.exception))

    def test_roadmap_assess_marks_related_test_detected_for_simple_mapping(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """# Phase 3 Diff-aware Verification Seed

## Success Criteria
- diff-aware assessment を表示できる
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
                revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
                store = Store(db)
                revision = store.get("roadmap_revisions", revision_id)
                commitment_id = revision["proposed_commitment_id"]
                store.close()
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "diff-aware を確認するため"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_diff",
                        "--title",
                        "Implement Phase 3 Diff-aware Verification Seed",
                        "--commitment",
                        commitment_id,
                    ]
                )

            store = Store(db)
            store.insert(
                "agent_reports",
                {
                    "id": "report_diff",
                    "task_id": "task_diff",
                    "agent": "codex",
                    "claimed_status": "done",
                    "changed_files": ["src/nilo/cli.py"],
                    "body_md": "report",
                    "created_at": "2026-01-01T00:00:01+00:00",
                },
            )
            store.insert(
                "verification_runs",
                {
                    "id": "verification_diff",
                    "task_id": "task_diff",
                    "evidence_check_id": None,
                    "command": f'"{sys.executable}" -m unittest tests.test_cli',
                    "cwd": str(root),
                    "stdout": "",
                    "stderr": "",
                    "exit_code": 0,
                    "timed_out": False,
                    "timeout_seconds": 10.0,
                    "git_head": "abc",
                    "metadata": {"working_tree_available": True, "working_tree_dirty": False, "working_tree_files": []},
                    "started_at": "2026-01-01T00:00:02+00:00",
                    "finished_at": "2026-01-01T00:00:03+00:00",
                    "created_at": "2026-01-01T00:00:03+00:00",
                },
            )
            store.close()

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "roadmap", "assess", "--project", "project_test"])
            body = output.getvalue()
            self.assertIn("- status: evidence_present", body)
            self.assertIn("- closure_ready: true", body)
            self.assertIn("diff_verification: related_test_detected", body)
            self.assertIn("matched_tests: src/nilo/cli.py -> tests/test_cli.py", body)

    def test_roadmap_assess_marks_missing_related_test_as_human_review(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """# Phase 3 Diff-aware Verification Seed

## Success Criteria
- missing test を human review に倒せる
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
                revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
                store = Store(db)
                revision = store.get("roadmap_revisions", revision_id)
                commitment_id = revision["proposed_commitment_id"]
                store.close()
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "diff-aware を確認するため"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_diff",
                        "--title",
                        "Implement Phase 3 Diff-aware Verification Seed",
                        "--commitment",
                        commitment_id,
                    ]
                )

            store = Store(db)
            store.insert(
                "agent_reports",
                {
                    "id": "report_diff",
                    "task_id": "task_diff",
                    "agent": "codex",
                    "claimed_status": "done",
                    "changed_files": ["src/nilo/cli.py"],
                    "body_md": "report",
                    "created_at": "2026-01-01T00:00:01+00:00",
                },
            )
            store.insert(
                "verification_runs",
                {
                    "id": "verification_diff",
                    "task_id": "task_diff",
                    "evidence_check_id": None,
                    "command": f'"{sys.executable}" -m unittest tests.test_guard',
                    "cwd": str(root),
                    "stdout": "",
                    "stderr": "",
                    "exit_code": 0,
                    "timed_out": False,
                    "timeout_seconds": 10.0,
                    "git_head": "abc",
                    "metadata": {"working_tree_available": True, "working_tree_dirty": False, "working_tree_files": []},
                    "started_at": "2026-01-01T00:00:02+00:00",
                    "finished_at": "2026-01-01T00:00:03+00:00",
                    "created_at": "2026-01-01T00:00:03+00:00",
                },
            )
            store.close()

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "roadmap", "assess", "--project", "project_test"])
            body = output.getvalue()
            self.assertIn("- status: needs_human_review", body)
            self.assertIn("- closure_ready: false", body)
            self.assertIn("diff_verification: needs_human_review", body)
            self.assertIn("missing_tests: src/nilo/cli.py -> tests/test_cli.py", body)

    def test_roadmap_summary_explains_diff_review_as_human_wait(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """# Phase 4 Human-readable Status

## Success Criteria
- human summary を表示できる
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
                revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
                store = Store(db)
                revision = store.get("roadmap_revisions", revision_id)
                commitment_id = revision["proposed_commitment_id"]
                store.close()
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "human summary を確認するため"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_diff",
                        "--title",
                        "Implement Phase 4 Human-readable Status",
                        "--commitment",
                        commitment_id,
                    ]
                )

            store = Store(db)
            store.insert(
                "agent_reports",
                {
                    "id": "report_diff",
                    "task_id": "task_diff",
                    "agent": "codex",
                    "claimed_status": "done",
                    "changed_files": ["src/nilo/cli.py"],
                    "body_md": "report",
                    "created_at": "2026-01-01T00:00:01+00:00",
                },
            )
            store.insert(
                "verification_runs",
                {
                    "id": "verification_diff",
                    "task_id": "task_diff",
                    "evidence_check_id": None,
                    "command": f'"{sys.executable}" -m unittest tests.test_guard',
                    "cwd": str(root),
                    "stdout": "",
                    "stderr": "",
                    "exit_code": 0,
                    "timed_out": False,
                    "timeout_seconds": 10.0,
                    "git_head": "abc",
                    "metadata": {"working_tree_available": True, "working_tree_dirty": False, "working_tree_files": []},
                    "started_at": "2026-01-01T00:00:02+00:00",
                    "finished_at": "2026-01-01T00:00:03+00:00",
                    "created_at": "2026-01-01T00:00:03+00:00",
                },
            )
            store.close()

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "roadmap", "summary", "--project", "project_test"])
            body = output.getvalue()
            self.assertIn("# 現在の状態", body)
            self.assertIn("次に判断すること", body)
            self.assertIn("テスト失敗ではありません", body)
            self.assertIn("人間確認待ち", body)
            self.assertIn("変更ファイルとテストコマンド", body)
            self.assertIn("task_diff", body)

    def test_roadmap_assess_treats_unittest_discover_start_dir_as_broad_suite(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """# Phase 3 Diff-aware Verification Seed

## Success Criteria
- broad unittest discover を related test として扱える
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
                revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
                store = Store(db)
                revision = store.get("roadmap_revisions", revision_id)
                commitment_id = revision["proposed_commitment_id"]
                store.close()
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "diff-aware を確認するため"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_diff",
                        "--title",
                        "Implement Phase 3 Diff-aware Verification Seed",
                        "--commitment",
                        commitment_id,
                    ]
                )

            store = Store(db)
            store.insert(
                "agent_reports",
                {
                    "id": "report_diff",
                    "task_id": "task_diff",
                    "agent": "codex",
                    "claimed_status": "done",
                    "changed_files": ["src/nilo/cli.py"],
                    "body_md": "report",
                    "created_at": "2026-01-01T00:00:01+00:00",
                },
            )
            store.insert(
                "verification_runs",
                {
                    "id": "verification_diff",
                    "task_id": "task_diff",
                    "evidence_check_id": None,
                    "command": f'"{sys.executable}" -m unittest discover -s tests',
                    "cwd": str(root),
                    "stdout": "",
                    "stderr": "",
                    "exit_code": 0,
                    "timed_out": False,
                    "timeout_seconds": 10.0,
                    "git_head": "abc",
                    "metadata": {"working_tree_available": True, "working_tree_dirty": False, "working_tree_files": []},
                    "started_at": "2026-01-01T00:00:02+00:00",
                    "finished_at": "2026-01-01T00:00:03+00:00",
                    "created_at": "2026-01-01T00:00:03+00:00",
                },
            )
            store.close()

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "roadmap", "assess", "--project", "project_test"])
            body = output.getvalue()
            self.assertIn("- status: evidence_present", body)
            self.assertIn("- closure_ready: true", body)
            self.assertIn("diff_verification: related_test_detected", body)
            self.assertIn("matched_tests: src/nilo/cli.py -> tests/test_cli.py", body)

    def test_roadmap_assess_marks_unknown_test_mapping_as_human_review(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            proposal = root / "roadmap.md"
            proposal.write_text(
                """# Phase 3 Diff-aware Verification Seed

## Success Criteria
- unknown mapping を human review に倒せる
""",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                import_output = io.StringIO()
                with redirect_stdout(import_output):
                    main(["--db", str(db), "roadmap", "import", "--project", "project_test", "--file", str(proposal)])
                revision_id = next(line.split(": ", 1)[1] for line in import_output.getvalue().splitlines() if line.startswith("roadmap_revision: "))
                store = Store(db)
                revision = store.get("roadmap_revisions", revision_id)
                commitment_id = revision["proposed_commitment_id"]
                store.close()
                main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "diff-aware を確認するため"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_diff",
                        "--title",
                        "Implement Phase 3 Diff-aware Verification Seed",
                        "--commitment",
                        commitment_id,
                    ]
                )

            store = Store(db)
            store.insert(
                "agent_reports",
                {
                    "id": "report_diff",
                    "task_id": "task_diff",
                    "agent": "codex",
                    "claimed_status": "done",
                    "changed_files": ["src/nilo/__init__.py"],
                    "body_md": "report",
                    "created_at": "2026-01-01T00:00:01+00:00",
                },
            )
            store.insert(
                "verification_runs",
                {
                    "id": "verification_diff",
                    "task_id": "task_diff",
                    "evidence_check_id": None,
                    "command": f'"{sys.executable}" -m unittest tests.test_cli',
                    "cwd": str(root),
                    "stdout": "",
                    "stderr": "",
                    "exit_code": 0,
                    "timed_out": False,
                    "timeout_seconds": 10.0,
                    "git_head": "abc",
                    "metadata": {"working_tree_available": True, "working_tree_dirty": False, "working_tree_files": []},
                    "started_at": "2026-01-01T00:00:02+00:00",
                    "finished_at": "2026-01-01T00:00:03+00:00",
                    "created_at": "2026-01-01T00:00:03+00:00",
                },
            )
            store.close()

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "roadmap", "assess", "--project", "project_test"])
            body = output.getvalue()
            self.assertIn("- status: needs_human_review", body)
            self.assertIn("- closure_ready: false", body)
            self.assertIn("diff_verification: needs_human_review", body)
            self.assertIn("diff_reason: no simple source/test mapping for changed source files", body)
            self.assertIn("unknown_files: src/nilo/__init__.py", body)

    def test_project_summary_recent_history_includes_multiple_events_for_same_task(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            script = root / "verify.py"
            script.write_text("print('ok')\n", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "履歴を確認する",
                    ]
                )
                main(["--db", str(db), "instruct", "--task", "task_test"])
                main(["--db", str(db), "verification", "run", "--task", "task_test", "--command", f'"{sys.executable}" "{script}"'])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])

            summary = json.loads(output.getvalue())
            task_events = [item for item in summary["recent_history"] if item["task_id"] == "task_test"]
            event_names = {item["event"] for item in task_events}
            self.assertGreaterEqual(len(task_events), 3)
            self.assertIn("task_created", event_names)
            self.assertIn("instruction", event_names)
            self.assertIn("verification_run", event_names)
            self.assertTrue(all("event_id" in item for item in task_events))

    def test_project_summary_commit_mapping_uses_base_and_verification_head(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            script = root / "verify.py"
            script.write_text("print('ok')\n", encoding="utf-8")

            commits = [{"hash": "abc123", "subject": "Implement mapping"}]
            with redirect_stdout(io.StringIO()), patch("nilo.cli_handlers.workflow.head_commit", return_value="base123"), patch(
                "nilo.snapshot.head_commit", return_value="head456"
            ), patch("nilo.project_logic.git_commit_log", return_value=commits):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "コミット対応を確認する",
                    ]
                )
                main(["--db", str(db), "instruct", "--task", "task_test"])
                main(["--db", str(db), "verification", "run", "--task", "task_test", "--command", f'"{sys.executable}" "{script}"'])
                text_output = io.StringIO()
                with redirect_stdout(text_output):
                    main(["--db", str(db), "project", "summary", "--project", "project_test"])
                json_output = io.StringIO()
                with redirect_stdout(json_output):
                    main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])

            self.assertIn("task_test [mapped_candidate] base_commit=base123 latest_verification_head=head456", text_output.getvalue())
            self.assertIn("abc123 Implement mapping", text_output.getvalue())
            summary = json.loads(json_output.getvalue())
            mapping = summary["commit_mapping"][0]
            self.assertEqual(mapping["task_id"], "task_test")
            self.assertEqual(mapping["base_commit"], "base123")
            self.assertEqual(mapping["latest_verification_head"], "head456")
            self.assertEqual(mapping["commits"], commits)
            self.assertEqual(mapping["status"], "mapped_candidate")

    def test_project_summary_commit_mapping_marks_multiple_commits_ambiguous(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            script = root / "verify.py"
            script.write_text("print('ok')\n", encoding="utf-8")
            commits = [
                {"hash": "abc123", "subject": "First commit"},
                {"hash": "def456", "subject": "Second commit"},
            ]

            with redirect_stdout(io.StringIO()), patch("nilo.cli_handlers.workflow.head_commit", return_value="base123"), patch(
                "nilo.snapshot.head_commit", return_value="head456"
            ), patch("nilo.project_logic.git_commit_log", return_value=commits):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "曖昧なコミット対応を確認する",
                    ]
                )
                main(["--db", str(db), "instruct", "--task", "task_test"])
                main(["--db", str(db), "verification", "run", "--task", "task_test", "--command", f'"{sys.executable}" "{script}"'])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])

            mapping = json.loads(output.getvalue())["commit_mapping"][0]
            self.assertEqual(mapping["status"], "ambiguous")
            self.assertEqual(mapping["commits"], commits)

    def test_project_summary_commit_mapping_marks_shared_range_ambiguous(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            script = root / "verify.py"
            script.write_text("print('ok')\n", encoding="utf-8")
            commits = [{"hash": "abc123", "subject": "Shared commit"}]

            with redirect_stdout(io.StringIO()), patch("nilo.cli_handlers.workflow.head_commit", return_value="base123"), patch(
                "nilo.snapshot.head_commit", return_value="head456"
            ), patch("nilo.project_logic.git_commit_log", return_value=commits):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                for task_id in ("task_one", "task_two"):
                    main(
                        [
                            "--db",
                            str(db),
                            "task",
                            "create",
                            "--project",
                            "project_test",
                            "--id",
                            task_id,
                            "--title",
                            f"{task_id} の対応を確認する",
                        ]
                    )
                    main(["--db", str(db), "instruct", "--task", task_id])
                    main(["--db", str(db), "verification", "run", "--task", task_id, "--command", f'"{sys.executable}" "{script}"'])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])

            mappings = json.loads(output.getvalue())["commit_mapping"]
            self.assertEqual({mapping["status"] for mapping in mappings}, {"ambiguous"})
            self.assertTrue(all(mapping["commits"] == commits for mapping in mappings))

    def test_project_export_handson_writes_compatible_markdown(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            output_file = root / "generated_handoff.md"
            script = root / "verify.py"
            script.write_text("print('ok')\n", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_verified",
                        "--title",
                        "検証済みタスク",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_unverified",
                        "--title",
                        "未検証タスク",
                        "--type",
                        "design",
                    ]
                )
                main(["--db", str(db), "instruct", "--task", "task_unverified"])
                main(["--db", str(db), "verification", "run", "--task", "task_verified", "--command", f'"{sys.executable}" "{script}"'])
                with patch("nilo.project_logic.handson_language", return_value="ja"):
                    main(["--db", str(db), "project", "export-handson", "--project", "project_test", "--file", str(output_file)])

            body = output_file.read_text(encoding="utf-8")
            self.assertIn("# 作業進捗", body)
            self.assertIn("## ロードマップ現在地", body)
            self.assertIn("## 現在の作業状態", body)
            self.assertIn("人間の完了判断待ちです。", body)
            self.assertIn("## 現在のフェーズ", body)
            self.assertIn("## 進行中タスク", body)
            self.assertIn("task_verified [verification_passed]", body)
            self.assertIn("task_unverified [instruction_generated]", body)
            self.assertIn("## 直近履歴", body)
            self.assertIn("## 未実行検証", body)
            self.assertIn("task_unverified: verification run not recorded", body)
            self.assertIn("## 次のステップ", body)
            self.assertIn("task_verified: 差分、変更ファイル一覧、検証結果、未解決事項を確認する", body)
            self.assertIn("task_verified: 承認する場合は task complete で完了を記録し、コミットも任せる場合だけ --commit を付ける", body)

            store = Store(db)
            self.assertIsNone(store.latest_for_task("task_completions", "task_verified"))
            store.close()

    def test_task_create_records_description_and_acceptance(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "詳細仕様を指示書に反映する",
                        "--description",
                        "タスク説明を保存する。",
                        "--description",
                        "複数行の説明を扱う。",
                        "--acceptance",
                        "指示書に説明が表示される",
                        "--acceptance",
                        "指示書に受け入れ条件が表示される",
                    ]
                )
                status_output = io.StringIO()
                with redirect_stdout(status_output):
                    main(["--db", str(db), "task", "status", "--task", "task_test"])
                instruct_output = io.StringIO()
                with redirect_stdout(instruct_output):
                    main(["--db", str(db), "instruct", "--task", "task_test"])

            store = Store(db)
            task = store.get("tasks", "task_test")
            store.close()
            self.assertEqual(task["description"], "タスク説明を保存する。\n複数行の説明を扱う。")
            self.assertEqual(task["acceptance_criteria"], ["指示書に説明が表示される", "指示書に受け入れ条件が表示される"])
            self.assertIn("説明:", status_output.getvalue())
            self.assertIn("受け入れ条件:", status_output.getvalue())
            self.assertIn("## タスク説明\nタスク説明を保存する。", instruct_output.getvalue())
            self.assertIn("- 指示書に説明が表示される", instruct_output.getvalue())
            self.assertIn(".nilo/reports/task_test.md", instruct_output.getvalue())
            self.assertIn("nilo report import --task task_test --file .nilo/reports/task_test.md", instruct_output.getvalue())

    def test_task_update_updates_description_and_status_output(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "更新対象"])
            update_output = io.StringIO()
            with redirect_stdout(update_output):
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "update",
                        "--task",
                        "task_test",
                        "--description",
                        "更新後の説明",
                        "--description",
                        "追加行",
                    ]
                )
            status_output = io.StringIO()
            with redirect_stdout(status_output):
                main(["--db", str(db), "task", "status", "--task", "task_test"])

            store = Store(db)
            task = store.get("tasks", "task_test")
            store.close()
            self.assertEqual(task["description"], "更新後の説明\n追加行")
            self.assertIn("updated: description", update_output.getvalue())
            self.assertIn("更新後の説明\n追加行", status_output.getvalue())

    def test_task_update_appends_acceptance_without_replacing_existing(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "追記対象",
                        "--acceptance",
                        "既存条件",
                    ]
                )
                main(["--db", str(db), "task", "update", "--task", "task_test", "--append-acceptance", "追加条件"])
            status_output = io.StringIO()
            with redirect_stdout(status_output):
                main(["--db", str(db), "task", "status", "--task", "task_test"])

            store = Store(db)
            task = store.get("tasks", "task_test")
            store.close()
            self.assertEqual(task["acceptance_criteria"], ["既存条件", "追加条件"])
            self.assertIn("- 既存条件", status_output.getvalue())
            self.assertIn("- 追加条件", status_output.getvalue())

    def test_task_update_replaces_acceptance_explicitly(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "置換対象",
                        "--acceptance",
                        "古い条件",
                    ]
                )
                main(["--db", str(db), "task", "update", "--task", "task_test", "--acceptance", "新しい条件"])

            store = Store(db)
            task = store.get("tasks", "task_test")
            store.close()
            self.assertEqual(task["acceptance_criteria"], ["新しい条件"])

    def test_task_update_rejects_missing_task_and_ambiguous_acceptance_mode(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "曖昧指定"])
            with self.assertRaises(SystemExit) as missing:
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "task", "update", "--task", "task_missing", "--description", "missing"])
            with self.assertRaises(SystemExit) as ambiguous:
                with redirect_stdout(io.StringIO()):
                    main(
                        [
                            "--db",
                            str(db),
                            "task",
                            "update",
                            "--task",
                            "task_test",
                            "--acceptance",
                            "置換",
                            "--append-acceptance",
                            "追記",
                        ]
                    )

            self.assertIn("task not found: task_missing", str(missing.exception))
            self.assertIn("use either --acceptance", str(ambiguous.exception))

    def test_latest_status_event_uses_deterministic_tie_break(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            created_at = "2026-06-16T00:00:00+00:00"
            store = Store(db)
            store.insert(
                "tasks",
                {
                    "id": "task_test",
                    "project_id": "project_test",
                    "title": "同時刻イベントを確認する",
                    "description": "",
                    "acceptance_criteria": [],
                    "parent_task_id": None,
                    "split_index": None,
                    "task_type": "implementation",
                    "risk_level": "medium",
                    "requires_understanding_check": False,
                    "status": "planned",
                    "assigned_model_profile": "",
                    "degradation_mode": "normal",
                    "base_commit": None,
                    "created_at": created_at,
                },
            )
            store.insert(
                "agent_reports",
                {
                    "id": "report_test",
                    "task_id": "task_test",
                    "agent": "test",
                    "claimed_status": "reported",
                    "changed_files": [],
                    "body_md": "body",
                    "created_at": created_at,
                },
            )
            latest = store.latest_task_status_event("task_test")
            store.close()
            self.assertEqual(latest["source"], "agent_report")
            self.assertEqual(latest["status"], "agent_reported")

    def test_latest_evidence_check_can_supersede_old_outcome(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            report.write_text(REPORT, encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                main(["--db", str(db), "outcome", "reject", "--task", "task_test", "--reason", "差し戻し"])
                with patch("nilo.cli_handlers.workflow.evaluate_evidence", return_value=("evidence_submitted", [], {"ok": True})):
                    main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(report)])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "task", "status", "--task", "task_test"])

            self.assertIn("状態: 作業報告あり", output.getvalue())
            self.assertIn("証跡: 未提出", output.getvalue())

    def test_report_import_does_not_auto_create_rules(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            report.write_text(REPORT.replace("3 passed", "TODO"), encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(report)])

            store = Store(db)
            failures = store.list_where("failure_logs", "task_id=?", ("task_test",))
            store.close()
            self.assertTrue(failures)
            self.assertTrue(LEGACY_LEARNING_TABLES.isdisjoint(self.table_names(db)))

    def test_rules_derive_command_is_removed(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])

            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "rules", "derive", "prepare", "--project", "project_test"])

            self.assertTrue(LEGACY_LEARNING_TABLES.isdisjoint(self.table_names(db)))

    def test_rules_list_command_is_removed_even_with_legacy_rules(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])

            conn = sqlite3.connect(db)
            try:
                conn.execute("CREATE TABLE derived_rules (id TEXT PRIMARY KEY, project_id TEXT NOT NULL)")
                conn.execute("INSERT INTO derived_rules (id, project_id) VALUES ('rule_existing', 'project_test')")
                conn.commit()
            finally:
                conn.close()

            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "rules", "list", "--project", "project_test"])

    def test_successful_reports_do_not_auto_update_derived_rules(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            bad_report = root / "bad_report.md"
            report.write_text(REPORT, encoding="utf-8")
            bad_report.write_text(REPORT.replace("3 passed", "TODO"), encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "テストログ必須のCLIフローを確認する",
                    ]
                )
                main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(bad_report)])
                main(["--db", str(db), "instruct", "--task", "task_test"])
                with patch("nilo.cli_handlers.workflow.evaluate_evidence", return_value=("evidence_submitted", [], {"ok": True})):
                    for _ in range(5):
                        main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(report)])

            store = Store(db)
            failures = store.list_where("failure_logs", "task_id=?", ("task_test",))
            store.close()
            self.assertTrue(failures)
            self.assertTrue(LEGACY_LEARNING_TABLES.isdisjoint(self.table_names(db)))

    def test_task_create_records_type_and_risk(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "仕様を調査する",
                        "--type",
                        "research",
                        "--risk",
                        "high",
                        "--requires-understanding-check",
                    ]
                )

            store = Store(db)
            task = store.get("tasks", "task_test")
            store.close()
            self.assertEqual(task["task_type"], "research")
            self.assertEqual(task["risk_level"], "high")
            self.assertEqual(task["requires_understanding_check"], 1)

    def test_base_commit_is_recorded_at_instruct_time(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()), patch("nilo.cli_handlers.workflow.head_commit", return_value="create_head"):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )

            store = Store(db)
            created_task = store.get("tasks", "task_test")
            store.close()
            self.assertIsNone(created_task["base_commit"])

            with redirect_stdout(io.StringIO()), patch("nilo.cli_handlers.workflow.head_commit", return_value="instruct_head"):
                main(["--db", str(db), "instruct", "--task", "task_test"])

            store = Store(db)
            instructed_task = store.get("tasks", "task_test")
            store.close()
            self.assertEqual(instructed_task["base_commit"], "instruct_head")

    def test_outcome_accept_records_human_decision(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "outcome",
                        "accept-with-concerns",
                        "--task",
                        "task_test",
                        "--reason",
                        "証跡は揃っているが文言は後で見直す",
                        "--concern",
                        "エラーメッセージの統一感が弱い",
                    ]
                )
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "task", "status", "--task", "task_test"])

            store = Store(db)
            outcome = store.latest_for_task("outcome_reviews", "task_test")
            completion = store.latest_for_task("task_completions", "task_test")
            store.close()
            self.assertIsNone(outcome)
            self.assertEqual(completion["completed_by"], "human")
            self.assertTrue(completion["completed_with_reservations"])
            self.assertIn("エラーメッセージの統一感が弱い", completion["human_decision_note"])
            self.assertTrue(completion["completed_snapshot"]["git_diff_hash"])
            self.assertIn("状態: 人間が完了", output.getvalue())
            self.assertIn("留保付き完了: はい", output.getvalue())

    def test_outcome_reject_records_failure_log_without_status_event(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "outcome",
                        "reject",
                        "--task",
                        "task_test",
                        "--reason",
                        "仕様意図と異なる実装になっている",
                    ]
                )

            store = Store(db)
            task = store.get("tasks", "task_test")
            latest = store.latest_task_status_event("task_test")
            projected = projected_task_status(store, task)
            failures = store.list_where("failure_logs", "task_id=?", ("task_test",))
            outcome = store.latest_for_task("outcome_reviews", "task_test")
            store.close()
            self.assertEqual(projected, "planned")
            self.assertEqual(latest["source"], "task")
            self.assertEqual(latest["status"], "planned")
            self.assertIsNone(outcome)
            self.assertEqual(failures[0]["category"], "human_rejected")
            self.assertTrue(LEGACY_LEARNING_TABLES.isdisjoint(self.table_names(db)))

    def test_quality_quick_records_human_summary(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "quality",
                        "quick",
                        "--task",
                        "task_test",
                        "--summary",
                        "仕様には合っているが既存設計との統一感は弱い",
                        "--issue",
                        "エラー文言定義と統一されていない",
                    ]
                )

            store = Store(db)
            review = store.latest_for_task("quality_reviews", "task_test")
            store.close()
            self.assertEqual(review["reviewer"], "human")
            self.assertEqual(review["scores"], {})
            self.assertEqual(review["issues"], ["エラー文言定義と統一されていない"])

    def test_quality_quick_records_scores(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                quality_output = io.StringIO()
                with redirect_stdout(quality_output):
                    main(
                        [
                            "--db",
                            str(db),
                            "quality",
                            "quick",
                            "--task",
                            "task_test",
                            "--summary",
                            "品質は概ね良い",
                            "--score",
                            "requirement_fit=4",
                            "--score",
                            "scope_control=5",
                        ]
                    )
                status_output = io.StringIO()
                with redirect_stdout(status_output):
                    main(["--db", str(db), "task", "status", "--task", "task_test"])

            store = Store(db)
            review = store.latest_for_task("quality_reviews", "task_test")
            store.close()
            self.assertEqual(review["scores"], {"requirement_fit": 4, "scope_control": 5})
            self.assertIn("quality_scores:", quality_output.getvalue())
            self.assertIn("- requirement_fit: 4", quality_output.getvalue())
            self.assertIn("latest_quality_review:", status_output.getvalue())
            self.assertIn("- scope_control: 5", status_output.getvalue())

    def test_quality_quick_interactive_records_review(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                with patch(
                    "builtins.input",
                    side_effect=["対話レビューで記録した", "懸念がひとつある", "", "requirement_fit=4", ""],
                ):
                    main(["--db", str(db), "quality", "quick", "--task", "task_test", "--interactive"])

            store = Store(db)
            review = store.latest_for_task("quality_reviews", "task_test")
            store.close()
            self.assertEqual(review["summary"], "対話レビューで記録した")
            self.assertEqual(review["issues"], ["懸念がひとつある"])
            self.assertEqual(review["scores"], {"requirement_fit": 4})

    def test_quality_quick_rejects_empty_score_key(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(
                        [
                            "--db",
                            str(db),
                            "quality",
                            "quick",
                            "--task",
                            "task_test",
                            "--summary",
                            "品質は概ね良い",
                            "--score",
                            "=4",
                        ]
                    )
            self.assertIn("score key must not be empty", str(raised.exception))

    def test_quality_quick_strict_scores_rejects_missing_required_score(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(
                        [
                            "--db",
                            str(db),
                            "quality",
                            "quick",
                            "--task",
                            "task_test",
                            "--summary",
                            "品質は概ね良い",
                            "--score",
                            "requirement_fit=4",
                            "--required-score",
                            "requirement_fit",
                            "--required-score",
                            "scope_control",
                            "--strict-scores",
                        ]
                    )
            self.assertIn("missing required quality scores: scope_control", str(raised.exception))

    def test_quality_quick_required_scores_are_non_strict_by_default(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "quality",
                        "quick",
                        "--task",
                        "task_test",
                        "--summary",
                        "品質は概ね良い",
                        "--score",
                        "requirement_fit=4",
                        "--required-score",
                        "scope_control",
                    ]
                )

            store = Store(db)
            review = store.latest_for_task("quality_reviews", "task_test")
            store.close()
            self.assertEqual(review["scores"], {"requirement_fit": 4})

    def test_quality_schema_set_lists_required_scores(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "quality",
                        "schema",
                        "set",
                        "--project",
                        "project_test",
                        "--required-score",
                        "requirement_fit",
                        "--required-score",
                        "scope_control",
                    ]
                )
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "quality", "schema", "list", "--project", "project_test"])

            store = Store(db)
            schema = store.get("quality_score_schemas", "project_test")
            store.close()
            self.assertEqual(schema["required_scores"], ["requirement_fit", "scope_control"])
            self.assertIn("- requirement_fit", output.getvalue())
            self.assertIn("- scope_control", output.getvalue())

    def test_quality_schema_set_replaces_existing_required_scores(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "quality",
                        "schema",
                        "set",
                        "--project",
                        "project_test",
                        "--required-score",
                        "requirement_fit",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "quality",
                        "schema",
                        "set",
                        "--project",
                        "project_test",
                        "--required-score",
                        "scope_control",
                    ]
                )

            store = Store(db)
            schemas = store.list_where("quality_score_schemas", "project_id=?", ("project_test",))
            store.close()
            self.assertEqual(len(schemas), 1)
            self.assertEqual(schemas[0]["required_scores"], ["scope_control"])

    def test_quality_quick_strict_scores_uses_project_schema(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "quality",
                        "schema",
                        "set",
                        "--project",
                        "project_test",
                        "--required-score",
                        "requirement_fit",
                        "--required-score",
                        "scope_control",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(
                        [
                            "--db",
                            str(db),
                            "quality",
                            "quick",
                            "--task",
                            "task_test",
                            "--summary",
                            "品質は概ね良い",
                            "--score",
                            "requirement_fit=4",
                            "--strict-scores",
                        ]
                    )
            self.assertIn("missing required quality scores: scope_control", str(raised.exception))

    def test_quality_quick_strict_scores_combines_schema_and_command_required_scores(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "quality",
                        "schema",
                        "set",
                        "--project",
                        "project_test",
                        "--required-score",
                        "requirement_fit",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(
                        [
                            "--db",
                            str(db),
                            "quality",
                            "quick",
                            "--task",
                            "task_test",
                            "--summary",
                            "品質は概ね良い",
                            "--score",
                            "requirement_fit=4",
                            "--required-score",
                            "scope_control",
                            "--strict-scores",
                        ]
                    )
            self.assertIn("missing required quality scores: scope_control", str(raised.exception))

    def test_quality_autoscore_prepare_outputs_agent_prompt(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "quality",
                        "schema",
                        "set",
                        "--project",
                        "project_test",
                        "--required-score",
                        "requirement_fit",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "採点プロンプトを生成する",
                    ]
                )
            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "quality", "autoscore", "prepare", "--task", "task_test"])

            self.assertIn("# Quality Autoscore 指示", output.getvalue())
            self.assertIn("採点プロンプトを生成する", output.getvalue())
            self.assertIn("- requirement_fit", output.getvalue())

    def test_quality_autoscore_import_validates_schema_and_records_scores(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            autoscore = root / "autoscore.md"
            autoscore.write_text(
                "# QualityReview\n\n"
                "## Summary\n採点結果は良好。\n\n"
                "## Scores\nrequirement_fit: 4\nscope_control: 5\n",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "quality",
                        "schema",
                        "set",
                        "--project",
                        "project_test",
                        "--required-score",
                        "requirement_fit",
                        "--required-score",
                        "scope_control",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "採点結果を取り込む",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "quality",
                        "autoscore",
                        "import",
                        "--task",
                        "task_test",
                        "--file",
                        str(autoscore),
                        "--strict-scores",
                    ]
                )

            store = Store(db)
            review = store.latest_for_task("quality_reviews", "task_test")
            store.close()
            self.assertEqual(review["reviewer"], "ai_autoscore")
            self.assertEqual(review["scores"], {"requirement_fit": 4, "scope_control": 5})

    def test_quality_autoscore_import_rejects_unknown_score_when_schema_exists(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            autoscore = root / "autoscore.md"
            autoscore.write_text("# QualityReview\n\n## Summary\n採点。\n\n## Scores\nunknown_axis: 4\n", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "quality",
                        "schema",
                        "set",
                        "--project",
                        "project_test",
                        "--required-score",
                        "requirement_fit",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "採点結果を取り込む",
                    ]
                )

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(
                        [
                            "--db",
                            str(db),
                            "quality",
                            "autoscore",
                            "import",
                            "--task",
                            "task_test",
                            "--file",
                            str(autoscore),
                        ]
                    )
            self.assertIn("unknown quality scores: unknown_axis", str(raised.exception))

    def test_success_add_command_is_removed(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])

            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    main(
                        [
                            "--db",
                            str(db),
                            "success",
                            "add",
                            "--project",
                            "project_test",
                            "--pattern",
                            "影響範囲が不明な修正では先にresearchタスクを作る",
                        ]
                    )

            self.assertTrue(LEGACY_LEARNING_TABLES.isdisjoint(self.table_names(db)))

    def test_success_list_command_is_removed_even_with_legacy_patterns(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])

            conn = sqlite3.connect(db)
            try:
                conn.execute("CREATE TABLE success_patterns (id TEXT PRIMARY KEY, project_id TEXT NOT NULL)")
                conn.execute("INSERT INTO success_patterns (id, project_id) VALUES ('success_existing', 'project_test')")
                conn.commit()
            finally:
                conn.close()

            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "success", "list", "--project", "project_test"])

    def test_success_pattern_is_not_injected_into_instruction(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを実装する",
                        "--type",
                        "implementation",
                    ]
                )
            conn = sqlite3.connect(db)
            try:
                conn.execute(
                    "CREATE TABLE success_patterns (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, pattern_text TEXT NOT NULL)"
                )
                conn.execute(
                    "INSERT INTO success_patterns (id, project_id, pattern_text) VALUES (?, ?, ?)",
                    ("success_existing", "project_test", "影響範囲が不明な修正では先にresearchタスクを作る"),
                )
                conn.commit()
            finally:
                conn.close()
            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "instruct", "--task", "task_test"])

            self.assertNotIn("## 参考にする成功パターン", output.getvalue())
            self.assertNotIn("影響範囲が不明な修正では先にresearchタスクを作る", output.getvalue())

    def test_task_split_creates_subtasks(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを改善する",
                        "--risk",
                        "high",
                    ]
                )
            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "task", "split", "--task", "task_test"])

            store = Store(db)
            subtasks = store.list_where("tasks", "parent_task_id=?", ("task_test",))
            store.close()
            task_types = {task["task_type"] for task in subtasks}
            self.assertEqual(len(subtasks), 4)
            self.assertEqual(task_types, {"research", "design", "implementation", "verification"})
            implementation = next(task for task in subtasks if task["task_type"] == "implementation")
            self.assertEqual(implementation["requires_understanding_check"], 1)
            self.assertIn("Generated subtasks:", output.getvalue())

    def test_task_complete_records_final_human_completion(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "complete",
                        "--task",
                        "task_test",
                        "--reason",
                        "人間が成果物を確認して完了と判断した",
                    ]
                )
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "task", "status", "--task", "task_test"])

            store = Store(db)
            completion = store.latest_for_task("task_completions", "task_test")
            store.close()
            self.assertEqual(completion["actor"], "human")
            self.assertEqual(completion["reason"], "人間が成果物を確認して完了と判断した")
            self.assertIn("状態: 人間が完了", output.getvalue())

    def test_task_complete_rejects_ai_completion_without_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "AI 完了を確認する",
                    ]
                )

            with self.assertRaises(SystemExit):
                output = io.StringIO()
                with redirect_stdout(output):
                    main(
                        [
                            "--db",
                            str(db),
                            "task",
                            "complete",
                            "--task",
                            "task_test",
                            "--reason",
                            "AI が検証済み成果物を受け入れた",
                            "--actor",
                            "ai",
                        ]
                    )

            store = Store(db)
            completion = store.latest_for_task("task_completions", "task_test")
            store.close()
            self.assertIsNone(completion)

    def test_task_complete_rejects_ai_completion_after_report_only(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            report.write_text(REPORT, encoding="utf-8")
            with redirect_stdout(io.StringIO()), patch("nilo.cli_handlers.workflow.evaluate_evidence", return_value=("evidence_submitted", [], {"ok": True})):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "AI 完了を確認する",
                    ]
                )
                main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(report)])
                with self.assertRaises(SystemExit) as raised:
                    main(
                        [
                            "--db",
                            str(db),
                            "task",
                            "complete",
                            "--task",
                            "task_test",
                            "--reason",
                            "AI が検証済み成果物を受け入れた",
                            "--actor",
                            "ai",
                        ]
                    )

            store = Store(db)
            completion = store.latest_for_task("task_completions", "task_test")
            store.close()
            self.assertIsNone(completion)
            self.assertIn("current verification run", str(raised.exception))

    def test_task_complete_can_record_ai_completion_after_successful_verification(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            script = root / "verify.py"
            script.write_text("print('ok')\n", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "AI 完了を検証で許可する",
                    ]
                )
                main(["--db", str(db), "verification", "run", "--task", "task_test", "--command", f'"{sys.executable}" "{script}"'])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(
                        [
                            "--db",
                            str(db),
                            "task",
                            "complete",
                            "--task",
                            "task_test",
                            "--reason",
                            "AI が成功した検証を確認した",
                            "--actor",
                            "ai",
                        ]
                    )

            self.assertIn("status: completed_by_ai", output.getvalue())

    def test_task_complete_does_not_auto_regenerate_handoff_for_default_db(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["project", "create", "Nilo", "--id", "project_test"])
                    main(
                        [
                            "task",
                            "create",
                            "--project",
                            "project_test",
                            "--id",
                            "task_test",
                            "--title",
                            "handoff を更新する",
                        ]
                    )
                    main(
                        [
                            "task",
                            "complete",
                            "--task",
                            "task_test",
                            "--reason",
                            "人間が成果物を確認して完了と判断した",
                        ]
                    )
            finally:
                os.chdir(previous_cwd)

            handoff = root / "HANDOFF.md"
            self.assertFalse(handoff.exists())
            self.assertNotIn("handoff: regenerated", output.getvalue())

    def test_task_complete_can_commit_accepted_changes(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "承認後コミットを確認する",
                    ]
                )
                output = io.StringIO()
                with patch("nilo.cli.git_changed_files", return_value=["src/nilo/cli.py"]), patch(
                    "nilo.cli.commit_changed_files", return_value=(0, "abc123 Commit message", "")
                ) as commit_mock, redirect_stdout(output):
                    main(
                        [
                            "--db",
                            str(db),
                            "task",
                            "complete",
                            "--task",
                            "task_test",
                            "--reason",
                            "人間が成果物を確認して完了と判断した",
                            "--commit",
                            "--commit-message",
                            "Complete approval guidance",
                        ]
                    )

            commit_mock.assert_called_once()
            self.assertIn("commit: created", output.getvalue())
            self.assertIn("abc123 Commit message", output.getvalue())

    def test_git_changed_files_preserves_porcelain_status_columns(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout=" M src/nilo/cli.py\nA  tests/test_cli.py\nR  old.py -> new.py\n",
            stderr="",
        )
        with patch("nilo.cli.subprocess.run", return_value=completed):
            self.assertEqual(git_changed_files(Path.cwd()), ["new.py", "src/nilo/cli.py", "tests/test_cli.py"])

    def test_review_prepare_outputs_review_only_prompt(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローをレビューする",
                        "--type",
                        "review",
                    ]
                )
            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "review", "prepare", "--task", "task_test"])

            self.assertIn("# レビュー指示", output.getvalue())
            self.assertIn("コード変更は禁止", output.getvalue())

    def test_review_import_records_quality_review(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            review = root / "review.md"
            review.write_text("# QualityReview\n\n## Summary\n設計妥当性に懸念がある。", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローをレビューする",
                    ]
                )
                main(["--db", str(db), "review", "import", "--task", "task_test", "--file", str(review)])

            store = Store(db)
            quality = store.latest_for_task("quality_reviews", "task_test")
            store.close()
            self.assertEqual(quality["reviewer"], "ai_review")
            self.assertIn("設計妥当性に懸念", quality["summary"])

    def test_review_import_parses_issues_and_scores(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            review = root / "review.md"
            review.write_text(
                "# QualityReview\n\n"
                "## Summary\n仕様には合っている。\n\n"
                "## Issues\n- 既存設計との統一感が弱い\n\n"
                "## Scores\nrequirement_fit: 4\nscope_control=5\n",
                encoding="utf-8",
            )
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローをレビューする",
                    ]
                )
                main(["--db", str(db), "review", "import", "--task", "task_test", "--file", str(review)])

            store = Store(db)
            quality = store.latest_for_task("quality_reviews", "task_test")
            store.close()
            self.assertEqual(quality["summary"], "仕様には合っている。")
            self.assertEqual(quality["issues"], ["既存設計との統一感が弱い"])
            self.assertEqual(quality["scores"], {"requirement_fit": 4, "scope_control": 5})

    def test_review_import_parses_labeled_natural_language(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            review = root / "review.md"
            review.write_text(
                "Summary: 仕様には合っている。\n"
                "Issue: 既存設計との統一感が弱い\n"
                "Scores: requirement_fit=4 scope_control=5\n",
                encoding="utf-8",
            )
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローをレビューする",
                    ]
                )
                main(["--db", str(db), "review", "import", "--task", "task_test", "--file", str(review)])

            store = Store(db)
            quality = store.latest_for_task("quality_reviews", "task_test")
            store.close()
            self.assertEqual(quality["summary"], "仕様には合っている。")
            self.assertEqual(quality["issues"], ["既存設計との統一感が弱い"])
            self.assertEqual(quality["scores"], {"requirement_fit": 4, "scope_control": 5})

    def test_review_import_rejects_invalid_score(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            review = root / "review.md"
            review.write_text("# QualityReview\n\n## Summary\n評価。\n\n## Scores\nrequirement_fit: 6\n", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローをレビューする",
                    ]
                )

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "review", "import", "--task", "task_test", "--file", str(review)])
            self.assertIn("score must be 1-5: requirement_fit=6", str(raised.exception))

    def test_review_request_prepare_import_and_status_workflow(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            review = root / "review.md"
            report.write_text(REPORT, encoding="utf-8")
            review.write_text(
                "# ReviewResult\n\n"
                "## Verdict\nchanges_requested\n\n"
                "## Summary\n境界値の確認が不足している。\n\n"
                "## Findings\n"
                "### F1\n"
                "severity: high\n"
                "status: unresolved\n"
                "file: src/nilo/cli.py\n"
                "line: 12\n"
                "blocking: true\n\n"
                "境界値テストが不足している。\n\n"
                "### F2\n"
                "severity: high\n"
                "status: unresolved\n"
                "blocking: false\n\n"
                "明示的に nonblocking とした高 severity finding。\n\n"
                "### F3\n"
                "severity: typo\n"
                "status: unresolved\n\n"
                "不正 severity は medium に正規化する。\n",
                encoding="utf-8",
            )
            verification_result = {
                "command": "python -m unittest discover tests",
                "cwd": str(root),
                "stdout": "ok\n",
                "stderr": "",
                "exit_code": 0,
                "timed_out": False,
                "timeout_seconds": 300.0,
                "git_head": "abc123",
                "metadata": {
                    "secret_issue_count": 0,
                    "secret_issues": [],
                    "runner": "local",
                    "sandbox": "none",
                    "working_tree_dirty": False,
                    "working_tree_files": [],
                    "working_tree_available": True,
                },
                "started_at": "2099-06-17T00:00:00+09:00",
                "finished_at": "2099-06-17T00:00:01+09:00",
                "created_at": "2099-06-17T00:00:01+09:00",
            }

            with redirect_stdout(io.StringIO()), patch("nilo.cli_handlers.workflow.evaluate_evidence", return_value=("evidence_submitted", [], {"ok": True})):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "レビュー依頼を管理する",
                        "--acceptance",
                        "review prepare に acceptance criteria が含まれる",
                    ]
                )
                main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(report)])
                with patch("nilo.cli_handlers.workflow.run_local_verification", return_value=verification_result):
                    main(["--db", str(db), "verification", "run", "--task", "task_test", "--command", "python -m unittest discover tests"])
                register_test_reviewer(db, "codex")
                request_output = io.StringIO()
                with redirect_stdout(request_output):
                    main(
                        [
                            "--db",
                            str(db),
                            "review",
                            "request",
                            "--task",
                            "task_test",
                            "--from",
                            "claude-code",
                            "--to",
                            "codex",
                            "--reason",
                            "別AIレビュー",
                        ]
                    )

            request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))

            status_output = io.StringIO()
            with redirect_stdout(status_output):
                main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])
            self.assertIn(
                f"MCP reviewer worker codex should claim review {request_id} with nilo mcp reviewer-claim or claim_next_review",
                status_output.getvalue(),
            )

            prepare_output = io.StringIO()
            with redirect_stdout(prepare_output):
                main(["--db", str(db), "review", "prepare", "--task", "task_test", "--review", request_id])
            prepare_body = prepare_output.getvalue()
            self.assertIn("# Review Request", prepare_body)
            self.assertIn("review prepare に acceptance criteria が含まれる", prepare_body)
            self.assertIn("## Implementation Report", prepare_body)
            self.assertIn("## Verification History", prepare_body)
            self.assertIn("python -m unittest discover tests", prepare_body)

            import_output = io.StringIO()
            with redirect_stdout(import_output):
                main(["--db", str(db), "review", "import", "--task", "task_test", "--review", request_id, "--file", str(review)])
            self.assertIn("verdict: changes_requested", import_output.getvalue())

            review_status = io.StringIO()
            with redirect_stdout(review_status):
                main(["--db", str(db), "review", "status", "--task", "task_test"])
            self.assertIn(f"{request_id} [completed] claude-code -> codex", review_status.getvalue())
            self.assertIn("[unresolved] high blocking src/nilo/cli.py:12: F1", review_status.getvalue())

            store = Store(db)
            result = store.latest_for_task("review_results", "task_test")
            findings = store.list_where("review_findings", "task_id=?", ("task_test",))
            store.close()
            self.assertEqual(result["verdict"], "changes_requested")
            self.assertEqual(result["reviewer"], "codex")
            by_title = {finding["title"]: finding for finding in findings}
            self.assertEqual(by_title["F1"]["status"], "unresolved")
            self.assertTrue(by_title["F1"]["blocking"])
            self.assertFalse(by_title["F2"]["blocking"])
            self.assertEqual(by_title["F3"]["severity"], "medium")

    def test_mcp_reviewer_start_registers_heartbeats_and_claims_pending_review(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "reviewer worker"])
                    register_test_reviewer(db, "claude-code")
                    request_output = io.StringIO()
                    with redirect_stdout(request_output):
                        main(
                            [
                                "--db",
                                str(db),
                                "review",
                                "request",
                                "--task",
                                "task_test",
                                "--from",
                                "codex",
                                "--to",
                                "claude-code",
                                "--reason",
                                "MCP worker startup",
                            ]
                        )
                request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))

                output = io.StringIO()
                with redirect_stdout(output):
                    main(
                        [
                            "--db",
                            str(db),
                            "mcp",
                            "reviewer-start",
                            "--reviewer",
                            "claude-code",
                            "--project",
                            "project_test",
                        ]
                    )
                store = Store(db)
                try:
                    reviewer = store.list_where("review_reviewers", "reviewer=?", ("claude-code",))[0]
                    request = store.get("review_requests", request_id)
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(reviewer["status"], "available")
        self.assertEqual(request["status"], "requested")
        self.assertIn("revived_review_requests:\n- none", output.getvalue())
        self.assertIn("- none", output.getvalue())
        self.assertIn("next_action: reviewer worker must call claim_next_review via MCP", output.getvalue())
        self.assertNotIn(f"- review_request: {request_id}", output.getvalue())

    def test_mcp_ping_runs_stdio_handshake_and_writes_transcript(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            log_file = root / "mcp_ping.json"
            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "mcp", "ping", "--log-file", str(log_file)])

            body = json.loads(output.getvalue())
            saved = json.loads(log_file.read_text(encoding="utf-8"))

        self.assertTrue(body["ok"])
        self.assertEqual(body["server"]["name"], "nilo")
        self.assertIn("identity", body)
        self.assertEqual(body["identity"]["db_path"], str(db.resolve()))
        self.assertGreater(body["tool_count"], 0)
        self.assertEqual(saved["server"]["name"], "nilo")
        self.assertIn("identity", saved)
        self.assertEqual(saved["transcript"][0]["request"]["method"], "initialize")
        self.assertEqual(saved["transcript"][0]["request"]["params"]["clientInfo"]["name"], "hello-client")
        self.assertEqual(saved["transcript"][0]["response"]["result"]["serverInfo"]["name"], "nilo")
        self.assertEqual(saved["transcript"][2]["request"]["method"], "tools/list")
        self.assertIn("get_status", saved["tool_names"])
        self.assertIn("record_verification", saved["tool_names"])

    def test_mcp_reviewer_start_heartbeats_alias_without_claiming(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "alias reviewer worker"])
                    main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "claude", "--reason", "alias startup"])

                output = io.StringIO()
                with redirect_stdout(output):
                    main(
                        [
                            "--db",
                            str(db),
                            "mcp",
                            "reviewer-start",
                            "--reviewer",
                            "claude",
                            "--project",
                            "project_test",
                        ]
                    )
                store = Store(db)
                try:
                    request = store.latest_for_task("review_requests", "task_test")
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(request["reviewer"], "claude-code")
        self.assertEqual(request["status"], "reviewer_unavailable")
        self.assertIn("reviewer: claude-code", output.getvalue())
        self.assertIn("revived_review_requests:\n- none", output.getvalue())
        self.assertIn("next_action: reviewer worker must call claim_next_review via MCP", output.getvalue())
        self.assertNotIn(f"- review_request: {request['id']}", output.getvalue())

    def test_mcp_reviewer_claim_registers_worker_claims_and_writes_handoff(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "claim reviewer worker"])
                    main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "claude", "--reason", "claim startup"])

                output = io.StringIO()
                with redirect_stdout(output):
                    main(
                        [
                            "--db",
                            str(db),
                            "mcp",
                            "reviewer-claim",
                            "--reviewer",
                            "claude",
                            "--project",
                            "project_test",
                            "--write-default",
                        ]
                    )
                store = Store(db)
                try:
                    request = store.latest_for_task("review_requests", "task_test")
                    reviewer = store.list_where("review_reviewers", "reviewer=?", ("claude-code",))[0]
                finally:
                    store.close()
                prompt_path = root / ".nilo" / "reviews" / f"{request['id']}_prompt.md"
                template_path = root / ".nilo" / "reviews" / f"{request['id']}.md"
                prompt_exists = prompt_path.exists()
                template_exists = template_path.exists()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(request["reviewer"], "claude-code")
        self.assertEqual(request["status"], "claimed")
        self.assertEqual(reviewer["metadata"], {"worker_path": "nilo mcp reviewer-claim"})
        self.assertTrue(prompt_exists)
        self.assertTrue(template_exists)
        self.assertIn("reviewer: claude-code", output.getvalue())
        self.assertIn(f"- review_request: {request['id']}", output.getvalue())
        self.assertIn("next_action: review prompt_md and import_review_result through MCP", output.getvalue())

    def test_review_request_transaction_completes_through_mcp_reviewer_worker(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            result_file = root / "review_result.md"
            result_file.write_text(review_body(verdict="approved", summary="Nilo transaction completed through MCP worker.", findings=""), encoding="utf-8")
            previous_cwd = Path.cwd()
            worker: subprocess.Popen[str] | None = None
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "transaction worker"])

                env = os.environ.copy()
                src_path = str(Path(__file__).resolve().parents[1] / "src")
                env["PYTHONPATH"] = src_path + os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else src_path
                worker = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "from nilo.cli import main; main()",
                        "--db",
                        str(db),
                        "mcp",
                        "reviewer-worker",
                        "--reviewer",
                        "claude-code",
                        "--project",
                        "project_test",
                        "--result-file",
                        str(result_file),
                        "--wait-seconds",
                        "5",
                        "--poll-interval",
                        "0.05",
                    ],
                    cwd=str(root),
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                wait_for_dispatch_capable_reviewer(db, "claude-code")

                request_output = io.StringIO()
                with redirect_stdout(request_output):
                    main(
                        [
                            "--db",
                            str(db),
                            "review",
                            "request",
                            "--task",
                            "task_test",
                            "--from",
                            "codex",
                            "--to",
                            "claude-code",
                            "--reason",
                            "transaction e2e",
                        ]
                    )
                stdout, stderr = worker.communicate(timeout=10)

                status_output = io.StringIO()
                with redirect_stdout(status_output):
                    main(["--db", str(db), "review", "status", "--task", "task_test"])
                store = Store(db)
                try:
                    request = store.latest_for_task("review_requests", "task_test")
                    result = store.latest_for_task("review_results", "task_test")
                finally:
                    store.close()
            finally:
                if worker is not None and worker.poll() is None:
                    worker.kill()
                    worker.communicate(timeout=5)
                os.chdir(previous_cwd)

        self.assertEqual(worker.returncode, 0, stderr)
        self.assertIn("review_request:", request_output.getvalue())
        self.assertIn("review_request:", stdout)
        self.assertIn("verdict: approved", stdout)
        self.assertEqual(request["status"], "completed")
        self.assertEqual(result["verdict"], "approved")
        self.assertIn("[completed] codex -> claude-code: transaction e2e", status_output.getvalue())

    def test_project_next_marks_expired_claimed_review_stale(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "expired review claim"])
            store = Store(db)
            try:
                store.insert(
                    "review_requests",
                    {
                        "id": "review_expired_claim",
                        "task_id": "task_test",
                        "requester": "codex",
                        "reviewer": "claude-code",
                        "status": "claimed",
                        "reason": "expired claim",
                        "created_at": "2000-01-01T00:00:00+00:00",
                        "updated_at": "2000-01-01T00:00:00+00:00",
                    },
                )
            finally:
                store.close()

            next_output = io.StringIO()
            with redirect_stdout(next_output):
                main(["--db", str(db), "next", "--project", "project_test"])
            store = Store(db)
            try:
                request = store.get("review_requests", "review_expired_claim")
            finally:
                store.close()

        self.assertEqual(request["status"], "stale")
        self.assertIn("claude-code reviewer is missing", next_output.getvalue())
        self.assertIn("review review_expired_claim", next_output.getvalue())
        self.assertNotIn("wait for MCP reviewer", next_output.getvalue())

    def test_project_next_explains_stale_review_with_heartbeat_only_reviewer(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "heartbeat only review"])
                main(["--db", str(db), "mcp", "reviewer-start", "--reviewer", "claude-code", "--project", "project_test"])
            store = Store(db)
            try:
                store.insert(
                    "review_requests",
                    {
                        "id": "review_heartbeat_only",
                        "task_id": "task_test",
                        "requester": "codex",
                        "reviewer": "claude-code",
                        "status": "stale",
                        "reason": "heartbeat only reviewer",
                        "created_at": now_iso(),
                        "updated_at": now_iso(),
                    },
                )
            finally:
                store.close()

            next_output = io.StringIO()
            with redirect_stdout(next_output):
                main(["--db", str(db), "next", "--project", "project_test"])

        body = next_output.getvalue()
        self.assertIn("review_heartbeat_only", body)
        self.assertIn("claude-code reviewer is heartbeat_only", body)
        self.assertIn("reviewer-start only records heartbeat", body)
        self.assertIn("start a real MCP reviewer worker", body)

    def test_project_next_explains_stale_review_with_stale_reviewer_heartbeat(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "stale reviewer heartbeat"])
            store = Store(db)
            try:
                store.insert(
                    "review_reviewers",
                    {
                        "id": "reviewer_stale_heartbeat",
                        "reviewer": "claude-code",
                        "status": "available",
                        "capabilities": ["review"],
                        "max_concurrent": 1,
                        "metadata": {"worker_path": "nilo mcp reviewer-claim"},
                        "last_heartbeat_at": "2000-01-01T00:00:00+00:00",
                        "created_at": "2000-01-01T00:00:00+00:00",
                        "updated_at": "2000-01-01T00:00:00+00:00",
                    },
                )
                store.insert(
                    "review_requests",
                    {
                        "id": "review_stale_heartbeat",
                        "task_id": "task_test",
                        "requester": "codex",
                        "reviewer": "claude-code",
                        "status": "stale",
                        "reason": "stale reviewer heartbeat",
                        "created_at": now_iso(),
                        "updated_at": now_iso(),
                    },
                )
            finally:
                store.close()

            next_output = io.StringIO()
            with redirect_stdout(next_output):
                main(["--db", str(db), "next", "--project", "project_test"])

        body = next_output.getvalue()
        self.assertIn("review_stale_heartbeat", body)
        self.assertIn("claude-code reviewer heartbeat is stale", body)
        self.assertIn("start or refresh a real MCP reviewer worker", body)

    def test_review_prepare_file_and_template_generate_handoff_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            prompt = root / ".nilo" / "reviews" / "prompt.md"
            template = root / ".nilo" / "reviews" / "review.md"
            report.write_text(REPORT, encoding="utf-8")
            verification_result = {
                "command": "python -m unittest discover tests",
                "cwd": str(root),
                "stdout": "ok\n",
                "stderr": "",
                "exit_code": 0,
                "timed_out": False,
                "timeout_seconds": 300.0,
                "git_head": "abc123",
                "metadata": {
                    "secret_issue_count": 0,
                    "secret_issues": [],
                    "runner": "local",
                    "sandbox": "none",
                    "working_tree_dirty": False,
                    "working_tree_files": [],
                    "working_tree_available": True,
                },
                "started_at": "2099-06-17T00:00:00+09:00",
                "finished_at": "2099-06-17T00:00:01+09:00",
                "created_at": "2099-06-17T00:00:01+09:00",
            }

            with redirect_stdout(io.StringIO()), patch("nilo.cli_handlers.workflow.evaluate_evidence", return_value=("evidence_submitted", [], {"ok": True})):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "handoff file を作る",
                        "--acceptance",
                        "prompt file に acceptance criteria が含まれる",
                    ]
                )
                main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(report)])
                with patch("nilo.cli_handlers.workflow.run_local_verification", return_value=verification_result):
                    main(["--db", str(db), "verification", "run", "--task", "task_test", "--command", "python -m unittest discover tests"])
                register_test_reviewer(db, "claude-code")
                request_output = io.StringIO()
                with redirect_stdout(request_output):
                    main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "claude-code", "--reason", "handoff"])

            request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))
            self.assertIn(f"handoff_prompt: nilo review prepare --task task_test --review {request_id} --write-default", request_output.getvalue())
            self.assertIn(f"review_template: nilo review template --review {request_id} --write-default", request_output.getvalue())

            prepare_output = io.StringIO()
            with redirect_stdout(prepare_output):
                main(["--db", str(db), "review", "prepare", "--task", "task_test", "--review", request_id, "--file", str(prompt)])
            self.assertIn(f"review_context: {prompt}", prepare_output.getvalue())
            prompt_body = prompt.read_text(encoding="utf-8")
            self.assertIn("# Review Request", prompt_body)
            self.assertIn("prompt file に acceptance criteria が含まれる", prompt_body)
            self.assertIn("python -m unittest discover tests", prompt_body)

            template_output = io.StringIO()
            with redirect_stdout(template_output):
                main(["--db", str(db), "review", "template", "--review", request_id, "--file", str(template)])
            self.assertIn(f"review_template: {template}", template_output.getvalue())
            template_body = template.read_text(encoding="utf-8")
            self.assertIn("# ReviewResult", template_body)
            self.assertIn(f"review_id: {request_id}", template_body)
            self.assertIn(f"nilo review import --task task_test --review {request_id}", template_body)

    def test_review_handoff_write_default_paths(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "default handoff"])
                    register_test_reviewer(db, "claude-code")
                    request_output = io.StringIO()
                    with redirect_stdout(request_output):
                        main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "claude-code", "--reason", "handoff"])
                request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))

                prepare_output = io.StringIO()
                with redirect_stdout(prepare_output):
                    main(["--db", str(db), "review", "prepare", "--task", "task_test", "--review", request_id, "--write-default"])
                template_output = io.StringIO()
                with redirect_stdout(template_output):
                    main(["--db", str(db), "review", "template", "--review", request_id, "--write-default"])
            finally:
                os.chdir(previous_cwd)

            prompt = root / ".nilo" / "reviews" / f"{request_id}_prompt.md"
            template = root / ".nilo" / "reviews" / f"{request_id}.md"
            default_prompt = Path(".nilo") / "reviews" / f"{request_id}_prompt.md"
            default_template = Path(".nilo") / "reviews" / f"{request_id}.md"
            self.assertTrue(prompt.exists())
            self.assertTrue(template.exists())
            self.assertIn(f"review_context: {default_prompt}", prepare_output.getvalue())
            self.assertIn(f"review_template: {default_template}", template_output.getvalue())
            self.assertIn("# Review Request", prompt.read_text(encoding="utf-8"))
            self.assertIn("# ReviewResult", template.read_text(encoding="utf-8"))

    def test_review_handoff_rejects_file_and_write_default_together(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    main(
                        [
                            "--db",
                            str(db),
                            "review",
                            "prepare",
                            "--task",
                            "task_test",
                            "--review",
                            "review_test",
                            "--file",
                            "prompt.md",
                            "--write-default",
                        ]
                    )

    def test_review_commented_status_has_project_next_action(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            review = root / "review.md"
            review.write_text("# ReviewResult\n\n## Verdict\ncommented\n\n## Summary\n参考コメントのみ。", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "コメントレビューを扱う"])
                register_test_reviewer(db, "human")
                request_output = io.StringIO()
                with redirect_stdout(request_output):
                    main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "human", "--reason", "参考レビュー"])
            request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "review", "import", "--task", "task_test", "--review", request_id, "--file", str(review)])

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])

            body = output.getvalue()
            self.assertIn("work_state: レビュー結果の確認待ちです。", body)
            self.assertIn("task_test [review_commented]", body)
            self.assertIn("review imported findings and decide whether to address them", body)

    def test_review_delegate_creates_request_for_active_task_and_prints_claude_instruction(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "delegate review"])
                    register_test_reviewer(db, "claude-code")
                output = io.StringIO()
                with redirect_stdout(output):
                    main(
                        [
                            "--db",
                            str(db),
                            "review",
                            "delegate",
                            "--project",
                            "project_test",
                            "--to",
                            "claude-code",
                            "--reason",
                            "ユーザー依頼によるレビュー",
                        ]
                    )
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                request = store.latest_for_task("review_requests", "task_test")
            finally:
                store.close()

        self.assertEqual(request["reviewer"], "claude-code")
        self.assertEqual(request["requester"], "codex")
        self.assertEqual(request["reason"], "ユーザー依頼によるレビュー")
        body = output.getvalue()
        self.assertIn(f"review_request: {request['id']}", body)
        self.assertIn(f"Nilo MCP を使って task task_test の review {request['id']} をレビューして。", body)
        self.assertIn("register_reviewer", body)
        self.assertIn("claim_next_review", body)
        self.assertIn('get_status(project_id="project_test")', body)
        self.assertIn("import_review_result", body)
        self.assertIn("record_verification", body)
        self.assertNotIn("get_agent_work_context", body)
        self.assertNotIn("submit_agent_report", body)
        self.assertNotIn("record_test_result", body)

    def test_review_request_resolves_registered_reviewer_alias(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "alias review"])
                register_test_reviewer(db, "claude-code")
                main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "claude", "--reason", "alias"])

            store = Store(db)
            try:
                request = store.latest_for_task("review_requests", "task_test")
            finally:
                store.close()

        self.assertEqual(request["reviewer"], "claude-code")
        self.assertEqual(request["status"], "requested")

    def test_review_request_marks_unregistered_reviewer_unavailable(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "missing reviewer"])

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "claude-code", "--reason", "missing"])

            store = Store(db)
            try:
                requests = store.list_where("review_requests", "task_id=?", ("task_test",))
            finally:
                store.close()

            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["status"], "reviewer_unavailable")
            self.assertIn("status: reviewer_unavailable", output.getvalue())
            self.assertIn("start a real MCP reviewer worker for claude-code", output.getvalue())
            self.assertIn("reviewer-start only records heartbeat", output.getvalue())
            next_output = io.StringIO()
            with redirect_stdout(next_output):
                main(["--db", str(db), "next", "--project", "project_test"])
            self.assertIn("claude-code reviewer is missing", next_output.getvalue())
            self.assertIn("start a real MCP reviewer worker", next_output.getvalue())
            self.assertIn("claim_next_review", next_output.getvalue())

    def test_review_request_marks_stale_reviewer_unavailable(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "stale reviewer"])
            store = Store(db)
            try:
                store.insert(
                    "review_reviewers",
                    {
                        "id": "reviewer_stale",
                        "reviewer": "claude-code",
                        "status": "available",
                        "capabilities": ["review"],
                        "max_concurrent": 1,
                        "metadata": {"test": True},
                        "last_heartbeat_at": "2000-01-01T00:00:00+00:00",
                        "created_at": "2000-01-01T00:00:00+00:00",
                        "updated_at": "2000-01-01T00:00:00+00:00",
                    },
                )
            finally:
                store.close()

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "claude-code", "--reason", "stale"])
            store = Store(db)
            try:
                requests = store.list_where("review_requests", "task_id=?", ("task_test",))
            finally:
                store.close()

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["status"], "reviewer_unavailable")

    def test_review_prepare_reviewer_readiness_outputs_json(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "mcp", "reviewer-start", "--reviewer", "claude-code", "--project", "project_test"])

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "review", "prepare", "--project", "project_test", "--reviewer", "claude-code"])
            heartbeat_only = json.loads(output.getvalue())

            store = Store(db)
            try:
                reviewer_row = store.list_where("review_reviewers", "reviewer=?", ("claude-code",))[0]
                store.update(
                    "review_reviewers",
                    reviewer_row["id"],
                    {
                        "metadata": {
                            "worker_path": "claude-code-mcp-session",
                            "dispatch_capable": True,
                            "source": "real Claude Code session",
                        },
                    },
                )
            finally:
                store.close()
            ready_output = io.StringIO()
            with redirect_stdout(ready_output):
                main(["--db", str(db), "review", "prepare", "--project", "project_test", "--reviewer", "claude-code"])
            ready = json.loads(ready_output.getvalue())

        self.assertFalse(heartbeat_only["ready"])
        self.assertEqual(heartbeat_only["reason"], "heartbeat_only")
        self.assertIn("Open the Claude Code session", heartbeat_only["next_action"])
        self.assertEqual(heartbeat_only["register_reviewer_json"]["metadata"]["worker_path"], "claude-code-mcp-session")
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["availability"], "available")

    def test_review_withdraw_marks_requested_review_and_removes_pending_next_action(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "withdraw review"])
                register_test_reviewer(db, "claude-code")
                request_output = io.StringIO()
                with redirect_stdout(request_output):
                    main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "claude-code", "--reason", "review me"])
            request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))

            withdraw_output = io.StringIO()
            with redirect_stdout(withdraw_output):
                main(["--db", str(db), "review", "withdraw", "--review", request_id, "--reason", "superseded", "--actor", "codex"])
            status_output = io.StringIO()
            with redirect_stdout(status_output):
                main(["--db", str(db), "review", "status", "--task", "task_test"])
            project_status = io.StringIO()
            with redirect_stdout(project_status):
                main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])
            next_output = io.StringIO()
            with redirect_stdout(next_output):
                main(["--db", str(db), "next", "--project", "project_test"])

            store = Store(db)
            try:
                request = store.get("review_requests", request_id)
            finally:
                store.close()

        self.assertEqual(request["status"], "withdrawn")
        self.assertEqual(request["withdrawn_reason"], "superseded")
        self.assertEqual(request["withdrawn_actor"], "codex")
        self.assertIn("status: withdrawn", withdraw_output.getvalue())
        self.assertIn(f"- {request_id} [withdrawn] codex -> claude-code: review me", status_output.getvalue())
        self.assertIn("withdrawn_reason: superseded", status_output.getvalue())
        self.assertNotIn("claim review", project_status.getvalue())
        self.assertNotIn("claim review", next_output.getvalue())

    def test_review_withdraw_rejects_completed_review_without_changing_results(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            review = root / "review.md"
            review.write_text("# ReviewResult\n\n## Verdict\napproved\n\n## Summary\nOK。", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "terminal review"])
                register_test_reviewer(db, "claude-code")
                request_output = io.StringIO()
                with redirect_stdout(request_output):
                    main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "claude-code", "--reason", "review me"])
            request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "review", "import", "--task", "task_test", "--review", request_id, "--file", str(review)])

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "review", "withdraw", "--review", request_id, "--reason", "too late", "--actor", "codex"])
            store = Store(db)
            try:
                request = store.get("review_requests", request_id)
                results = store.list_where("review_results", "task_id=?", ("task_test",))
            finally:
                store.close()

        self.assertIn("terminal and cannot be withdrawn", str(raised.exception))
        self.assertEqual(request["status"], "completed")
        self.assertEqual(len(results), 1)

    def test_review_wait_returns_completed_review_result(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            review = root / "review.md"
            review.write_text("# ReviewResult\n\n## Verdict\napproved\n\n## Summary\nOK。", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "wait review"])
                register_test_reviewer(db, "claude-code")
                request_output = io.StringIO()
                with redirect_stdout(request_output):
                    main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "claude-code", "--reason", "review me"])
            request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "review", "import", "--task", "task_test", "--review", request_id, "--file", str(review)])

            wait_output = io.StringIO()
            with redirect_stdout(wait_output):
                main(["--db", str(db), "review", "wait", "--review", request_id, "--timeout", "0"])

        self.assertIn(f"review_request: {request_id}", wait_output.getvalue())
        self.assertIn("status: completed", wait_output.getvalue())
        self.assertIn("verdict: approved", wait_output.getvalue())

    def test_review_request_wait_timeout_withdraws_request_and_clears_next_action(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "timeout review"])
                register_test_reviewer(db, "claude-code")
            request_output = io.StringIO()
            with self.assertRaises(SystemExit) as raised, redirect_stdout(request_output):
                main(
                    [
                        "--db",
                        str(db),
                        "review",
                        "request",
                        "--task",
                        "task_test",
                        "--from",
                        "codex",
                        "--to",
                        "claude-code",
                        "--reason",
                        "review me",
                        "--wait",
                        "--wait-timeout",
                        "0",
                        "--poll-interval",
                        "0",
                    ]
                )
            request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))
            next_output = io.StringIO()
            with redirect_stdout(next_output):
                main(["--db", str(db), "next", "--project", "project_test"])
            store = Store(db)
            try:
                request = store.get("review_requests", request_id)
            finally:
                store.close()

        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(request["status"], "withdrawn")
        self.assertEqual(request["withdrawn_actor"], "codex")
        self.assertIn("wait_result: timed_out", request_output.getvalue())
        self.assertIn("review wait timed out after 0 seconds", request["withdrawn_reason"])
        self.assertNotIn("claim review", next_output.getvalue())

    def test_review_wait_withdraws_when_reviewer_heartbeat_is_stale(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "stale wait"])
            store = Store(db)
            try:
                store.insert(
                    "review_reviewers",
                    {
                        "id": "reviewer_stale_wait",
                        "reviewer": "claude-code",
                        "status": "available",
                        "capabilities": ["review"],
                        "max_concurrent": 1,
                        "metadata": {"test": True},
                        "last_heartbeat_at": "2000-01-01T00:00:00+00:00",
                        "created_at": "2000-01-01T00:00:00+00:00",
                        "updated_at": "2000-01-01T00:00:00+00:00",
                    },
                )
                store.insert(
                    "review_requests",
                    {
                        "id": "review_stale_wait",
                        "task_id": "task_test",
                        "requester": "codex",
                        "reviewer": "claude-code",
                        "status": "requested",
                        "reason": "old pending request",
                        "created_at": now_iso(),
                        "updated_at": now_iso(),
                    },
                )
            finally:
                store.close()

            wait_output = io.StringIO()
            with self.assertRaises(SystemExit) as raised, redirect_stdout(wait_output):
                main(["--db", str(db), "review", "wait", "--review", "review_stale_wait", "--timeout", "600", "--poll-interval", "0", "--actor", "codex"])
            store = Store(db)
            try:
                request = store.get("review_requests", "review_stale_wait")
            finally:
                store.close()

        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(request["status"], "withdrawn")
        self.assertEqual(request["withdrawn_reason"], "reviewer unavailable while waiting: claude-code")
        self.assertIn("wait_result: reviewer_unavailable", wait_output.getvalue())

    def test_review_human_launch_claude_runs_claude_code_with_instruction_on_stdin(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "claude review"])
                    register_test_reviewer(db, "claude-code")
                completed = subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout="レビュー完了\n",
                    stderr="",
                )
                output = io.StringIO()
                with patch("subprocess.run", return_value=completed) as run_mock, redirect_stdout(output):
                    main(["--db", str(db), "review", "human-launch-claude", "--project", "project_test", "--task", "task_test"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                request = store.latest_for_task("review_requests", "task_test")
            finally:
                store.close()

        self.assertEqual(request["reviewer"], "claude-code")
        self.assertEqual(request["requester"], "codex")
        call = run_mock.call_args
        self.assertEqual(
            call.args[0],
            [
                "rtk",
                "proxy",
                "claude",
                "-p",
                "--mcp-config",
                ".mcp.json",
                "--permission-mode",
                "bypassPermissions",
            ],
        )
        self.assertIn(f"Nilo MCP を使って task task_test の review {request['id']} をレビューして。", call.kwargs["input"])
        self.assertEqual(call.kwargs["timeout"], 600.0)
        body = output.getvalue()
        self.assertNotIn("claude_command:", body)
        self.assertNotIn("rtk proxy claude", body)
        self.assertNotIn("claude -p", body)
        self.assertIn("claude_exit_code: 0", body)
        self.assertIn("review_status: requested", body)

    def test_natural_language_cluade_code_review_prints_mcp_handoff_without_cli_dispatch(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_dispatch_reviewer(root, verdict="approved", findings="なし")
                write_dispatch_reviewer_config(root, ["claude-code"])
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", root.name])
                    main(["--db", str(db), "task", "create", "--project", root.name, "--id", "task_test", "--title", "natural review"])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "Cluade", "Codeにレビューしてもらって"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                request = store.latest_for_task("review_requests", "task_test")
                result = store.latest_for_task("review_results", "task_test")
                dispatches = store.list_where("review_dispatches")
            finally:
                store.close()

        self.assertIsNone(request)
        self.assertIsNone(result)
        self.assertEqual(dispatches, [])
        body = output.getvalue()
        self.assertIn("review_handoff: use Nilo MCP dispatch_review", body)
        self.assertIn("review_handoff_reason: natural-language CLI entrypoint cannot call MCP tools directly", body)
        self.assertIn("reviewer: claude-code", body)
        self.assertIn("task: task_test", body)
        self.assertIn('"reason": "Cluade Codeにレビューしてもらって"', body)
        arguments_line = next(line for line in body.splitlines() if line.startswith("mcp_arguments: "))
        self.assertEqual(
            json.loads(arguments_line.removeprefix("mcp_arguments: ")),
            {
                "task_id": "task_test",
                "project_id": root.name,
                "actor": "codex",
                "reviewer": "claude-code",
                "reason": "Cluade Codeにレビューしてもらって",
            },
        )
        self.assertIn("cli_fallback: use `nilo review dispatch` only after explaining why MCP review workflow is unavailable", body)

    def test_review_quick_imports_parseable_review_result(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_dispatch_reviewer(root, verdict="approved", findings="なし")
                write_dispatch_reviewer_config(root, ["claude-code"], timeout_seconds=600)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "quick review"])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "review", "quick", "--task", "task_test", "--reviewer", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                request = store.latest_for_task("review_requests", "task_test")
                result = store.latest_for_task("review_results", "task_test")
                dispatches = store.list_where("review_dispatches")
            finally:
                store.close()

        body = output.getvalue()
        self.assertIn("# ReviewResult", body)
        self.assertIn("quick_usage: local CLI fallback / diagnostics only", body)
        self.assertIn("quick_status: review_imported", body)
        self.assertIn("quick_imported: true", body)
        self.assertEqual(request["status"], "completed")
        self.assertEqual(result["verdict"], "approved")
        self.assertEqual(dispatches, [])

    def test_review_quick_shows_raw_output_when_unparseable(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_dispatch_reviewer_script(root, "print('plain raw review')\n")
                write_dispatch_reviewer_config(root, ["claude-code"])
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "raw quick review"])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "review", "quick", "--task", "task_test", "--reviewer", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                request = store.latest_for_task("review_requests", "task_test")
                result = store.latest_for_task("review_results", "task_test")
            finally:
                store.close()

        body = output.getvalue()
        self.assertIn("plain raw review", body)
        self.assertIn("quick_status: raw_review", body)
        self.assertIn("quick_imported: false", body)
        self.assertEqual(request["status"], "failed")
        self.assertIsNone(result)

    def test_review_quick_marks_request_failed_on_unexpected_exception(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_dispatch_reviewer(root, verdict="approved", findings="なし")
                write_dispatch_reviewer_config(root, ["claude-code"])
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "exception quick review"])
                output = io.StringIO()
                with patch("nilo.review_dispatcher.build_prompt_file", side_effect=OSError("cannot write prompt")):
                    with self.assertRaises(SystemExit), redirect_stdout(output):
                        main(["--db", str(db), "review", "quick", "--task", "task_test", "--reviewer", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                request = store.latest_for_task("review_requests", "task_test")
                result = store.latest_for_task("review_results", "task_test")
            finally:
                store.close()

        body = output.getvalue()
        self.assertIn("quick_status: review_failed", body)
        self.assertIn("cannot write prompt", body)
        self.assertEqual(request["status"], "failed")
        self.assertIn("cannot write prompt", request["withdrawn_reason"])
        self.assertIsNone(result)

    def test_review_quick_no_import_does_not_create_review_request(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_dispatch_reviewer(root, verdict="approved", findings="なし")
                write_dispatch_reviewer_config(root, ["claude-code"])
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "no import quick review"])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "review", "quick", "--task", "task_test", "--reviewer", "claude-code", "--no-import"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                requests = store.list_where("review_requests", "task_id=?", ("task_test",))
            finally:
                store.close()

        self.assertIn("quick_status: raw_review", output.getvalue())
        self.assertEqual(requests, [])

    def test_natural_language_light_review_prints_mcp_handoff_not_quick_or_dispatch(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_dispatch_reviewer(root, verdict="approved", findings="なし")
                write_dispatch_reviewer_config(root, ["claude-code"])
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", root.name])
                    main(["--db", str(db), "task", "create", "--project", root.name, "--id", "task_test", "--title", "light natural review"])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "Claudeに軽くレビューして"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                result = store.latest_for_task("review_results", "task_test")
                dispatches = store.list_where("review_dispatches")
                request = store.latest_for_task("review_requests", "task_test")
            finally:
                store.close()

        self.assertIsNone(result)
        self.assertEqual(dispatches, [])
        self.assertIsNone(request)
        self.assertIn("quick requested; quick is local CLI fallback / diagnostics only", output.getvalue())
        self.assertIn("mcp_tool: dispatch_review", output.getvalue())

    def test_natural_language_formal_review_prints_mcp_handoff_not_dispatch(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_dispatch_reviewer(root, verdict="approved", findings="なし")
                write_dispatch_reviewer_config(root, ["claude-code"])
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", root.name])
                    main(["--db", str(db), "task", "create", "--project", root.name, "--id", "task_test", "--title", "formal natural review"])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "Claudeに正式レビューして"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                dispatches = store.list_where("review_dispatches")
            finally:
                store.close()

        self.assertEqual(dispatches, [])
        self.assertIn("review_handoff: use Nilo MCP dispatch_review", output.getvalue())

    def test_review_dispatch_missing_config_returns_structured_next_action(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "missing config"])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "review", "dispatch", "--task", "task_test", "--actor", "codex", "--reviewer", "claude-code", "--no-auto-configure"])
            finally:
                os.chdir(previous_cwd)

        body = output.getvalue()
        self.assertIn('"status": "needs_reviewer_config"', body)
        self.assertIn('"type": "create_reviewer_config"', body)
        self.assertIn("nilo review init --reviewer claude-code", body)

    def test_review_dispatch_command_not_found_records_command_resolution_failure(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_dispatch_reviewer_config(root, ["claude-code"], command="definitely_missing_reviewer_binary")
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "missing command"])
                output = io.StringIO()
                with self.assertRaises(SystemExit), redirect_stdout(output):
                    main(["--db", str(db), "review", "dispatch", "--task", "task_test", "--actor", "codex", "--reviewer", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                dispatch = store.list_where("review_dispatches")[0]
            finally:
                store.close()

        self.assertEqual(dispatch["status"], "review_failed")
        self.assertEqual(dispatch["failure_stage"], "command_resolution")
        self.assertIn('"failure_stage": "command_resolution"', output.getvalue())

    def test_review_dispatch_timeout_fails_and_marks_request_failed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_dispatch_reviewer_script(root, "import time\ntime.sleep(10)\n")
                write_dispatch_reviewer_config(root, ["claude-code"], timeout_seconds=0.02)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "timeout"])
                output = io.StringIO()
                with self.assertRaises(SystemExit), redirect_stdout(output):
                    main(["--db", str(db), "review", "dispatch", "--task", "task_test", "--actor", "codex", "--reviewer", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                request = store.latest_for_task("review_requests", "task_test")
                dispatch = store.list_where("review_dispatches")[0]
            finally:
                store.close()

        self.assertEqual(request["status"], "failed")
        self.assertEqual(dispatch["failure_stage"], "reviewer_timeout")
        self.assertIn('"failure_stage": "reviewer_timeout"', output.getvalue())
        self.assertIn('"type": "reviewer_timeout"', output.getvalue())

    def test_review_dispatch_malformed_output_does_not_complete(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_dispatch_reviewer_script(root, "print('not a review result')\n")
                write_dispatch_reviewer_config(root, ["claude-code"])
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "malformed"])
                output = io.StringIO()
                with self.assertRaises(SystemExit), redirect_stdout(output):
                    main(["--db", str(db), "review", "dispatch", "--task", "task_test", "--actor", "codex", "--reviewer", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                request = store.latest_for_task("review_requests", "task_test")
                result = store.latest_for_task("review_results", "task_test")
                dispatch = store.list_where("review_dispatches")[0]
            finally:
                store.close()

        self.assertEqual(request["status"], "failed")
        self.assertIsNone(result)
        self.assertEqual(dispatch["failure_stage"], "review_output_received")
        self.assertIn("reviewer output malformed", output.getvalue())

    def test_review_dispatch_unrecognized_json_output_does_not_complete(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_dispatch_reviewer_script(root, "print('{\"error\":\"rate limited\"}')\n")
                write_dispatch_reviewer_config(root, ["claude-code"])
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "malformed json"])
                output = io.StringIO()
                with self.assertRaises(SystemExit), redirect_stdout(output):
                    main(["--db", str(db), "review", "dispatch", "--task", "task_test", "--actor", "codex", "--reviewer", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                request = store.latest_for_task("review_requests", "task_test")
                result = store.latest_for_task("review_results", "task_test")
                dispatch = store.list_where("review_dispatches")[0]
            finally:
                store.close()

        self.assertEqual(request["status"], "failed")
        self.assertIsNone(result)
        self.assertEqual(dispatch["failure_stage"], "review_output_received")
        self.assertIn("reviewer output malformed", output.getvalue())

    def test_review_dispatch_masks_secret_in_failure_outputs(self) -> None:
        secret = "sk-" + "a" * 24
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_dispatch_reviewer_script(root, f"import sys\nprint({secret!r})\nprint({secret!r}, file=sys.stderr)\nsys.exit(2)\n")
                write_dispatch_reviewer_config(root, ["claude-code"])
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "secret"])
                output = io.StringIO()
                with self.assertRaises(SystemExit), redirect_stdout(output):
                    main(["--db", str(db), "review", "dispatch", "--task", "task_test", "--actor", "codex", "--reviewer", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                dispatch = store.list_where("review_dispatches")[0]
            finally:
                store.close()

        self.assertNotIn(secret, output.getvalue())
        self.assertNotIn(secret, dispatch["stdout"])
        self.assertNotIn(secret, dispatch["stderr"])
        self.assertIn("[MASKED:openai_api_key]", output.getvalue())

    def test_review_dispatch_supersedes_stale_requests(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                write_fake_dispatch_reviewer(root, verdict="approved", findings="なし")
                write_dispatch_reviewer_config(root, ["claude-code"])
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "stale cleanup"])
                store = Store(db)
                try:
                    store.insert(
                        "review_requests",
                        {
                            "id": "review_stale",
                            "task_id": "task_test",
                            "requester": "codex",
                            "reviewer": "claude-code",
                            "status": "stale",
                            "reason": "old",
                            "created_at": now_iso(),
                            "updated_at": now_iso(),
                        },
                    )
                finally:
                    store.close()
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "review", "dispatch", "--task", "task_test", "--actor", "codex", "--reviewer", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                stale = store.get("review_requests", "review_stale")
                latest_event = store.latest_task_status_event("task_test")
            finally:
                store.close()

        self.assertEqual(stale["status"], "superseded")
        self.assertNotEqual(latest_event["event_id"], "review_stale")

    def test_windows_command_resolution_finds_cmd_suffix(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            command = root / "claude.cmd"
            command.write_text("@echo off\n", encoding="utf-8")
            env = {"PATH": str(root)}
            with patch("nilo.review_dispatcher.sys.platform", "win32"):
                resolved = find_executable("claude", env)

        self.assertEqual(Path(resolved).name.lower(), "claude.cmd")

    def test_review_human_launch_claude_dry_run_skips_subprocess(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "claude dry run"])
                output = io.StringIO()
                with patch("subprocess.run") as run_mock, redirect_stdout(output):
                    main(["--db", str(db), "review", "human-launch-claude", "--project", "project_test", "--dry-run"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                requests = store.list_where("review_requests", "task_id=?", ("task_test",))
            finally:
                store.close()

        claude_calls = [call for call in run_mock.call_args_list if call.args and call.args[0][:3] == ["rtk", "proxy", "claude"]]
        self.assertEqual(claude_calls, [])
        self.assertEqual(requests, [])
        body = output.getvalue()
        self.assertIn("claude_status: skipped (dry-run)", body)
        self.assertNotIn("claude_command:", body)
        self.assertNotIn("rtk proxy claude", body)
        self.assertNotIn("claude -p", body)

    def test_review_human_launch_claude_verbose_prints_human_runner_command(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "claude dry run"])
                output = io.StringIO()
                with patch("subprocess.run") as run_mock, redirect_stdout(output):
                    main(["--db", str(db), "review", "human-launch-claude", "--project", "project_test", "--dry-run", "--verbose"])
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(run_mock.call_args_list, [])
        body = output.getvalue()
        self.assertIn("human_runner_command:", body)
        self.assertIn("rtk proxy claude", body)
        self.assertNotIn("claude_command:", body)

    def test_review_delegate_creates_review_task_for_dirty_tree_without_active_task(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                (root / "dirty.txt").write_text("review me\n", encoding="utf-8")
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    register_test_reviewer(db, "claude-code")
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "review", "delegate", "--project", "project_test", "--to", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                tasks = store.list_where("tasks", "project_id=?", ("project_test",))
                task = tasks[0]
                request = store.latest_for_task("review_requests", task["id"])
            finally:
                store.close()

        self.assertEqual(task["task_type"], "review")
        self.assertIn("Dirty files:", task["description"])
        self.assertIn("dirty.txt", task["description"])
        self.assertIn("Dirty file: dirty.txt", task["acceptance_criteria"])
        self.assertEqual(request["reviewer"], "claude-code")
        body = output.getvalue()
        self.assertIn("review_target: current dirty tree", body)
        self.assertIn("レビュー対象は現在の未コミット差分です。", body)
        self.assertIn("コード変更はしないで。", body)

    def test_review_delegate_creates_fresh_dirty_tree_task_on_repeated_delegation(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                (root / "first.txt").write_text("first\n", encoding="utf-8")
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    register_test_reviewer(db, "claude-code")
                    main(["--db", str(db), "review", "delegate", "--project", "project_test", "--to", "claude-code"])
                (root / "second.txt").write_text("second\n", encoding="utf-8")
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "review", "delegate", "--project", "project_test", "--to", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                tasks = store.list_where("tasks", "project_id=?", ("project_test",))
                reviews_by_task = {task["id"]: store.list_where("review_requests", "task_id=?", (task["id"],)) for task in tasks}
            finally:
                store.close()

        self.assertEqual(len(tasks), 2)
        self.assertNotEqual(tasks[0]["id"], tasks[1]["id"])
        self.assertTrue(all(task["task_type"] == "review" for task in tasks))
        self.assertTrue(all(reviews_by_task[task["id"]] for task in tasks))

    def test_review_delegate_refuses_clean_tree_without_active_task(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    register_test_reviewer(db, "claude-code")
                with self.assertRaises(SystemExit) as raised:
                    main(["--db", str(db), "review", "delegate", "--project", "project_test", "--to", "claude-code"])
            finally:
                os.chdir(previous_cwd)

        self.assertIn("active task not found and dirty tree could not be inspected", str(raised.exception))

    def test_review_delegate_rejects_unavailable_reviewer_without_creating_request_or_dirty_task(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                (root / "dirty.txt").write_text("review me\n", encoding="utf-8")
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                with self.assertRaises(SystemExit) as raised:
                    main(["--db", str(db), "review", "delegate", "--project", "project_test", "--to", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                tasks = store.list_where("tasks", "project_id=?", ("project_test",))
                requests = store.list_where("review_requests")
            finally:
                store.close()

        self.assertEqual(tasks, [])
        self.assertEqual(requests, [])
        self.assertIn("reviewer is not registered or available: claude-code", str(raised.exception))

    def test_dirty_tree_status_parser_handles_spaces_unicode_and_renames(self) -> None:
        raw = " M spaced name.txt\0?? 日本語.txt\0R  old name.txt\0new name.txt\0"

        self.assertEqual(
            parse_git_status_porcelain_z(raw),
            ["new name.txt", "spaced name.txt", "日本語.txt"],
        )

    def test_review_delegate_prefers_review_ready_task_over_old_rework_task(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_old", "--title", "old rework"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_ready", "--title", "ready review"])
                    register_test_reviewer(db, "claude-code")
                store = Store(db)
                try:
                    created_at = "2099-01-01T00:00:00+09:00"
                    store.insert(
                        "review_results",
                        {
                            "id": "review_result_old",
                            "task_id": "task_old",
                            "review_request_id": "review_old",
                            "reviewer": "claude-code",
                            "verdict": "changes_requested",
                            "summary": "needs rework",
                            "body_md": "# ReviewResult",
                            "created_at": created_at,
                        },
                    )
                    store.insert(
                        "verification_runs",
                        {
                            "id": "verification_ready",
                            "task_id": "task_ready",
                            "evidence_check_id": None,
                            "source": "nilo_executed",
                            "command": "python -m unittest",
                            "cwd": str(root),
                            "stdout": "",
                            "stderr": "",
                            "exit_code": 0,
                            "timed_out": False,
                            "timeout_seconds": 300.0,
                            "git_head": "abc123",
                            "metadata": {},
                            "started_at": "2099-01-01T00:00:01+09:00",
                            "finished_at": "2099-01-01T00:00:02+09:00",
                            "created_at": "2099-01-01T00:00:02+09:00",
                        },
                    )
                finally:
                    store.close()

                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "review", "delegate", "--project", "project_test", "--to", "claude-code"])
            finally:
                os.chdir(previous_cwd)

            store = Store(db)
            try:
                request = store.latest_for_task("review_requests", "task_ready")
                old_request = store.latest_for_task("review_requests", "task_old")
            finally:
                store.close()

        self.assertIsNotNone(request)
        self.assertIsNone(old_request)
        self.assertIn(f"task task_ready の review {request['id']}", output.getvalue())

    def test_unresolved_blocking_review_finding_blocks_ai_completion(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            review = root / "review.md"
            review.write_text(
                "# ReviewResult\n\n"
                "## Verdict\nchanges_requested\n\n"
                "## Summary\n修正が必要。\n\n"
                "## Findings\n"
                "### F1\n"
                "severity: high\n"
                "status: unresolved\n"
                "blocking: true\n\n"
                "完了前に直す必要がある。\n",
                encoding="utf-8",
            )
            verification_result = {
                "command": "python -m unittest discover tests",
                "cwd": str(root),
                "stdout": "ok\n",
                "stderr": "",
                "exit_code": 0,
                "timed_out": False,
                "timeout_seconds": 300.0,
                "git_head": "abc123",
                "metadata": {
                    "secret_issue_count": 0,
                    "secret_issues": [],
                    "runner": "local",
                    "sandbox": "none",
                    "working_tree_dirty": False,
                    "working_tree_files": [],
                    "working_tree_available": True,
                },
                "started_at": "2099-06-17T00:00:00+09:00",
                "finished_at": "2099-06-17T00:00:01+09:00",
                "created_at": "2099-06-17T00:00:01+09:00",
            }

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "完了をブロックする"])
                register_test_reviewer(db, "human")
                with patch("nilo.cli_handlers.workflow.run_local_verification", return_value=verification_result):
                    main(["--db", str(db), "verification", "run", "--task", "task_test", "--command", "python -m unittest discover tests"])
                request_output = io.StringIO()
                with redirect_stdout(request_output):
                    main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "human", "--reason", "確認"])
            request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "review", "import", "--task", "task_test", "--review", request_id, "--file", str(review)])

            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "task", "complete", "--task", "task_test", "--reason", "検証済み", "--actor", "ai"])
            self.assertIn("AI completion blocked by unresolved review findings", str(raised.exception))

    def test_project_status_default_is_human_readable_for_review_changes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            review = root / "review.md"
            review.write_text(
                "# ReviewResult\n\n"
                "## Verdict\nchanges_requested\n\n"
                "## Summary\n修正が必要。\n\n"
                "## Findings\n"
                "### F1\n"
                "severity: high\n"
                "status: unresolved\n"
                "file: src/nilo/project_logic.py\n"
                "line: 1\n"
                "blocking: true\n\n"
                "完了前に直す必要がある。\n",
                encoding="utf-8",
            )
            verification_result = {
                "command": "python -m unittest tests.test_cli",
                "cwd": str(root),
                "stdout": "ok\n",
                "stderr": "",
                "exit_code": 0,
                "timed_out": False,
                "timeout_seconds": 300.0,
                "git_head": "abc123",
                "metadata": {
                    "secret_issue_count": 0,
                    "secret_issues": [],
                    "runner": "local",
                    "sandbox": "none",
                    "working_tree_dirty": False,
                    "working_tree_files": [],
                    "working_tree_available": True,
                },
                "started_at": "2099-06-17T00:00:00+09:00",
                "finished_at": "2099-06-17T00:00:01+09:00",
                "created_at": "2099-06-17T00:00:01+09:00",
            }

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "Nilo human-readable status surface 改善",
                    ]
                )
                register_test_reviewer(db, "human")
                with patch("nilo.cli_handlers.workflow.run_local_verification", return_value=verification_result):
                    main(["--db", str(db), "verification", "run", "--task", "task_test", "--command", "python -m unittest tests.test_cli"])
                request_output = io.StringIO()
                with redirect_stdout(request_output):
                    main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "human", "--reason", "確認"])
            request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "review", "import", "--task", "task_test", "--review", request_id, "--file", str(review)])

            store = Store(db)
            finding = store.latest_for_task("review_findings", "task_test")
            store.close()

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "project", "status", "--project", "project_test"])
            body = output.getvalue()
            self.assertIn("状態: 作業中", body)
            self.assertIn("レビュー指摘が1件残っています", body)
            self.assertIn("直近の検証は成功しています", body)
            self.assertNotIn("review_changes_requested", body)
            self.assertNotIn("address unresolved review finding", body)
            self.assertNotIn("exit_code=0", body)
            self.assertNotIn("unresolved blocking review finding", body)
            self.assertNotIn("task_test", body)
            self.assertNotIn(finding["id"], body)

            verbose_output = io.StringIO()
            with redirect_stdout(verbose_output):
                main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])
            verbose_body = verbose_output.getvalue()
            self.assertIn("task_test", verbose_body)
            self.assertIn(finding["id"], verbose_body)

    def test_review_finding_update_records_history_and_unblocks_completion(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            review = root / "review.md"
            review.write_text(
                "# ReviewResult\n\n"
                "## Verdict\nchanges_requested\n\n"
                "## Summary\n修正が必要。\n\n"
                "## Findings\n"
                "### F1\n"
                "severity: high\n"
                "status: unresolved\n"
                "blocking: true\n\n"
                "完了前に直す必要がある。\n",
                encoding="utf-8",
            )
            verification_result = {
                "command": "python -m unittest discover tests",
                "cwd": str(root),
                "stdout": "ok\n",
                "stderr": "",
                "exit_code": 0,
                "timed_out": False,
                "timeout_seconds": 300.0,
                "git_head": "abc123",
                "metadata": {
                    "secret_issue_count": 0,
                    "secret_issues": [],
                    "runner": "local",
                    "sandbox": "none",
                    "working_tree_dirty": False,
                    "working_tree_files": [],
                    "working_tree_available": True,
                },
                "started_at": "2099-06-17T00:00:00+09:00",
                "finished_at": "2099-06-17T00:00:01+09:00",
                "created_at": "2099-06-17T00:00:01+09:00",
            }

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "finding 更新で完了可能にする"])
                register_test_reviewer(db, "human")
                with patch("nilo.cli_handlers.workflow.run_local_verification", return_value=verification_result):
                    main(["--db", str(db), "verification", "run", "--task", "task_test", "--command", "python -m unittest discover tests"])
                request_output = io.StringIO()
                with redirect_stdout(request_output):
                    main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "human", "--reason", "確認"])
            request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "review", "import", "--task", "task_test", "--review", request_id, "--file", str(review)])

            store = Store(db)
            finding = store.latest_for_task("review_findings", "task_test")
            store.close()

            update_output = io.StringIO()
            with redirect_stdout(update_output):
                main(
                    [
                        "--db",
                        str(db),
                        "review",
                        "finding",
                        "update",
                        "--finding",
                        finding["id"],
                        "--status",
                        "addressed",
                        "--reason",
                        "テストを追加して修正済み",
                        "--actor",
                        "codex",
                    ]
                )
            self.assertIn("previous_status: unresolved", update_output.getvalue())
            self.assertIn("status: addressed", update_output.getvalue())

            status_output = io.StringIO()
            with redirect_stdout(status_output):
                main(["--db", str(db), "review", "status", "--task", "task_test"])
            status_body = status_output.getvalue()
            self.assertIn("review_summary:", status_body)
            self.assertIn("- total_findings: 1", status_body)
            self.assertIn("- unresolved_blocking: 0", status_body)
            self.assertIn("- addressed: 1", status_body)
            self.assertIn("[addressed] high blocking", status_body)
            self.assertIn("update_history:", status_body)
            self.assertIn("codex: unresolved -> addressed; テストを追加して修正済み", status_body)

            task_status_output = io.StringIO()
            with redirect_stdout(task_status_output):
                main(["--db", str(db), "task", "show", "--task", "task_test", "--ai"])
            self.assertNotIn("[review_changes_requested]", task_status_output.getvalue())

            with redirect_stdout(io.StringIO()), patch(
                "nilo.task_logic.current_git_snapshot",
                return_value={"git_head": "abc123", "git_diff_hash": "", "working_tree_dirty": False},
            ):
                main(["--db", str(db), "task", "complete", "--task", "task_test", "--reason", "finding 対応済み", "--actor", "ai"])

            store = Store(db)
            updated = store.get("review_findings", finding["id"])
            updates = store.list_where("review_finding_updates", "finding_id=?", (finding["id"],))
            completion = store.latest_for_task("task_completions", "task_test")
            store.close()
            self.assertEqual(updated["status"], "addressed")
            self.assertEqual(updates[0]["previous_status"], "unresolved")
            self.assertEqual(updates[0]["new_status"], "addressed")
            self.assertEqual(updates[0]["reason"], "テストを追加して修正済み")
            self.assertEqual(updates[0]["actor"], "codex")
            self.assertIsNotNone(completion)

    def test_review_status_shows_summary_and_empty_update_history(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            review = root / "review.md"
            review.write_text(
                "# ReviewResult\n\n"
                "## Verdict\nchanges_requested\n\n"
                "## Summary\n要対応。\n\n"
                "## Findings\n"
                "### F1\n"
                "severity: high\n"
                "status: unresolved\n"
                "blocking: true\n\n"
                "blocking finding。\n\n"
                "### F2\n"
                "severity: low\n"
                "status: addressed\n"
                "blocking: false\n\n"
                "対応済み finding。\n",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "review status summary"])
                register_test_reviewer(db, "human")
                request_output = io.StringIO()
                with redirect_stdout(request_output):
                    main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "human", "--reason", "summary"])
            request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "review", "import", "--task", "task_test", "--review", request_id, "--file", str(review)])

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "review", "status", "--task", "task_test"])

            body = output.getvalue()
            self.assertIn("review_summary:", body)
            self.assertIn("- total_findings: 2", body)
            self.assertIn("- unresolved_blocking: 1", body)
            self.assertIn("- addressed: 1", body)
            self.assertIn("- unresolved: 1", body)
            self.assertIn("update_history:", body)
            self.assertIn("  - none", body)

    def test_review_status_json_includes_summary_findings_and_history(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            review = root / "review.md"
            review.write_text(
                "# ReviewResult\n\n"
                "## Verdict\nchanges_requested\n\n"
                "## Summary\n要対応。\n\n"
                "## Findings\n"
                "### F1\n"
                "severity: high\n"
                "status: unresolved\n"
                "blocking: true\n\n"
                "blocking finding。\n",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "review status json"])
                register_test_reviewer(db, "human")
                request_output = io.StringIO()
                with redirect_stdout(request_output):
                    main(["--db", str(db), "review", "request", "--task", "task_test", "--from", "codex", "--to", "human", "--reason", "json"])
            request_id = next(line.split(": ", 1)[1] for line in request_output.getvalue().splitlines() if line.startswith("review_request: "))
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "review", "import", "--task", "task_test", "--review", request_id, "--file", str(review)])
            store = Store(db)
            finding = store.latest_for_task("review_findings", "task_test")
            store.close()
            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "--db",
                        str(db),
                        "review",
                        "finding",
                        "update",
                        "--finding",
                        finding["id"],
                        "--status",
                        "addressed",
                        "--reason",
                        "JSON確認用に更新",
                        "--actor",
                        "codex",
                    ]
                )

            output = io.StringIO()
            with redirect_stdout(output):
                main(["--db", str(db), "review", "status", "--task", "task_test", "--format", "json"])

            data = json.loads(output.getvalue())
            self.assertEqual(data["task_id"], "task_test")
            self.assertEqual(data["total_findings"], 1)
            self.assertEqual(data["unresolved_blocking"], 0)
            self.assertEqual(data["finding_status_counts"]["addressed"], 1)
            self.assertEqual(data["review_requests"][0]["id"], request_id)
            self.assertEqual(data["review_findings"][0]["id"], finding["id"])
            self.assertEqual(data["review_findings"][0]["status"], "addressed")
            self.assertEqual(data["review_findings"][0]["update_history"][0]["reason"], "JSON確認用に更新")

    def test_review_finding_update_rejects_invalid_status(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    main(
                        [
                            "--db",
                            str(db),
                            "review",
                            "finding",
                            "update",
                            "--finding",
                            "finding_missing",
                            "--status",
                            "closed",
                            "--reason",
                            "invalid",
                        ]
                    )

    def test_verification_run_records_command_output_and_status(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            script = root / "verify.py"
            script.write_text("import sys\nprint('ok stdout')\nprint('ok stderr', file=sys.stderr)\n", encoding="utf-8")
            command = f'"{sys.executable}" "{script}"'

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "検証実行を記録する",
                    ]
                )
                verification_output = io.StringIO()
                with redirect_stdout(verification_output):
                    main(["--db", str(db), "verification", "run", "--task", "task_test", "--command", command, "--mode", "targeted"])
                status_output = io.StringIO()
                with redirect_stdout(status_output):
                    main(["--db", str(db), "task", "status", "--task", "task_test"])

            store = Store(db)
            run = store.latest_for_task("verification_runs", "task_test")
            store.close()
            self.assertEqual(run["command"], command)
            self.assertIn("ok stdout", run["stdout"])
            self.assertIn("ok stderr", run["stderr"])
            self.assertEqual(run["exit_code"], 0)
            self.assertEqual(run["source"], "nilo_executed")
            self.assertFalse(run["timed_out"])
            self.assertTrue(run["started_at"])
            self.assertTrue(run["finished_at"])
            self.assertIn("working_tree_dirty", run["metadata"])
            self.assertIn("working_tree_files", run["metadata"])
            self.assertEqual(run["metadata"]["verification_mode"], "targeted")
            self.assertIsInstance(run["metadata"]["working_tree_dirty"], bool)
            self.assertIsInstance(run["metadata"]["working_tree_files"], list)
            self.assertIn("mode: targeted", verification_output.getvalue())
            self.assertIn("状態: 検証成功", status_output.getvalue())
            self.assertIn("最新の検証実行:", status_output.getvalue())
            self.assertIn("検証元: nilo_executed", status_output.getvalue())
            self.assertIn("verification_mode: targeted", status_output.getvalue())

    def test_facade_check_records_quick_verification_mode(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            script = root / "verify.py"
            script.write_text("print('quick ok')\n", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "quick verification",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "check",
                        f'"{sys.executable}" "{script}"',
                        "--project",
                        "project_test",
                        "--mode",
                        "quick",
                    ]
                )

            store = Store(db)
            run = store.latest_for_task("verification_runs", "task_test")
            store.close()
            self.assertEqual(run["metadata"]["verification_mode"], "quick")

    def test_facade_check_without_active_task_explains_recovery_path(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])

            with self.assertRaises(SystemExit) as raised:
                main(["--db", str(db), "check", "python --version", "--project", "project_test"])

            message = str(raised.exception)
            self.assertIn("active task not found for project: project_test", message)
            self.assertIn("Before implementation, create or select a Nilo task", message)
            self.assertIn('nilo start "<short title>" --project project_test', message)
            self.assertIn("rerun `nilo check ...` or pass `--task <task_id>`", message)

    def test_facade_check_with_multiple_active_tasks_explains_evidence_target(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_first",
                        "--title",
                        "first task",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_second",
                        "--title",
                        "second task",
                    ]
                )

            with self.assertRaises(SystemExit) as raised:
                main(["--db", str(db), "check", "python --version", "--project", "project_test"])

            message = str(raised.exception)
            self.assertIn("multiple active tasks for project: project_test", message)
            self.assertIn("verification evidence must be attached to exactly one task", message)
            self.assertIn("Pass `--task <task_id>`", message)
            self.assertIn("task_first, task_second", message)
            self.assertIn("do not attach it to an unrelated task", message)

    def test_status_surfaces_dirty_verification_working_tree(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            result = {
                "command": "python -m unittest",
                "cwd": str(Path.cwd()),
                "stdout": "ok\n",
                "stderr": "",
                "exit_code": 0,
                "timed_out": False,
                "timeout_seconds": 300.0,
                "git_head": "abc123",
                "metadata": {
                    "secret_issue_count": 0,
                    "secret_issues": [],
                    "runner": "local",
                    "sandbox": "none",
                    "working_tree_dirty": True,
                    "working_tree_files": ["src/nilo/cli.py", "tests/test_cli.py"],
                    "working_tree_available": True,
                    "snapshot_excluded_paths": [{"path": "dist/app.js", "reason": "ignored", "size": 12}],
                    "snapshot_hashed_paths": ["src/nilo/cli.py"],
                    "snapshot_large_paths": [],
                    "snapshot_binary_paths": [],
                    "snapshot_policy": {"max_file_bytes": 1000000, "ignore_file": ".niloignore", "default_ignore_patterns": True},
                },
                "started_at": "2099-06-17T00:00:00+09:00",
                "finished_at": "2099-06-17T00:00:01+09:00",
                "created_at": "2099-06-17T00:00:01+09:00",
            }

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "dirty tree を表示する",
                    ]
                )
                with patch("nilo.cli_handlers.workflow.run_local_verification", return_value=result):
                    main(["--db", str(db), "verification", "run", "--task", "task_test", "--command", "python -m unittest"])
            task_output = io.StringIO()
            with redirect_stdout(task_output):
                main(["--db", str(db), "task", "status", "--task", "task_test"])
            project_output = io.StringIO()
            with redirect_stdout(project_output):
                main(["--db", str(db), "project", "status", "--project", "project_test", "--verbose"])
            summary_output = io.StringIO()
            with redirect_stdout(summary_output):
                main(["--db", str(db), "project", "summary", "--project", "project_test"])
            json_output = io.StringIO()
            with redirect_stdout(json_output):
                main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])
            summary = json.loads(json_output.getvalue())

            self.assertIn("検証時の作業ツリー: dirty (2 files)", task_output.getvalue())
            self.assertIn("snapshot:", task_output.getvalue())
            self.assertIn("skipped reasons: ignored=1", task_output.getvalue())
            self.assertIn("- src/nilo/cli.py", task_output.getvalue())
            self.assertIn("verification_working_tree: dirty (2 files)", project_output.getvalue())
            self.assertIn("skipped reasons: ignored=1", project_output.getvalue())
            self.assertIn("verification_working_tree: dirty (2 files)", summary_output.getvalue())
            self.assertIn("skipped reasons: ignored=1", summary_output.getvalue())
            self.assertIn("review dirty-tree verification metadata before accepting this task", project_output.getvalue())
            self.assertIn("add --commit only when you want Nilo to commit the accepted changes", project_output.getvalue())
            self.assertTrue(summary["active_tasks"][0]["verification_working_tree_dirty"])
            self.assertEqual(summary["active_tasks"][0]["verification_working_tree_files"], ["src/nilo/cli.py", "tests/test_cli.py"])
            self.assertEqual(summary["active_tasks"][0]["verification_snapshot_policy"]["skipped_reasons"], {"ignored": 1})

    def test_verification_run_records_snapshot_without_evidence_check_link(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            script = root / "verify.py"
            report.write_text(REPORT, encoding="utf-8")
            script.write_text("print('verified')\n", encoding="utf-8")

            with redirect_stdout(io.StringIO()), patch("nilo.cli_handlers.workflow.evaluate_evidence", return_value=("evidence_submitted", [], {"ok": True})):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "検証実行を証跡チェックに接続する",
                    ]
                )
                main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(report)])
                main(
                    [
                        "--db",
                        str(db),
                        "verification",
                        "run",
                        "--task",
                        "task_test",
                        "--command",
                        f'"{sys.executable}" "{script}"',
                    ]
                )

            store = Store(db)
            check = store.latest_for_task("evidence_checks", "task_test")
            run = store.latest_for_task("verification_runs", "task_test")
            store.close()
            self.assertIsNone(check)
            self.assertIsNone(run["evidence_check_id"])
            self.assertTrue(run["git_diff_hash"])
            self.assertIn("git_status_porcelain", run)
            self.assertIsInstance(run["observed_paths"], list)

    def test_verification_run_masks_secrets_before_save(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            script = root / "verify.py"
            script.write_text("print('sk-thislookssecret1234567890')\n", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "検証ログの秘密値をマスクする",
                    ]
                )
                main(["--db", str(db), "verification", "run", "--task", "task_test", "--command", f'"{sys.executable}" "{script}"'])

            store = Store(db)
            run = store.latest_for_task("verification_runs", "task_test")
            failures = store.list_where("failure_logs", "task_id=?", ("task_test",))
            store.close()
            self.assertNotIn("sk-thislookssecret1234567890", run["stdout"])
            self.assertIn("[MASKED:openai_api_key]", run["stdout"])
            self.assertEqual(run["metadata"]["secret_issue_count"], 1)
            self.assertTrue(any(failure["category"] == "secret_detected" for failure in failures))

    def test_verification_run_records_timeout(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            script = root / "sleep.py"
            script.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "検証実行をタイムアウトする",
                    ]
                )
                main(
                    [
                        "--db",
                        str(db),
                        "verification",
                        "run",
                        "--task",
                        "task_test",
                        "--command",
                        f'"{sys.executable}" "{script}"',
                        "--timeout",
                        "0.02",
                    ]
                )

            store = Store(db)
            run = store.latest_for_task("verification_runs", "task_test")
            store.close()
            self.assertTrue(run["timed_out"])
            self.assertIsNone(run["exit_code"])
            self.assertIn("timed out", run["stderr"])

    def test_secret_guard_flags_report_for_human_review(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            report = root / "report.md"
            report.write_text(REPORT.replace("3 passed", "3 passed\nsk-thislookssecret1234567890"), encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを確認する",
                    ]
                )
                main(["--db", str(db), "report", "import", "--task", "task_test", "--file", str(report)])

            store = Store(db)
            check = store.latest_for_task("evidence_checks", "task_test")
            failures = store.list_where("failure_logs", "task_id=?", ("task_test",))
            stored_report = store.latest_for_task("agent_reports", "task_test")
            store.close()
            self.assertIsNone(check)
            self.assertTrue(any(failure["category"] == "secret_detected" for failure in failures))
            self.assertNotIn("sk-thislookssecret1234567890", stored_report["body_md"])
            self.assertIn("[MASKED:openai_api_key]", stored_report["body_md"])

    def test_success_disable_command_is_removed(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
            conn = sqlite3.connect(db)
            try:
                conn.execute(
                    "CREATE TABLE success_patterns (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, state TEXT NOT NULL)"
                )
                conn.execute(
                    "INSERT INTO success_patterns (id, project_id, state) VALUES ('success_existing', 'project_test', 'active')"
                )
                conn.commit()
            finally:
                conn.close()

            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "success", "disable", "--pattern", "success_existing"])

            conn = sqlite3.connect(db)
            try:
                state = conn.execute("SELECT state FROM success_patterns WHERE id='success_existing'").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(state, "active")

    def test_success_pattern_usage_does_not_update_on_instruct(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "CLIフローを実装する",
                    ]
                )
            conn = sqlite3.connect(db)
            try:
                conn.execute(
                    "CREATE TABLE success_patterns (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, success_count INTEGER NOT NULL, last_used_at TEXT NOT NULL)"
                )
                conn.execute(
                    "INSERT INTO success_patterns (id, project_id, success_count, last_used_at) VALUES (?, ?, ?, ?)",
                    ("success_existing", "project_test", 1, "2026-01-01T00:00:00+00:00"),
                )
                conn.commit()
                before = conn.execute(
                    "SELECT success_count, last_used_at FROM success_patterns WHERE id='success_existing'"
                ).fetchone()
            finally:
                conn.close()
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "instruct", "--task", "task_test"])
            conn = sqlite3.connect(db)
            try:
                after = conn.execute(
                    "SELECT success_count, last_used_at FROM success_patterns WHERE id='success_existing'"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(after[0], before[0])
            self.assertEqual(after[1], before[1])

    def test_understanding_gate_blocks_high_risk_instruction_until_approved(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "危険な実装を行う",
                        "--risk",
                        "high",
                    ]
                )

            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "instruct", "--task", "task_test"])

            understanding = Path(directory) / "understanding.md"
            understanding.write_text("# 実装前確認\n\n## 1. タスク目的の理解\n理解した。", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "understanding", "prepare", "--task", "task_test"])
                main(["--db", str(db), "understanding", "import", "--task", "task_test", "--file", str(understanding)])
                main(["--db", str(db), "understanding", "approve", "--task", "task_test"])
                status_output = io.StringIO()
                with redirect_stdout(status_output):
                    main(["--db", str(db), "task", "status", "--task", "task_test"])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "instruct", "--task", "task_test"])

            self.assertIn("状態: 実装承認済み", status_output.getvalue())
            self.assertIn("# 作業指示", output.getvalue())

    def test_understanding_approve_requires_imported_report(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "危険な実装を行う",
                        "--requires-understanding-check",
                    ]
                )
                main(["--db", str(db), "understanding", "prepare", "--task", "task_test"])

            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "understanding", "approve", "--task", "task_test"])

    def test_understanding_import_records_reported_status(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            understanding = root / "understanding.md"
            understanding.write_text("# 実装前確認\n\n## 1. タスク目的の理解\n目的を理解した。", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                main(
                    [
                        "--db",
                        str(db),
                        "task",
                        "create",
                        "--project",
                        "project_test",
                        "--id",
                        "task_test",
                        "--title",
                        "危険な実装を行う",
                        "--requires-understanding-check",
                    ]
                )
                main(["--db", str(db), "understanding", "import", "--task", "task_test", "--file", str(understanding)])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "task", "status", "--task", "task_test"])

            store = Store(db)
            latest = store.latest_for_task("understanding_checks", "task_test")
            store.close()
            self.assertEqual(latest["status"], "understanding_reported")
            self.assertIn("状態: 理解確認報告あり", output.getvalue())


def wait_for_dispatch_capable_reviewer(db: Path, reviewer: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        store = Store(db)
        try:
            rows = store.list_where("review_reviewers", "reviewer=? AND status='available'", (reviewer,))
        finally:
            store.close()
        if any(row["metadata"].get("worker_path") == "nilo mcp reviewer-worker" for row in rows):
            return
        time.sleep(0.05)
    raise AssertionError(f"reviewer worker did not become available: {reviewer}")


def review_body(verdict: str = "changes_requested", summary: str = "Review found an issue.", findings: str | None = None) -> str:
    if findings is None:
        findings = """### F1
severity: high
status: unresolved
file: src/nilo/mcp_server.py
line: 12
blocking: true

Review finding.
"""
    return f"""# ReviewResult

## Verdict
{verdict}

## Summary
{summary}

## Findings
{findings}
"""


def write_fake_dispatch_reviewer(root: Path, verdict: str = "changes_requested", findings: str | None = None) -> Path:
    if findings is None:
        findings = "なし"
    script = root / "fake_reviewer.py"
    script.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "prompt = Path(sys.argv[1]).read_text(encoding='utf-8')\n"
        "assert '# Review Request' in prompt\n"
        "print('# ReviewResult')\n"
        "print('\\n## Verdict')\n"
        f"print({verdict!r})\n"
        "print('\\n## Summary')\n"
        "print('Natural language dispatch completed.')\n"
        "print('\\n## Findings')\n"
        f"print({findings!r})\n",
        encoding="utf-8",
    )
    return script


def write_fake_dispatch_reviewer_script(root: Path, body: str) -> Path:
    script = root / "fake_reviewer.py"
    script.write_text(body, encoding="utf-8")
    return script


def write_dispatch_reviewer_config(
    root: Path,
    reviewers: list[str],
    *,
    command: str | None = None,
    timeout_seconds: float = 10,
) -> Path:
    config_dir = root / ".nilo"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "reviewers.toml"
    blocks = []
    for reviewer in reviewers:
        blocks.append(
            f"[reviewers.{reviewer}]\n"
            'kind = "agent"\n'
            f"command = {json.dumps(command or sys.executable)}\n"
            'args = ["fake_reviewer.py", "{prompt_file}"]\n'
            'working_directory = "{repo_root}"\n'
            "auto_start = true\n"
            f"timeout_seconds = {timeout_seconds}\n"
            "dispatch_capable = true\n"
        )
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


if __name__ == "__main__":
    unittest.main()
