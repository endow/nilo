from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..cli_support import make_id
from ..recipe import discover_recipes
from ..snapshot import compact_snapshot, current_git_snapshot
from ..store import Store
from ..timeutil import now_iso
from ..transitions import TransitionError, record_verification_run
from ..verification import run_local_verification
from ..workflow_context import (
    active_recipe_run,
    execute_pending_public_operations,
    mark_release_commit_recorded,
    public_approval_text,
    release_version_from_run,
)
from .recipe import _create_recipe_task, _find_recipe, _render_task_fields


RELEASE_FULL_CHECK_COMMAND = "PYTHONPATH=src python tests/run_shards.py --all --jobs auto"


def cmd_release_prepare(args: argparse.Namespace) -> None:
    project_id = args.project or Path.cwd().name
    cwd = Path.cwd()
    store = Store(args.db)
    try:
        if not store.get("projects", project_id):
            raise SystemExit(f"project not found: {project_id}")
        run = _ensure_release_run(store, args, project_id, cwd)
        target_version = (args.target_version or release_version_from_run(run)).lstrip("v")
        if not target_version:
            raise SystemExit("target version could not be resolved")

        _require_clean_or_release_managed(cwd, target_version)
        changed = _update_release_managed_files(cwd, target_version)
        print("release_prepare:")
        print(f"- target_version: {target_version}")
        for path in changed:
            print(f"- updated: {path}")

        verification = run_local_verification(RELEASE_FULL_CHECK_COMMAND, cwd, args.timeout, snapshot_mode="full")
        verification.setdefault("metadata", {})["verification_mode"] = "full"
        verification["metadata"]["release_prepare"] = True
        verification_row = {"id": make_id("verification"), "task_id": run["task_id"], "evidence_check_id": None, **verification}
        try:
            record_verification_run(store, run["task_id"], row=verification_row, actor="nilo")
        except TransitionError as exc:
            raise SystemExit(f"{exc.message}{(': ' + exc.remediation) if exc.remediation else ''}") from exc
        print(f"- verification_run: {verification_row['id']}")
        print(f"- full_check: {RELEASE_FULL_CHECK_COMMAND}")
        print(f"- exit_code: {verification_row['exit_code']}")
        if verification_row.get("timed_out") or verification_row.get("exit_code") != 0:
            raise SystemExit("release full check failed")

        pre_commit_snapshot = current_git_snapshot(cwd)
        if compact_snapshot(verification_row) != compact_snapshot(pre_commit_snapshot):
            raise SystemExit("verification snapshot changed before commit; rerun release prepare")
        managed_files = release_managed_files(target_version)
        dirty_files = _git_changed_files(cwd)
        unexpected = sorted(path for path in dirty_files if path not in managed_files)
        if unexpected:
            raise SystemExit("release prepare found unmanaged dirty files: " + ", ".join(unexpected))
        if not dirty_files:
            raise SystemExit("no release managed file changes to commit")

        _git_checked(cwd, ["add", *managed_files])
        committed_files = _git_output(cwd, ["diff", "--cached", "--name-only"])[1].splitlines()
        unexpected_staged = sorted(path for path in committed_files if path not in managed_files)
        if unexpected_staged:
            raise SystemExit("release prepare staged unmanaged files: " + ", ".join(unexpected_staged))
        commit_message = f"Release {target_version}"
        _git_checked(cwd, ["commit", "-m", commit_message])
        commit_sha = _git_value(cwd, ["rev-parse", "HEAD"])
        committed_tree_hash = _git_value(cwd, ["rev-parse", "HEAD^{tree}"])
        post_commit_snapshot = current_git_snapshot(cwd)
        if post_commit_snapshot.get("working_tree_dirty"):
            raise SystemExit("working tree is dirty after release commit")

        updated_run = mark_release_commit_recorded(
            store,
            task_id=run["task_id"],
            commit_sha=commit_sha,
            commit_message=commit_message,
            post_commit_snapshot=compact_snapshot(post_commit_snapshot),
        )
        if not updated_run or updated_run.get("status") != "waiting_public_approval":
            raise SystemExit("release commit was recorded but public approval gate did not open")
        metadata = updated_run.get("metadata") or {}
        metadata.update(
            {
                "target_version": target_version,
                "verification_snapshot": compact_snapshot(verification_row),
                "pre_commit_snapshot": compact_snapshot(pre_commit_snapshot),
                "post_commit_snapshot": compact_snapshot(post_commit_snapshot),
                "committed_tree_hash": committed_tree_hash,
                "committed_files": committed_files,
                "release_prepare_managed_files": managed_files,
                "post_commit_full_check_reused": True,
            }
        )
        store.update("recipe_runs", updated_run["id"], {"metadata": metadata, "updated_at": now_iso()})

        _run_lightweight_post_commit_checks(project_id, cwd, str(args.db))
        final_run = store.get("recipe_runs", updated_run["id"])
        print(f"- commit: {commit_sha}")
        print("- recipe_run: waiting_public_approval")
        print("- pending_public_operations: created")
        print(f"approval: {public_approval_text(final_run)}")
        print(f"publish: {_release_publish_command(project_id, final_run)}")
    finally:
        store.close()


def cmd_release_publish(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        if not store.get("projects", args.project):
            raise SystemExit(f"project not found: {args.project}")
        try:
            run, logs = execute_pending_public_operations(
                store,
                project_id=args.project,
                approval=args.approval,
                release_url=args.release_url,
                cwd=Path.cwd(),
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print("executed_public_operations:")
        for log in logs:
            print(f"- {' '.join(log['command'])}: exit_code={log['exit_code']}")
        print(f"release_recipe: {run['status']}")
        print(f"recipe_run: {run['id']}")
        metadata = run.get("metadata") or {}
        if metadata.get("commit_sha"):
            print(f"commit: {metadata['commit_sha']}")
        if metadata.get("github_release_url"):
            print(f"github_release: {metadata['github_release_url']}")
    finally:
        store.close()


def _ensure_release_run(store: Store, args: argparse.Namespace, project_id: str, cwd: Path) -> dict[str, Any]:
    run = active_recipe_run(store, project_id)
    if run:
        if run.get("recipe_name") != "release":
            raise SystemExit("another recipe is already active")
        run_version = release_version_from_run(run).lstrip("v")
        requested_version = (args.target_version or "").lstrip("v")
        if requested_version and run_version and requested_version != run_version:
            raise SystemExit(f"active release recipe target_version is {run_version}; got {requested_version}")
        return run
    data = discover_recipes(cwd)
    source = _find_recipe(data["effective_recipes"], "release")
    if not source:
        raise SystemExit("recipe not found: release")
    raw_vars = [f"target_version={args.target_version}"] if args.target_version else []
    rendered, variable_messages = _render_task_fields(source, project_id, raw_vars, "", cwd)
    for message in variable_messages:
        print(message)
    recipe_args = argparse.Namespace(
        db=args.db,
        task_type="implementation",
        risk="medium",
        commitment="",
    )
    task_id = _create_recipe_task(recipe_args, project_id, source, rendered)
    from ..workflow_context import create_recipe_run

    return create_recipe_run(store, project_id=project_id, task_id=task_id, recipe_name="release", rendered_fields=rendered)


def release_managed_files(version: str) -> list[str]:
    return [
        "pyproject.toml",
        "src/nilo/__init__.py",
        f"docs/releases/{version}.md",
    ]


def _update_release_managed_files(cwd: Path, version: str) -> list[str]:
    changed: list[str] = []
    pyproject = cwd / "pyproject.toml"
    init_file = cwd / "src" / "nilo" / "__init__.py"
    release_note = cwd / "docs" / "releases" / f"{version}.md"
    if pyproject.exists() and _replace_version_line(pyproject, r'^(version\s*=\s*")[^"]+(")', version):
        changed.append("pyproject.toml")
    if init_file.exists() and _replace_version_line(init_file, r'^(__version__\s*=\s*")[^"]+(")', version):
        changed.append("src/nilo/__init__.py")
    if not release_note.exists():
        release_note.parent.mkdir(parents=True, exist_ok=True)
        release_note.write_text(_release_note_template(version), encoding="utf-8")
        changed.append(release_note.relative_to(cwd).as_posix())
    return changed


def _replace_version_line(path: Path, pattern: str, version: str) -> bool:
    text = path.read_text(encoding="utf-8")
    updated = re.sub(pattern, rf"\g<1>{version}\2", text, count=1, flags=re.MULTILINE)
    if updated == text:
        return False
    path.write_text(updated, encoding="utf-8")
    return True


def _release_note_template(version: str) -> str:
    return f"""# v{version}

## リリースノート（日本語）

- Nilo {version} のリリース準備。
- release prepare による version 更新、full verification、release commit 記録を確認。
- 公開は人間承認後に tag / push / GitHub release を実行。

## Release Notes (English)

- Prepared the Nilo {version} release.
- Verified version updates, full verification, and release commit recording through release prepare.
- Publishing remains gated by explicit human approval for tag, push, and GitHub release creation.
"""


def _require_clean_or_release_managed(cwd: Path, version: str) -> None:
    dirty = _git_changed_files(cwd)
    allowed = set(release_managed_files(version))
    unexpected = sorted(path for path in dirty if path not in allowed)
    if unexpected:
        raise SystemExit("release prepare requires a clean tree except release managed files: " + ", ".join(unexpected))


def _run_lightweight_post_commit_checks(project_id: str, cwd: Path, db_path: str) -> None:
    for command in (["recipe", "doctor", "--project", project_id], ["status", "--project", project_id]):
        completed = subprocess.run([sys.executable, "-m", "nilo", "--db", db_path, *command], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        print(f"- nilo {' '.join(command)}: exit_code={completed.returncode}")
        if completed.returncode != 0:
            raise SystemExit(completed.stderr or completed.stdout or f"command failed: nilo {' '.join(command)}")


def _release_publish_command(project_id: str, run: dict[str, Any]) -> str:
    approval = public_approval_text(run).strip('"')
    return " ".join(["nilo", "release", "publish", "--project", shlex.quote(project_id), "--approval", shlex.quote(approval)])


def _git_changed_files(cwd: Path) -> set[str]:
    code, out, err = _git_output(cwd, ["status", "--porcelain=v1", "--untracked-files=all"])
    if code != 0:
        raise SystemExit(err or "git status failed")
    files: set[str] = set()
    for line in out.splitlines():
        path = line[3:].strip() if len(line) > 2 and line[2] == " " else line[2:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path:
            files.add(path.replace("\\", "/"))
    return files


def _git_value(cwd: Path, args: list[str]) -> str:
    code, out, err = _git_output(cwd, args)
    if code != 0:
        raise SystemExit(err or f"git {' '.join(args)} failed")
    return out.strip()


def _git_checked(cwd: Path, args: list[str]) -> str:
    code, out, err = _git_output(cwd, args)
    if code != 0:
        raise SystemExit(err or f"git {' '.join(args)} failed")
    return out


def _git_output(cwd: Path, args: list[str]) -> tuple[int, str, str]:
    completed = subprocess.run(["git", *args], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    return completed.returncode, completed.stdout.rstrip("\n"), completed.stderr.rstrip("\n")
