from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nilo.ai_context import project_ai_context, render_ai_context_text, task_ai_context
from nilo.cli import main
from nilo.cli_handlers import release as release_handler
from nilo.snapshot import compact_snapshot, current_git_snapshot, snapshot_columns
from nilo.state_audit import audit_task
from nilo.state_audit import audit_workflow
from nilo.store import Store
from nilo.task_logic import projected_task_status
from nilo.timeutil import now_iso
from nilo.workflow_context import approve_pending_public_operations, mark_release_commit_recorded, public_operations_for_release, workflow_context


def run_git(root: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return completed.stdout.strip()


def init_repo(root: Path) -> None:
    run_git(root, "init")
    run_git(root, "config", "user.email", "test@example.com")
    run_git(root, "config", "user.name", "Test")
    root.joinpath("tracked.txt").write_text("initial\n", encoding="utf-8")
    run_git(root, "add", "tracked.txt")
    run_git(root, "commit", "-m", "initial")


def verification_row(task_id: str, root: Path) -> dict:
    snapshot = current_git_snapshot(root)
    now = now_iso()
    return {
        "id": f"verification_{task_id}",
        "task_id": task_id,
        "evidence_check_id": None,
        "source": "nilo_executed",
        "command": "python -m unittest tests.test_release_workflow",
        "cwd": str(root),
        "stdout": "ok",
        "stderr": "",
        "exit_code": 0,
        "timed_out": False,
        "timeout_seconds": 30,
        **snapshot_columns(snapshot),
        "metadata": {"verification_mode": "targeted"},
        "started_at": now,
        "finished_at": now,
        "created_at": now,
    }


def release_verification_result(root: Path, *, command: str = release_handler.RELEASE_FULL_CHECK_COMMAND, snapshot_mode: str = "full") -> dict:
    snapshot = current_git_snapshot(root)
    now = now_iso()
    return {
        "source": "nilo_executed",
        "command": command,
        "cwd": str(root),
        "stdout": "ok",
        "stderr": "",
        "exit_code": 0,
        "timed_out": False,
        "timeout_seconds": 600,
        **snapshot_columns(snapshot),
        "metadata": {"snapshot_mode": snapshot_mode, "requested_snapshot_mode": snapshot_mode},
        "started_at": now,
        "finished_at": now,
        "created_at": now,
    }


def failed_release_verification_result(
    root: Path, *, command: str = release_handler.RELEASE_FULL_CHECK_COMMAND, snapshot_mode: str = "full"
) -> dict:
    result = release_verification_result(root, command=command, snapshot_mode=snapshot_mode)
    result["stdout"] = "failed"
    result["stderr"] = "shard failed"
    result["exit_code"] = 1
    return result


def full_release_verification_row(task_id: str, root: Path, *, target_version: str = "0.3.1") -> dict:
    verified = release_verification_result(root, command=release_handler.RELEASE_FULL_CHECK_COMMAND, snapshot_mode="full")
    verified["metadata"]["verification_mode"] = "full"
    verified["metadata"]["release_prepare"] = True
    verified["metadata"]["release_target_version"] = target_version
    verified["metadata"]["release_effective_dirty_hash"] = release_handler._release_effective_worktree_hash(root)
    return {"id": f"verification_full_{task_id}", "task_id": task_id, "evidence_check_id": None, **verified}


def force_waiting_public_approval(store: Store, root: Path, project_id: str, task_id: str, *, target_version: str = "0.3.1") -> str:
    context = workflow_context(store, project_id)
    run = store.get("recipe_runs", context["recipe_run_id"])
    metadata = {**(run["metadata"] or {}), "target_version": target_version, "commit_sha": run_git(root, "rev-parse", "HEAD")}
    store.update(
        "recipe_runs",
        run["id"],
        {
            "status": "waiting_public_approval",
            "current_step": "public_release",
            "completed_steps": ["prepare_version", "run_required_checks", "commit"],
            "pending_steps": ["tag", "push_main", "push_tag", "create_github_release", "verify_release", "complete"],
            "pending_public_operations": public_operations_for_release(target_version),
            "metadata": metadata,
            "updated_at": now_iso(),
        },
    )
    return run["id"]


def install_fake_release_tools(root: Path) -> Path:
    real_git = shutil.which("git")
    if not real_git:
        raise RuntimeError("git not found")
    fake_bin = root / ".git" / "fake-bin"
    fake_bin.mkdir()
    log = root / ".git" / "release-tools.log"
    git_script = fake_bin / "git"
    git_script.write_text(
        f"""#!/bin/sh
if [ "$1" = "push" ]; then
  echo "git $@" >> "{log}"
  exit 0
fi
exec "{real_git}" "$@"
""",
        encoding="utf-8",
    )
    gh_script = fake_bin / "gh"
    gh_script.write_text(
        f"""#!/bin/sh
echo "gh $@" >> "{log}"
if [ "$1" = "release" ] && [ "$2" = "create" ]; then
  echo "https://github.com/example/project/releases/tag/$3"
  exit 0
fi
if [ "$1" = "release" ] && [ "$2" = "view" ]; then
  echo "https://github.com/example/project/releases/tag/$3"
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    git_script.chmod(0o755)
    gh_script.chmod(0o755)
    return fake_bin


class ReleaseWorkflowTests(unittest.TestCase):
    def create_project(self, db: Path, project_id: str) -> None:
        with redirect_stdout(io.StringIO()):
            main(["--db", str(db), "project", "create", "Nilo", "--id", project_id])

    def write_release_project_files(self, root: Path, version: str) -> None:
        root.joinpath("pyproject.toml").write_text(
            f'[project]\nname = "nilo"\nversion = "{version}"\n',
            encoding="utf-8",
        )
        root.joinpath("src/nilo").mkdir(parents=True)
        root.joinpath("src/nilo/__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
        run_git(root, "add", "pyproject.toml", "src/nilo/__init__.py")
        run_git(root, "commit", "-m", "project files")

    def test_release_prepare_runs_changed_check_commits_and_defers_public_gate(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            self.write_release_project_files(root, "0.3.0")
            db = root / ".git" / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                self.create_project(db, root.name)
                output = io.StringIO()
                with patch(
                    "nilo.cli_handlers.release.run_local_verification",
                    side_effect=lambda command, cwd, timeout, **kwargs: release_verification_result(cwd, command=command, snapshot_mode=kwargs.get("snapshot_mode", "fast")),
                ) as verification_check, patch(
                    "nilo.cli_handlers.release._run_lightweight_post_commit_checks"
                ) as lightweight_checks:
                    with redirect_stdout(output):
                        main(["--db", str(db), "release", "prepare", "--project", root.name, "--target-version", "0.3.1"])
                text = output.getvalue()
                self.assertIn("changed_check: PYTHONPATH=src python tests/run_shards.py --changed --jobs auto", text)
                self.assertIn("changed_check_exit_code: 0", text)
                self.assertIn("full_check: deferred", text)
                self.assertNotIn("python -m unittest discover tests", text)
                self.assertIn("recipe_run: active", text)
                self.assertIn("required_checks: full_check_deferred", text)
                self.assertIn("pending_public_operations: none", text)
                self.assertNotIn("pending_public_operations: created", text)
                self.assertNotIn("publish: nilo release publish --project", text)
                verification_check.assert_called_once_with(
                    release_handler.RELEASE_CHANGED_CHECK_COMMAND,
                    root,
                    600,
                    snapshot_mode="fast",
                )
                lightweight_checks.assert_called_once_with(root.name, root, str(db))
                self.assertEqual(root.joinpath("pyproject.toml").read_text(encoding="utf-8").count('version = "0.3.1"'), 1)
                self.assertIn('__version__ = "0.3.1"', root.joinpath("src/nilo/__init__.py").read_text(encoding="utf-8"))
                self.assertTrue(root.joinpath("docs/releases/0.3.1.md").exists())
                self.assertEqual(run_git(root, "status", "--porcelain=v1", "--untracked-files=all"), "")
                store = Store(db)
                try:
                    context = workflow_context(store, root.name)
                    self.assertEqual(context["status"], "active")
                    self.assertEqual(context["pending_public_operations"], [])
                    run = store.get("recipe_runs", context["recipe_run_id"])
                    metadata = run["metadata"]
                    self.assertTrue(metadata["commit_sha"])
                    self.assertEqual(metadata["committed_files"], ["docs/releases/0.3.1.md", "pyproject.toml", "src/nilo/__init__.py"])
                    self.assertFalse(metadata["post_commit_full_check_reused"])
                    self.assertEqual(metadata["release_prepare_check_mode"], "changed")
                    self.assertEqual(metadata["required_full_check"]["status"], "deferred")
                    self.assertEqual(metadata["required_full_check"]["mode"], "changed")
                    self.assertFalse(metadata["required_checks_passed"])
                    verification = store.latest_for_task("verification_runs", context["task_id"])
                    self.assertEqual(verification["command"], "PYTHONPATH=src python tests/run_shards.py --changed --jobs auto")
                    verification_metadata = verification["metadata"]
                    self.assertEqual(verification_metadata["verification_mode"], "changed")
                    self.assertTrue(verification_metadata["release_prepare"])
                    self.assertEqual(verification_metadata["release_target_version"], "0.3.1")
                    self.assertEqual(verification_metadata["requested_snapshot_mode"], "fast")
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

    def test_release_prepare_reuses_full_check_instead_of_changed_check(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            self.write_release_project_files(root, "0.3.0")
            db = root / ".git" / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                self.create_project(db, root.name)

                def reusable_full_check(store: Store, task_id: str, cwd: Path, *, target_version: str) -> dict:
                    verified = release_verification_result(cwd, command=release_handler.RELEASE_FULL_CHECK_COMMAND, snapshot_mode="full")
                    verified["metadata"]["verification_mode"] = "full"
                    verified["metadata"]["release_prepare"] = True
                    verified["metadata"]["release_target_version"] = target_version
                    verified["metadata"]["release_effective_dirty_hash"] = release_handler._release_effective_worktree_hash(cwd)
                    row = {
                        "id": "verification_reused_full",
                        "task_id": task_id,
                        "evidence_check_id": None,
                        **verified,
                    }
                    store.insert("verification_runs", row)
                    return {**row, "reuse_reason": "current_full_check", "snapshot_relation": "current_snapshot"}

                output = io.StringIO()
                with patch("nilo.cli_handlers.release.reusable_full_verification_for_release", side_effect=reusable_full_check), patch(
                    "nilo.cli_handlers.release.run_local_verification"
                ) as verification_check, patch("nilo.cli_handlers.release._run_lightweight_post_commit_checks"):
                    with redirect_stdout(output):
                        main(["--db", str(db), "release", "prepare", "--project", root.name, "--target-version", "0.3.1"])
                text = output.getvalue()
                verification_check.assert_not_called()
                self.assertIn("verification_run: verification_reused_full", text)
                self.assertIn("verification_reused: current_full_check", text)
                self.assertIn("full_check: reused", text)
                self.assertNotIn("changed_check:", text)
                store = Store(db)
                try:
                    context = workflow_context(store, root.name)
                    metadata = store.get("recipe_runs", context["recipe_run_id"])["metadata"]
                    self.assertTrue(metadata["post_commit_full_check_reused"])
                    self.assertEqual(metadata["release_prepare_check_mode"], "full")
                    self.assertEqual(metadata["required_full_check"]["status"], "satisfied")
                    self.assertEqual(metadata["required_full_check"]["mode"], "full")
                    self.assertEqual(metadata["required_full_check"]["verification_id"], "verification_reused_full")
                    self.assertTrue(metadata["required_full_check"]["reused"])
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

    def test_release_prepare_reuses_full_check_for_release_metadata_only_changes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            self.write_release_project_files(root, "0.3.0")
            db = root / ".git" / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                self.create_project(db, root.name)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "recipe", "run", "release", "--project", root.name, "--var", "target_version=0.3.1"])
                store = Store(db)
                try:
                    task_id = workflow_context(store, root.name)["task_id"]
                    store.insert("verification_runs", full_release_verification_row(task_id, root))
                finally:
                    store.close()

                output = io.StringIO()
                with patch("nilo.cli_handlers.release.run_local_verification") as verification_check, patch("nilo.cli_handlers.release._run_lightweight_post_commit_checks"):
                    with redirect_stdout(output):
                        main(["--db", str(db), "release", "prepare", "--project", root.name, "--target-version", "0.3.1"])
                text = output.getvalue()
                verification_check.assert_not_called()
                self.assertIn("verification_reused: release_metadata_only_changes", text)
                self.assertIn("full_check: reused", text)
                self.assertNotIn("changed_check:", text)
                store = Store(db)
                try:
                    context = workflow_context(store, root.name)
                    self.assertEqual(context["status"], "waiting_public_approval")
                    metadata = store.get("recipe_runs", context["recipe_run_id"])["metadata"]
                    self.assertTrue(metadata["post_commit_full_check_reused"])
                    self.assertEqual(metadata["release_prepare_check_mode"], "full")
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

    def test_release_prepare_already_satisfied_does_not_rerun_verification(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            self.write_release_project_files(root, "0.3.0")
            db = root / ".git" / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                self.create_project(db, root.name)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "recipe", "run", "release", "--project", root.name, "--var", "target_version=0.3.1"])
                store = Store(db)
                try:
                    context = workflow_context(store, root.name)
                    task_id = context["task_id"]
                    store.insert("verification_runs", full_release_verification_row(task_id, root))
                    updated = mark_release_commit_recorded(
                        store,
                        task_id=task_id,
                        commit_sha=run_git(root, "rev-parse", "HEAD"),
                        commit_message="Release 0.3.1",
                        post_commit_snapshot=compact_snapshot(current_git_snapshot(root)),
                    )
                    metadata = {**(updated["metadata"] or {})}
                    metadata["required_full_check"] = {
                        "status": "satisfied",
                        "verification_id": f"verification_full_{task_id}",
                        "reused": True,
                        "git_head": run_git(root, "rev-parse", "HEAD"),
                        "command": release_handler.RELEASE_FULL_CHECK_COMMAND,
                    }
                    store.update("recipe_runs", updated["id"], {"metadata": metadata, "updated_at": now_iso()})
                finally:
                    store.close()

                output = io.StringIO()
                with patch("nilo.cli_handlers.release.run_local_verification") as verification_check:
                    with redirect_stdout(output):
                        main(["--db", str(db), "release", "prepare", "--project", root.name, "--target-version", "0.3.1"])
                verification_check.assert_not_called()
                body = output.getvalue()
                self.assertIn("release prepare: already satisfied", body)
                self.assertIn("next_action: publish approval required", body)
                self.assertIn("publish: nilo release publish --project", body)
            finally:
                os.chdir(previous_cwd)

    def test_release_only_non_execution_changes_allows_release_metadata(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            self.write_release_project_files(root, "0.3.0")
            base = run_git(root, "rev-parse", "HEAD")

            root.joinpath("docs/releases").mkdir(parents=True)
            root.joinpath("docs/releases/0.3.1.md").write_text("release notes\n", encoding="utf-8")
            self.assertTrue(release_handler.release_only_non_execution_changes(root, "0.3.1", base))

            run_git(root, "add", "docs/releases/0.3.1.md")
            run_git(root, "commit", "-m", "Add release notes")
            base = run_git(root, "rev-parse", "HEAD")
            root.joinpath("pyproject.toml").write_text('[project]\nname = "nilo"\nversion = "0.3.1"\n', encoding="utf-8")
            self.assertTrue(release_handler.release_only_non_execution_changes(root, "0.3.1", base))

            run_git(root, "add", "pyproject.toml")
            run_git(root, "commit", "-m", "Update pyproject version")
            base = run_git(root, "rev-parse", "HEAD")
            root.joinpath("src/nilo/__init__.py").write_text('__version__ = "0.3.1"\n', encoding="utf-8")
            self.assertTrue(release_handler.release_only_non_execution_changes(root, "0.3.1", base))

    def test_release_only_non_execution_changes_rejects_execution_changes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            self.write_release_project_files(root, "0.3.0")
            base = run_git(root, "rev-parse", "HEAD")

            root.joinpath("pyproject.toml").write_text(
                '[project]\nname = "nilo"\nversion = "0.3.1"\ndependencies = ["pytest"]\n',
                encoding="utf-8",
            )
            self.assertFalse(release_handler.release_only_non_execution_changes(root, "0.3.1", base))

            run_git(root, "checkout", "--", "pyproject.toml")
            root.joinpath("src/nilo/runtime.py").write_text("print('changed')\n", encoding="utf-8")
            self.assertFalse(release_handler.release_only_non_execution_changes(root, "0.3.1", base))

            root.joinpath("src/nilo/runtime.py").unlink()
            root.joinpath("tests").mkdir(exist_ok=True)
            root.joinpath("tests/test_runtime.py").write_text("def test_runtime():\n    assert True\n", encoding="utf-8")
            self.assertFalse(release_handler.release_only_non_execution_changes(root, "0.3.1", base))

    def test_release_prepare_rejects_target_version_mismatch_for_active_run(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            self.write_release_project_files(root, "0.3.0")
            db = root / ".git" / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                self.create_project(db, root.name)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "recipe", "run", "release", "--project", root.name, "--var", "target_version=0.3.1"])
                with self.assertRaises(SystemExit) as raised:
                    with redirect_stdout(io.StringIO()):
                        main(["--db", str(db), "release", "prepare", "--project", root.name, "--target-version", "0.3.2"])
                self.assertIn("active release recipe target_version is 0.3.1; got 0.3.2", str(raised.exception))
                self.assertNotIn("0.3.2", root.joinpath("pyproject.toml").read_text(encoding="utf-8"))
                self.assertEqual(run_git(root, "status", "--porcelain=v1", "--untracked-files=all"), "")
            finally:
                os.chdir(previous_cwd)

    def test_release_recipe_run_records_task_content_in_japanese(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            db = root / ".git" / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                self.create_project(db, root.name)
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["--db", str(db), "recipe", "run", "release", "--project", root.name, "--var", "target_version=0.3.1"])
                task_id = output.getvalue().strip().splitlines()[-1]
                store = Store(db)
                try:
                    task = store.get("tasks", task_id)
                    self.assertEqual(task["title"], "リリース 0.3.1")
                    self.assertIn("指定されたリリースバージョン", task["description"])
                    self.assertIn("プロジェクトの version source が 0.3.1 に更新されている。", task["acceptance_criteria"])
                    self.assertNotIn("Prepare the requested release version", task["description"])
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

    def test_task_complete_commit_keeps_verified_dirty_tree_evidence_current(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            db = root / ".git" / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                self.create_project(db, root.name)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "task", "create", "--project", root.name, "--id", "task_release", "--title", "Release work"])
                root.joinpath("tracked.txt").write_text("verified change\n", encoding="utf-8")
                store = Store(db)
                try:
                    store.insert("verification_runs", verification_row("task_release", root))
                finally:
                    store.close()
                output = io.StringIO()
                with redirect_stdout(output):
                    main(
                        [
                            "--db",
                            str(db),
                            "task",
                            "complete",
                            "--task",
                            "task_release",
                            "--reason",
                            "verified",
                            "--actor",
                            "ai",
                            "--commit",
                            "--commit-message",
                            "Complete release work",
                        ]
                    )
                self.assertIn("commit: created", output.getvalue())
                store = Store(db)
                try:
                    task = store.get("tasks", "task_release")
                    self.assertEqual(projected_task_status(store, task, current_snapshot=current_git_snapshot(root)), "completed_by_ai")
                    self.assertEqual(task_ai_context(store, "task_release", cwd=root)["evidence"]["status"], "current")
                    self.assertFalse([item for item in audit_task(store, "task_release", cwd=root) if item["severity"] == "error"])
                    completion = store.latest_for_task("task_completions", "task_release")
                    metadata = completion["completed_snapshot"]["commit_transition"]
                    self.assertTrue(metadata["committed_from_verified_dirty_tree"])
                    self.assertTrue(metadata["commit_sha"])
                    self.assertEqual(metadata["commit_message"], "Complete release work")
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

    def test_commit_after_extra_change_makes_completion_need_review(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            db = root / ".git" / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                self.create_project(db, root.name)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "task", "create", "--project", root.name, "--id", "task_release", "--title", "Release work"])
                root.joinpath("tracked.txt").write_text("verified change\n", encoding="utf-8")
                store = Store(db)
                try:
                    store.insert("verification_runs", verification_row("task_release", root))
                finally:
                    store.close()
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "task", "complete", "--task", "task_release", "--reason", "verified", "--actor", "ai", "--commit"])
                root.joinpath("tracked.txt").write_text("post commit change\n", encoding="utf-8")
                store = Store(db)
                try:
                    task = store.get("tasks", "task_release")
                    self.assertEqual(projected_task_status(store, task, current_snapshot=current_git_snapshot(root)), "completion_needs_review")
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

    def test_commit_metadata_mismatch_is_audit_error(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            db = root / ".git" / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                self.create_project(db, root.name)
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "task", "create", "--project", root.name, "--id", "task_release", "--title", "Release work"])
                root.joinpath("tracked.txt").write_text("verified change\n", encoding="utf-8")
                store = Store(db)
                try:
                    store.insert("verification_runs", verification_row("task_release", root))
                finally:
                    store.close()
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "task", "complete", "--task", "task_release", "--reason", "verified", "--actor", "ai", "--commit"])
                store = Store(db)
                try:
                    completion = store.latest_for_task("task_completions", "task_release")
                    snapshot = completion["completed_snapshot"]
                    snapshot["commit_transition"]["pre_commit_snapshot"]["git_diff_hash"] = "mismatch"
                    store.update("task_completions", completion["id"], {"completed_snapshot": snapshot})
                    findings = audit_task(store, "task_release", cwd=root)
                    mismatch = next(item for item in findings if item["code"] == "completion_commit_verified_diff_mismatch")
                    self.assertIn("verified_diff_hash=", mismatch["message"])
                    self.assertIn("pre_commit_snapshot.git_diff_hash=mismatch", mismatch["message"])
                    self.assertIn("再検証", mismatch["remediation"])
                    self.assertIn("既存 dirty files", mismatch["remediation"])
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

    def test_release_recipe_context_blocks_unrelated_next_until_public_approval(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            db = root / ".git" / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                self.create_project(db, root.name)
                recipe_output = io.StringIO()
                with redirect_stdout(recipe_output):
                    main(["--db", str(db), "recipe", "run", "release", "--project", root.name, "--var", "target_version=0.3.1"])
                release_task_id = recipe_output.getvalue().strip().splitlines()[-1]
                with redirect_stdout(io.StringIO()):
                    main(["--db", str(db), "task", "create", "--project", root.name, "--id", "task_unrelated", "--title", "Unrelated cleanup"])

                next_output = io.StringIO()
                with redirect_stdout(next_output):
                    main(["--db", str(db), "next", "--project", root.name])
                self.assertIn("next_action: run_release_prepare", next_output.getvalue())
                self.assertIn("command: nilo release prepare --project", next_output.getvalue())
                self.assertIn("details: nilo status --ai --verbose --project", next_output.getvalue())
                self.assertNotIn("Unrelated cleanup", next_output.getvalue())
                verbose_next_output = io.StringIO()
                with redirect_stdout(verbose_next_output):
                    main(["--db", str(db), "next", "--project", root.name, "--verbose"])
                self.assertIn("workflow_context:", verbose_next_output.getvalue())

                store = Store(db)
                try:
                    store.insert("verification_runs", full_release_verification_row(release_task_id, root))
                    mark_release_commit_recorded(
                        store,
                        task_id=release_task_id,
                        commit_sha="abc123",
                        commit_message="Release 0.3.1",
                        post_commit_snapshot=compact_snapshot(current_git_snapshot(root)),
                    )
                    context = workflow_context(store, root.name)
                    self.assertEqual(context["status"], "waiting_public_approval")
                    self.assertEqual(context["next_step"], "await_public_operation_confirmation")
                    self.assertEqual([item["operation"] for item in context["pending_public_operations"]], ["create_tag", "push_branch", "push_tag", "create_github_release"])
                    status_context = project_ai_context(store, root.name, cwd=root, verbose=True)
                    self.assertEqual(status_context["current_task"]["task"]["id"], release_task_id)
                finally:
                    store.close()

                gated_output = io.StringIO()
                with redirect_stdout(gated_output):
                    main(["--db", str(db), "next", "--project", root.name])
                self.assertIn("next_action: await_public_approval", gated_output.getvalue())
                self.assertIn("required_approval_text: v0.3.1 を tag/push/release して", gated_output.getvalue())
                self.assertIn("command_after_approval: nilo release publish --project", gated_output.getvalue())
                self.assertIn("details: nilo status --ai --verbose --project", gated_output.getvalue())
                self.assertNotIn("pending_public_operations:", gated_output.getvalue())
                verbose_gated_output = io.StringIO()
                with redirect_stdout(verbose_gated_output):
                    main(["--db", str(db), "next", "--project", root.name, "--verbose"])
                self.assertIn("pending_public_operations:", verbose_gated_output.getvalue())
                self.assertIn("v0.3.1 を tag/push/release して", verbose_gated_output.getvalue())
                self.assertIn("nilo release publish --project", verbose_gated_output.getvalue())
                self.assertNotIn("Unrelated cleanup", gated_output.getvalue())

                store = Store(db)
                try:
                    self.assertEqual(store.get("tasks", "task_unrelated")["status"], "planned")
                    run = approve_pending_public_operations(store, project_id=root.name, approval="v0.3.1 を tag/push/release して", release_url="https://example.test/release/v0.3.1")
                    self.assertEqual(run["status"], "active")
                    self.assertEqual(run["current_step"], "verify_release")
                    self.assertEqual(run["pending_public_operations"], [])
                    active_context = workflow_context(store, root.name)
                    self.assertEqual(active_context["type"], "recipe_run")
                    self.assertEqual(active_context["next_step"], "verify_release")
                    run = approve_pending_public_operations(
                        store,
                        project_id=root.name,
                        approval="v0.3.1 を tag/push/release して",
                        release_url="https://example.test/release/v0.3.1",
                        executed=True,
                    )
                    self.assertEqual(run["status"], "completed")
                    self.assertEqual(run["pending_public_operations"], [])
                    completed_context = workflow_context(store, root.name)
                    self.assertEqual(completed_context["type"], "project")
                    rendered = render_ai_context_text(project_ai_context(store, root.name, cwd=root, verbose=True))
                    self.assertIn("Release recipe completed:", rendered)
                    self.assertIn("github_release: https://example.test/release/v0.3.1", rendered)
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

    def test_release_commit_without_required_checks_does_not_open_public_gate(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            db = root / ".git" / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                self.create_project(db, root.name)
                recipe_output = io.StringIO()
                with redirect_stdout(recipe_output):
                    main(["--db", str(db), "recipe", "run", "release", "--project", root.name, "--var", "target_version=0.3.1"])
                release_task_id = recipe_output.getvalue().strip().splitlines()[-1]
                store = Store(db)
                try:
                    run = mark_release_commit_recorded(
                        store,
                        task_id=release_task_id,
                        commit_sha="abc123",
                        commit_message="Release 0.3.1",
                        post_commit_snapshot=compact_snapshot(current_git_snapshot(root)),
                    )
                    self.assertEqual(run["status"], "active")
                    self.assertEqual(run["current_step"], "run_required_checks")
                    self.assertEqual(run["pending_public_operations"], [])
                    context = workflow_context(store, root.name)
                    self.assertEqual(context["next_step"], "run_required_checks")
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

    def test_approve_public_execute_runs_release_operations_and_completes_recipe(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            db = root / ".git" / "nilo.db"
            previous_cwd = Path.cwd()
            previous_path = os.environ.get("PATH", "")
            try:
                os.chdir(root)
                self.create_project(db, root.name)
                root.joinpath("docs/releases").mkdir(parents=True)
                root.joinpath("docs/releases/0.3.1.md").write_text("release notes\n", encoding="utf-8")
                run_git(root, "add", "docs/releases/0.3.1.md")
                run_git(root, "commit", "-m", "Add release notes")
                recipe_output = io.StringIO()
                with redirect_stdout(recipe_output):
                    main(["--db", str(db), "recipe", "run", "release", "--project", root.name, "--var", "target_version=0.3.1"])
                release_task_id = recipe_output.getvalue().strip().splitlines()[-1]
                store = Store(db)
                try:
                    store.insert("verification_runs", full_release_verification_row(release_task_id, root))
                    run = mark_release_commit_recorded(
                        store,
                        task_id=release_task_id,
                        commit_sha=run_git(root, "rev-parse", "HEAD"),
                        commit_message="Release 0.3.1",
                        post_commit_snapshot=compact_snapshot(current_git_snapshot(root)),
                    )
                    self.assertEqual(run["status"], "waiting_public_approval")
                finally:
                    store.close()

                fake_bin = install_fake_release_tools(root)
                os.environ["PATH"] = f"{fake_bin}{os.pathsep}{previous_path}"
                output = io.StringIO()
                with redirect_stdout(output):
                    main(
                        [
                            "--db",
                            str(db),
                            "recipe",
                            "approve-public",
                            "--project",
                            root.name,
                            "--approval",
                            "v0.3.1 を tag/push/release して",
                            "--execute",
                        ]
                    )

                text = output.getvalue()
                self.assertIn("release_recipe: completed", text)
                self.assertIn("github_release: https://github.com/example/project/releases/tag/v0.3.1", text)
                self.assertEqual(run_git(root, "rev-parse", "--verify", "refs/tags/v0.3.1"), run_git(root, "rev-parse", "HEAD"))
                log_text = root.joinpath(".git/release-tools.log").read_text(encoding="utf-8")
                self.assertIn("git push origin main", log_text)
                self.assertIn("git push origin v0.3.1", log_text)
                self.assertIn("gh release create v0.3.1 --title v0.3.1 --notes-file docs/releases/0.3.1.md", log_text)
                store = Store(db)
                try:
                    context = workflow_context(store, root.name)
                    self.assertEqual(context["type"], "project")
                    summary = context["latest_completed_release"]
                    self.assertEqual(summary["github_release"], "https://github.com/example/project/releases/tag/v0.3.1")
                    self.assertEqual(summary["release_task"], "completed")
                    completion = store.latest_for_task("task_completions", release_task_id)
                    events = store.list_where("transition_events", "entity_id=? AND transition='complete_task'", (release_task_id,))
                    self.assertTrue(completion)
                    self.assertEqual(events[-1]["related_ids"]["completion"], completion["id"])
                finally:
                    store.close()
            finally:
                os.environ["PATH"] = previous_path
                os.chdir(previous_cwd)

    def test_release_publish_runs_full_check_before_public_operations_when_missing(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            db = root / ".git" / "nilo.db"
            previous_cwd = Path.cwd()
            previous_path = os.environ.get("PATH", "")
            try:
                os.chdir(root)
                self.create_project(db, root.name)
                root.joinpath("docs/releases").mkdir(parents=True)
                root.joinpath("docs/releases/0.3.1.md").write_text("release notes\n", encoding="utf-8")
                run_git(root, "add", "docs/releases/0.3.1.md")
                run_git(root, "commit", "-m", "Release 0.3.1")
                recipe_output = io.StringIO()
                with redirect_stdout(recipe_output):
                    main(["--db", str(db), "recipe", "run", "release", "--project", root.name, "--var", "target_version=0.3.1"])
                release_task_id = recipe_output.getvalue().strip().splitlines()[-1]
                store = Store(db)
                try:
                    run_id = force_waiting_public_approval(store, root, root.name, release_task_id)
                finally:
                    store.close()

                fake_bin = install_fake_release_tools(root)
                os.environ["PATH"] = f"{fake_bin}{os.pathsep}{previous_path}"
                output = io.StringIO()
                with patch(
                    "nilo.cli_handlers.release.run_local_verification",
                    side_effect=lambda command, cwd, timeout, **kwargs: release_verification_result(cwd, command=command, snapshot_mode=kwargs.get("snapshot_mode", "full")),
                ) as full_check:
                    with redirect_stdout(output):
                        main(["--db", str(db), "release", "publish", "--project", root.name, "--approval", "v0.3.1 を tag/push/release して"])
                text = output.getvalue()
                full_check.assert_called_once_with(release_handler.RELEASE_FULL_CHECK_COMMAND, root, 600.0, snapshot_mode="full")
                self.assertIn("full_check: PYTHONPATH=src python tests/run_shards.py --all --jobs auto", text)
                self.assertIn("full_check_exit_code: 0", text)
                self.assertIn("release_recipe: completed", text)
                store = Store(db)
                try:
                    run = store.get("recipe_runs", run_id)
                    run_metadata = run["metadata"]
                    self.assertTrue(run_metadata["release_publish_full_check_required"])
                    self.assertTrue(run_metadata["release_publish_full_check_passed"])
                    self.assertFalse(run_metadata["release_publish_full_check_reused"])
                    self.assertEqual(run_metadata["release_publish_full_check_command"], release_handler.RELEASE_FULL_CHECK_COMMAND)
                finally:
                    store.close()
            finally:
                os.environ["PATH"] = previous_path
                os.chdir(previous_cwd)

    def test_release_publish_blocks_recipe_when_full_check_fails_before_public_operations(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            db = root / ".git" / "nilo.db"
            previous_cwd = Path.cwd()
            previous_path = os.environ.get("PATH", "")
            try:
                os.chdir(root)
                self.create_project(db, root.name)
                root.joinpath("docs/releases").mkdir(parents=True)
                root.joinpath("docs/releases/0.3.1.md").write_text("release notes\n", encoding="utf-8")
                run_git(root, "add", "docs/releases/0.3.1.md")
                run_git(root, "commit", "-m", "Release 0.3.1")
                recipe_output = io.StringIO()
                with redirect_stdout(recipe_output):
                    main(["--db", str(db), "recipe", "run", "release", "--project", root.name, "--var", "target_version=0.3.1"])
                release_task_id = recipe_output.getvalue().strip().splitlines()[-1]
                store = Store(db)
                try:
                    force_waiting_public_approval(store, root, root.name, release_task_id)
                finally:
                    store.close()

                fake_bin = install_fake_release_tools(root)
                os.environ["PATH"] = f"{fake_bin}{os.pathsep}{previous_path}"
                output = io.StringIO()
                with patch(
                    "nilo.cli_handlers.release.run_local_verification",
                    side_effect=lambda command, cwd, timeout, **kwargs: failed_release_verification_result(
                        cwd, command=command, snapshot_mode=kwargs.get("snapshot_mode", "full")
                    ),
                ):
                    with self.assertRaises(SystemExit) as raised:
                        with redirect_stdout(output):
                            main(["--db", str(db), "release", "publish", "--project", root.name, "--approval", "v0.3.1 を tag/push/release して"])
                self.assertIn("release publish full check failed", str(raised.exception))
                self.assertIn("release recipe paused_for_fix", str(raised.exception))
                self.assertIn("full_check_exit_code: 1", output.getvalue())
                self.assertIn("blocked_reason: failed_verification", output.getvalue())
                self.assertFalse(root.joinpath(".git/release-tools.log").exists())
                store = Store(db)
                try:
                    context = workflow_context(store, root.name)
                    self.assertEqual(context["status"], "paused_for_fix")
                    self.assertEqual(context["blocked_reason"], "failed_verification")
                    self.assertTrue(context["failed_verification_id"])
                    run = store.get("recipe_runs", context["recipe_run_id"])
                    metadata = run["metadata"]
                    self.assertTrue(metadata["release_publish_full_check_required"])
                    self.assertFalse(metadata["release_publish_full_check_passed"])
                    self.assertEqual(metadata["blocked_reason"], "failed_verification")
                    self.assertEqual(metadata["failed_verification"]["command"], release_handler.RELEASE_FULL_CHECK_COMMAND)
                    self.assertFalse([item for item in audit_workflow(store, root.name, cwd=root) if item["severity"] == "error"])
                finally:
                    store.close()

                next_output = io.StringIO()
                with redirect_stdout(next_output):
                    main(["--db", str(db), "next", "--project", root.name])
                next_body = next_output.getvalue()
                self.assertIn("verification 失敗で停止", next_body)
                self.assertIn("別 task", next_body)
                self.assertIn("next_action: create_separate_bugfix_task", next_body)

                next_ai = io.StringIO()
                with redirect_stdout(next_ai):
                    main(["--db", str(db), "next", "--ai", "--project", root.name])
                data = json.loads(next_ai.getvalue())
                self.assertEqual(data["next_action"], "create_separate_bugfix_task")
                self.assertEqual(data["blocked_recipe"], "release")
                self.assertEqual(data["blocked_reason"], "failed_verification")
                self.assertTrue(data["must_not_fix_inside_recipe"])
            finally:
                os.environ["PATH"] = previous_path
                os.chdir(previous_cwd)

    def test_approve_public_execute_dirty_tree_does_not_consume_public_gate(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            db = root / ".git" / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                self.create_project(db, root.name)
                recipe_output = io.StringIO()
                with redirect_stdout(recipe_output):
                    main(["--db", str(db), "recipe", "run", "release", "--project", root.name, "--var", "target_version=0.3.1"])
                release_task_id = recipe_output.getvalue().strip().splitlines()[-1]
                store = Store(db)
                try:
                    store.insert("verification_runs", full_release_verification_row(release_task_id, root))
                    mark_release_commit_recorded(
                        store,
                        task_id=release_task_id,
                        commit_sha=run_git(root, "rev-parse", "HEAD"),
                        commit_message="Release 0.3.1",
                        post_commit_snapshot=compact_snapshot(current_git_snapshot(root)),
                    )
                finally:
                    store.close()

                root.joinpath("tracked.txt").write_text("dirty after gate\n", encoding="utf-8")
                with self.assertRaises(SystemExit) as raised:
                    with redirect_stdout(io.StringIO()):
                        main(
                            [
                                "--db",
                                str(db),
                                "recipe",
                                "approve-public",
                                "--project",
                                root.name,
                                "--approval",
                                "v0.3.1 を tag/push/release して",
                                "--execute",
                            ]
                        )
                self.assertIn("working tree must be clean", str(raised.exception))

                store = Store(db)
                try:
                    context = workflow_context(store, root.name)
                    self.assertEqual(context["status"], "waiting_public_approval")
                    self.assertEqual([item["operation"] for item in context["pending_public_operations"]], ["create_tag", "push_branch", "push_tag", "create_github_release"])
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

    def test_release_publish_recovers_missing_pending_operations_after_manual_commit(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            db = root / ".git" / "nilo.db"
            previous_cwd = Path.cwd()
            previous_path = os.environ.get("PATH", "")
            try:
                os.chdir(root)
                self.create_project(db, root.name)
                recipe_output = io.StringIO()
                with redirect_stdout(recipe_output):
                    main(["--db", str(db), "recipe", "run", "release", "--project", root.name, "--var", "target_version=0.3.1"])
                release_task_id = recipe_output.getvalue().strip().splitlines()[-1]
                root.joinpath("docs/releases").mkdir(parents=True)
                root.joinpath("docs/releases/0.3.1.md").write_text("release notes\n", encoding="utf-8")
                run_git(root, "add", "docs/releases/0.3.1.md")
                run_git(root, "commit", "-m", "Release 0.3.1")
                store = Store(db)
                try:
                    store.insert("verification_runs", full_release_verification_row(release_task_id, root))
                    context = workflow_context(store, root.name)
                    self.assertEqual(context["pending_public_operations"], [])
                finally:
                    store.close()

                fake_bin = install_fake_release_tools(root)
                os.environ["PATH"] = f"{fake_bin}{os.pathsep}{previous_path}"
                output = io.StringIO()
                with redirect_stdout(output):
                    main(
                        [
                            "--db",
                            str(db),
                            "release",
                            "publish",
                            "--project",
                            root.name,
                            "--approval",
                            "v0.3.1 を tag/push/release して",
                        ]
                    )
                text = output.getvalue()
                self.assertIn("release_recipe: completed", text)
                self.assertIn("github_release: https://github.com/example/project/releases/tag/v0.3.1", text)
                store = Store(db)
                try:
                    completed_context = workflow_context(store, root.name)
                    self.assertEqual(completed_context["type"], "project")
                    runs = store.list_where("recipe_runs", "project_id=? AND recipe_name='release'", (root.name,))
                    self.assertTrue((runs[0]["metadata"] or {}).get("public_operations_recovered"))
                finally:
                    store.close()
            finally:
                os.environ["PATH"] = previous_path
                os.chdir(previous_cwd)

    def test_release_prepare_failure_pauses_for_fix_and_next_points_to_resume(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            self.write_release_project_files(root, "0.3.0")
            db = root / ".git" / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                self.create_project(db, root.name)
                output = io.StringIO()
                with patch(
                    "nilo.cli_handlers.release.run_local_verification",
                    side_effect=lambda command, cwd, timeout, **kwargs: failed_release_verification_result(
                        cwd, command=command, snapshot_mode=kwargs.get("snapshot_mode", "fast")
                    ),
                ):
                    with self.assertRaises(SystemExit) as raised:
                        with redirect_stdout(output):
                            main(["--db", str(db), "release", "prepare", "--project", root.name, "--target-version", "0.3.1"])
                self.assertIn("release recipe paused_for_fix", str(raised.exception))
                self.assertIn("recipe_run: paused_for_fix", output.getvalue())
                store = Store(db)
                try:
                    context = workflow_context(store, root.name)
                    self.assertEqual(context["status"], "paused_for_fix")
                    self.assertEqual(context["reason"], "changed_check_failed")
                    self.assertTrue(context["failed_verification_id"])
                    rendered = render_ai_context_text(project_ai_context(store, root.name, cwd=root))
                    self.assertIn("active_recipe: release", rendered)
                    self.assertIn("recipe_status: paused_for_fix", rendered)
                    self.assertIn("next_action:", rendered)
                    self.assertIn("resume_command: nilo release resume --project", rendered)
                    self.assertIn("latest_verification: status=failed", rendered)
                    self.assertFalse([item for item in audit_workflow(store, root.name, cwd=root) if item["severity"] == "error"])
                finally:
                    store.close()

                next_output = io.StringIO()
                with redirect_stdout(next_output):
                    main(["--db", str(db), "next", "--project", root.name])
                body = next_output.getvalue()
                self.assertIn("verification 失敗で停止", body)
                self.assertIn("next_action: create_separate_bugfix_task", body)
                self.assertIn("resume_command: nilo release resume --project", body)
            finally:
                os.chdir(previous_cwd)

    def test_release_resume_reuses_verified_dirty_tree_after_fix_commit(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            self.write_release_project_files(root, "0.3.0")
            db = root / ".git" / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                self.create_project(db, root.name)
                with patch(
                    "nilo.cli_handlers.release.run_local_verification",
                    side_effect=lambda command, cwd, timeout, **kwargs: failed_release_verification_result(
                        cwd, command=command, snapshot_mode=kwargs.get("snapshot_mode", "fast")
                    ),
                ):
                    with self.assertRaises(SystemExit):
                        with redirect_stdout(io.StringIO()):
                            main(["--db", str(db), "release", "prepare", "--project", root.name, "--target-version", "0.3.1"])

                root.joinpath("tracked.txt").write_text("fix\n", encoding="utf-8")
                store = Store(db)
                try:
                    run = workflow_context(store, root.name)
                    task_id = run["task_id"]
                    verified = release_verification_result(root)
                    verified["metadata"]["verification_mode"] = "full"
                    verified["metadata"]["release_target_version"] = "0.3.1"
                    verified["metadata"]["release_effective_dirty_hash"] = release_handler._release_effective_worktree_hash(root)
                    store.insert("verification_runs", {"id": "verification_reusable", "task_id": task_id, "evidence_check_id": None, **verified})
                finally:
                    store.close()

                run_git(root, "add", "tracked.txt", "pyproject.toml", "src/nilo/__init__.py", "docs/releases/0.3.1.md")
                run_git(root, "commit", "-m", "Fix release checks")
                output = io.StringIO()
                with patch("nilo.cli_handlers.release.run_local_verification") as full_check, patch("nilo.cli_handlers.release._run_lightweight_post_commit_checks"):
                    with redirect_stdout(output):
                        main(["--db", str(db), "release", "resume", "--project", root.name])
                full_check.assert_not_called()
                text = output.getvalue()
                self.assertIn("verification_reused: verified_dirty_tree_matches_current_commit", text)
                self.assertIn("recipe_run: waiting_public_approval", text)
                store = Store(db)
                try:
                    self.assertEqual(workflow_context(store, root.name)["status"], "waiting_public_approval")
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

    def test_release_resume_does_not_reuse_when_same_path_changes_after_verification(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            self.write_release_project_files(root, "0.3.0")
            db = root / ".git" / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                self.create_project(db, root.name)
                with patch(
                    "nilo.cli_handlers.release.run_local_verification",
                    side_effect=lambda command, cwd, timeout, **kwargs: failed_release_verification_result(
                        cwd, command=command, snapshot_mode=kwargs.get("snapshot_mode", "fast")
                    ),
                ):
                    with self.assertRaises(SystemExit):
                        with redirect_stdout(io.StringIO()):
                            main(["--db", str(db), "release", "prepare", "--project", root.name, "--target-version", "0.3.1"])

                root.joinpath("tracked.txt").write_text("verified fix\n", encoding="utf-8")
                store = Store(db)
                try:
                    task_id = workflow_context(store, root.name)["task_id"]
                    verified = release_verification_result(root)
                    verified["metadata"]["verification_mode"] = "full"
                    verified["metadata"]["release_target_version"] = "0.3.1"
                    verified["metadata"]["release_effective_dirty_hash"] = release_handler._release_effective_worktree_hash(root)
                    store.insert("verification_runs", {"id": "verification_reusable", "task_id": task_id, "evidence_check_id": None, **verified})
                finally:
                    store.close()

                root.joinpath("tracked.txt").write_text("edited after verification\n", encoding="utf-8")
                run_git(root, "add", "tracked.txt", "pyproject.toml", "src/nilo/__init__.py", "docs/releases/0.3.1.md")
                run_git(root, "commit", "-m", "Fix release checks")
                output = io.StringIO()
                with patch(
                    "nilo.cli_handlers.release.run_local_verification",
                    side_effect=lambda command, cwd, timeout, **kwargs: release_verification_result(cwd, command=command, snapshot_mode=kwargs.get("snapshot_mode", "fast")),
                ) as full_check, patch(
                    "nilo.cli_handlers.release._run_lightweight_post_commit_checks"
                ):
                    with redirect_stdout(output):
                        main(["--db", str(db), "release", "resume", "--project", root.name])
                full_check.assert_called_once()
                body = output.getvalue()
                self.assertNotIn("verification_reused: verified_dirty_tree_matches_current_commit", body)
                self.assertIn("recipe_run: active", body)
                self.assertIn("required_checks: full_check_deferred", body)
            finally:
                os.chdir(previous_cwd)

    def test_release_resume_blocks_unmanaged_dirty_separately_from_release_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            self.write_release_project_files(root, "0.3.0")
            db = root / ".git" / "nilo.db"
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                self.create_project(db, root.name)
                with patch(
                    "nilo.cli_handlers.release.run_local_verification",
                    side_effect=lambda command, cwd, timeout, **kwargs: failed_release_verification_result(
                        cwd, command=command, snapshot_mode=kwargs.get("snapshot_mode", "fast")
                    ),
                ):
                    with self.assertRaises(SystemExit):
                        with redirect_stdout(io.StringIO()):
                            main(["--db", str(db), "release", "prepare", "--project", root.name, "--target-version", "0.3.1"])
                root.joinpath("src/nilo/snapshot.py").write_text("bugfix\n", encoding="utf-8")
                with self.assertRaises(SystemExit) as raised:
                    with redirect_stdout(io.StringIO()):
                        main(["--db", str(db), "release", "resume", "--project", root.name])
                body = str(raised.exception)
                self.assertIn("release-managed dirty files:", body)
                self.assertIn("pyproject.toml", body)
                self.assertIn("unmanaged dirty files:", body)
                self.assertIn("src/nilo/snapshot.py", body)
                self.assertIn("commit or revert unmanaged files", body)
            finally:
                os.chdir(previous_cwd)

    def test_release_run_auto_patch_starts_prepare_and_none_db_path_is_not_created(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            self.write_release_project_files(root, "0.3.0")
            run_git(root, "tag", "v0.3.0")
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with redirect_stdout(io.StringIO()):
                    main(["project", "create", "Nilo", "--id", root.name])
                output = io.StringIO()
                with patch(
                    "nilo.cli_handlers.release.run_local_verification",
                    side_effect=lambda command, cwd, timeout, **kwargs: release_verification_result(cwd, command=command, snapshot_mode=kwargs.get("snapshot_mode", "fast")),
                ), patch(
                    "nilo.cli_handlers.release._run_lightweight_post_commit_checks"
                ) as lightweight_checks:
                    with redirect_stdout(output):
                        main(["release", "run", "--project", root.name, "--auto-patch"])
                self.assertIn("target_version: 0.3.1", output.getvalue())
                self.assertIn("recipe_run: active", output.getvalue())
                self.assertIn("required_checks: full_check_deferred", output.getvalue())
                self.assertFalse(root.joinpath("None").exists())
                self.assertEqual(lightweight_checks.call_args.args[2], str(root / ".nilo" / "nilo.db"))
            finally:
                os.chdir(previous_cwd)


if __name__ == "__main__":
    unittest.main()
