from __future__ import annotations

import io
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
from nilo.snapshot import compact_snapshot, current_git_snapshot, snapshot_columns
from nilo.state_audit import audit_task
from nilo.store import Store
from nilo.task_logic import projected_task_status
from nilo.timeutil import now_iso
from nilo.workflow_context import approve_pending_public_operations, mark_release_commit_recorded, workflow_context


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


def release_verification_result(root: Path) -> dict:
    snapshot = current_git_snapshot(root)
    now = now_iso()
    return {
        "source": "nilo_executed",
        "command": "PYTHONPATH=src python tests/run_shards.py --all --jobs auto",
        "cwd": str(root),
        "stdout": "ok",
        "stderr": "",
        "exit_code": 0,
        "timed_out": False,
        "timeout_seconds": 600,
        **snapshot_columns(snapshot),
        "metadata": {},
        "started_at": now,
        "finished_at": now,
        "created_at": now,
    }


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

    def test_release_prepare_updates_verifies_commits_and_opens_public_gate(self) -> None:
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
                with patch("nilo.cli_handlers.release.run_local_verification", side_effect=lambda command, cwd, timeout: release_verification_result(cwd)), patch(
                    "nilo.cli_handlers.release._run_lightweight_post_commit_checks"
                ) as lightweight_checks:
                    with redirect_stdout(output):
                        main(["--db", str(db), "release", "prepare", "--project", root.name, "--target-version", "0.3.1"])
                text = output.getvalue()
                self.assertIn("full_check: PYTHONPATH=src python tests/run_shards.py --all --jobs auto", text)
                self.assertNotIn("python -m unittest discover tests", text)
                self.assertIn("recipe_run: waiting_public_approval", text)
                self.assertIn("pending_public_operations: created", text)
                self.assertIn("publish: nilo release publish --project", text)
                lightweight_checks.assert_called_once_with(root.name, root, str(db))
                self.assertEqual(root.joinpath("pyproject.toml").read_text(encoding="utf-8").count('version = "0.3.1"'), 1)
                self.assertIn('__version__ = "0.3.1"', root.joinpath("src/nilo/__init__.py").read_text(encoding="utf-8"))
                self.assertTrue(root.joinpath("docs/releases/0.3.1.md").exists())
                self.assertEqual(run_git(root, "status", "--porcelain=v1", "--untracked-files=all"), "")
                store = Store(db)
                try:
                    context = workflow_context(store, root.name)
                    self.assertEqual(context["status"], "waiting_public_approval")
                    self.assertEqual([item["operation"] for item in context["pending_public_operations"]], ["create_tag", "push_branch", "push_tag", "create_github_release"])
                    run = store.get("recipe_runs", context["recipe_run_id"])
                    metadata = run["metadata"]
                    self.assertTrue(metadata["commit_sha"])
                    self.assertEqual(metadata["committed_files"], ["docs/releases/0.3.1.md", "pyproject.toml", "src/nilo/__init__.py"])
                    self.assertTrue(metadata["post_commit_full_check_reused"])
                    verification = store.latest_for_task("verification_runs", context["task_id"])
                    self.assertEqual(verification["command"], "PYTHONPATH=src python tests/run_shards.py --all --jobs auto")
                finally:
                    store.close()
            finally:
                os.chdir(previous_cwd)

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
                    codes = {item["code"] for item in audit_task(store, "task_release", cwd=root)}
                    self.assertIn("completion_commit_verified_diff_mismatch", codes)
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
                    store.insert("verification_runs", verification_row(release_task_id, root))
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
                    store.insert("verification_runs", verification_row(release_task_id, root))
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
                    store.insert("verification_runs", verification_row(release_task_id, root))
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
                    store.insert("verification_runs", verification_row(release_task_id, root))
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


if __name__ == "__main__":
    unittest.main()
