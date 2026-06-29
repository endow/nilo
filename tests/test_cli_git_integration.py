from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nilo.ai_context import AI_CONTEXT_TEXT_MAX_CHARS
from nilo.cli import git_changed_files, main
from nilo.cli_handlers.quality import parse_git_status_porcelain_z
from nilo.store import Store
from nilo.timeutil import now_iso

from tests.test_cli import REPORT, register_test_reviewer


class CliGitIntegrationTests(unittest.TestCase):
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

    def test_task_status_ai_allows_fast_snapshot_verification_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.init_git_with_tags(root, [])
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "fast evidence"])
                    main(["--db", str(db), "check", f'"{sys.executable}" -c "print(1)"', "--task", "task_test"])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "task", "status", "--task", "task_test", "--ai"])
            finally:
                os.chdir(previous_cwd)

        body = output.getvalue()
        self.assertIn("証跡: 提出あり (present)", body)
        self.assertIn("現在タスク完了診断: 完了可能 (completion_allowed)", body)
        self.assertIn("ブロック理由:\n- なし", body)

    def test_task_status_ai_does_not_allow_none_snapshot_verification_evidence(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.init_git_with_tags(root, [])
            db = root / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "none evidence"])
                    main(["--db", str(db), "check", f'"{sys.executable}" -c "print(1)"', "--task", "task_test", "--snapshot", "none"])
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "task", "status", "--task", "task_test", "--ai"])
            finally:
                os.chdir(previous_cwd)

        body = output.getvalue()
        self.assertIn("証跡: 古い証跡 (stale)", body)
        self.assertIn("現在タスク完了診断: 条件未充足 (completion_blocked)", body)
        self.assertIn("- evidence_stale", body)

    def test_task_status_ai_marks_fast_snapshot_stale_after_new_code_path_changes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.init_git_with_tags(root, [])
            db = root / "nilo.db"
            src = root / "src"
            src.mkdir()
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "project", "create", "Nilo", "--id", "project_test"])
                    main(["--db", str(db), "task", "create", "--project", "project_test", "--id", "task_test", "--title", "fast stale"])
                    (src / "first.py").write_text("print('first')\n", encoding="utf-8")
                    subprocess.run(["git", "add", "src/first.py"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    main(["--db", str(db), "check", f'"{sys.executable}" -c "print(1)"', "--task", "task_test"])
                    (src / "second.py").write_text("print('second')\n", encoding="utf-8")
                    subprocess.run(["git", "add", "src/second.py"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "task", "status", "--task", "task_test", "--ai"])
            finally:
                os.chdir(previous_cwd)

        body = output.getvalue()
        self.assertIn("証跡: 古い証跡 (stale)", body)
        self.assertIn("現在タスク完了診断: 条件未充足 (completion_blocked)", body)
        self.assertIn("- evidence_stale", body)

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

    def test_ai_context_surfaces_are_compact_and_json_serializable(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "nilo.db"
            project_id = root.name
            previous_cwd = Path.cwd()
            try:
                self.init_git_with_tags(root, [])
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
                self.assertIn("active_task: task_ai [planned] AI compact", body)
                self.assertIn("latest_verification: status=missing", body)
                self.assertIn("latest_review: unresolved=1", body)
                self.assertIn("detail_commands:", body)
                self.assertNotIn("完了可否", body)
                self.assertLess(len(body), 1200)

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
                self.assertTrue(data["compact"])
                self.assertEqual(data["active_task"]["id"], "task_ai")
                self.assertEqual(data["latest_verification"]["status"], "missing")
                self.assertEqual(data["latest_review"]["unresolved_count"], 1)
                self.assertEqual(data["blockers"]["count"], 2)

                verbose_status_json = io.StringIO()
                with redirect_stdout(verbose_status_json):
                    main(["--db", str(db), "status", "--ai", "--verbose", "--json"])
                verbose_data = json.loads(verbose_status_json.getvalue())
                self.assertEqual(verbose_data["current_task"]["task"]["id"], "task_ai")
                self.assertEqual(verbose_data["current_task"]["evidence"]["status"], "missing")
                self.assertNotEqual(verbose_data["current_task"]["git"]["git_diff_hash"], "__not_computed__")
                self.assertTrue(verbose_data["current_task"]["git"]["diff_hash_computed"])
                self.assertEqual(verbose_data["current_task"]["review"]["unresolved_count"], 1)
                self.assertFalse(verbose_data["current_task"]["completion"]["allowed"])

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
                self.assertIn("latest_verification: status=present", status_body)
                verbose_status_with_report = io.StringIO()
                with redirect_stdout(verbose_status_with_report):
                    main(["--db", str(db), "status", "--ai", "--verbose"])
                verbose_status_body = verbose_status_with_report.getvalue()
                self.assertIn("証跡: 提出あり (present)", verbose_status_body)
                self.assertIn("作業規模の判定:", verbose_status_body)
                self.assertIn("複数ファイルだけでは roadmap 扱いにせず", verbose_status_body)
                self.assertIn("複数機能・複数実装トラック", verbose_status_body)
                self.assertIn("CLI", verbose_status_body)
                self.assertIn("roadmap", verbose_status_body)

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
                self.assertIn('record it with `nilo check --task <task_id> "..." --mode quick|targeted|full`', help_body)
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
                    main(["--db", str(db), "roadmap", "accept", "--revision", revision_id, "--reason", "評価するため", "--actor", "human", "--human-confirm", "--decision-note", "test human decision"])
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
            self.assertIn("- status: needs_verification", body)
            self.assertIn("- closure_ready: false", body)
            self.assertIn("- [needs_verification] accepted commitment の達成状況を確認できる", body)
            self.assertIn("related_tasks: task_assess", body)
            self.assertIn("latest_verification:", body)
            self.assertIn("(passed)", body)

            summary_output = io.StringIO()
            with redirect_stdout(summary_output):
                main(["--db", str(db), "project", "summary", "--project", "project_test", "--format", "json"])
            summary = json.loads(summary_output.getvalue())
            self.assertEqual(summary["roadmap_assessments"][0]["status"], "needs_verification")
            self.assertFalse(summary["roadmap_assessments"][0]["closure_ready"])
            self.assertEqual(summary["roadmap_assessments"][0]["related_tasks"][0]["task_id"], "task_assess")
            self.assertEqual(summary["roadmap_agent_state"]["commitment_id"], commitment_id)
            self.assertEqual(summary["roadmap_agent_state"]["work_status"], "active")
            self.assertEqual(summary["roadmap_agent_state"]["evidence_status"], "incomplete")
            self.assertEqual(summary["roadmap_agent_state"]["verification_status"], "incomplete")
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
            self.assertIn("review the diff", summary["next_actions"][0])
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
            self.assertIn("closure_ready: false", text_summary_body)
            self.assertNotIn("roadmap_agent_state:", text_summary_body)
            self.assertNotIn("roadmap_agent_next_actions:", text_summary_body)
            self.assertNotIn("action_id: close_roadmap_commitment", text_summary_body)
            self.assertNotIn("--actor ai", text_summary_body)
            self.assertIn("task_assess:", text_summary_body)
            self.assertIn("review the diff", text_summary_body)

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
            self.assertIn("review the diff", status_body)
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
                        "--type",
                        "documentation",
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
                            "--actor",
                            "human",
                            "--human-confirm",
                            "--decision-note",
                            "test human decision",
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


if __name__ == "__main__":
    unittest.main()
