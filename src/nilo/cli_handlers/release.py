from __future__ import annotations

import argparse
import hashlib
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..cli_support import make_id
from ..project_boundary import resolve_project_boundary
from ..recipe import discover_recipes
from ..snapshot import compact_snapshot, current_git_snapshot
from ..store import Store
from ..timeutil import now_iso
from ..transitions import TransitionError, record_verification_run
from ..version_advisor import advise_version_bump
from ..verification import run_local_verification
from ..workflow_context import (
    active_recipe_run,
    execute_pending_public_operations,
    mark_release_commit_recorded,
    public_approval_text,
    release_version_from_run,
    validate_public_operation_approval,
)
from .recipe import _create_recipe_task, _find_recipe, _render_task_fields


RELEASE_FULL_CHECK_COMMAND = "PYTHONPATH=src python tests/run_shards.py --all --jobs auto"
RELEASE_CHANGED_CHECK_COMMAND = "PYTHONPATH=src python tests/run_shards.py --changed --jobs auto"


def cmd_release_prepare(args: argparse.Namespace) -> None:
    _release_prepare_or_resume(args, resume=False)


def cmd_release_resume(args: argparse.Namespace) -> None:
    _release_prepare_or_resume(args, resume=True)


def cmd_release_run(args: argparse.Namespace) -> None:
    project_id = args.project or Path.cwd().name
    cwd = Path.cwd()
    target_version = (args.target_version or "").lstrip("v")
    if args.auto_patch:
        resolution = advise_version_bump(cwd)
        current = resolution.get("current_version") or ""
        latest = resolution.get("latest_tag") or ""
        latest_version = resolution.get("latest_tag_version") or latest.lstrip("v")
        patch_candidate = str(resolution.get("patch_candidate") or "").lstrip("v")
        can_auto_patch = bool(current and patch_candidate and latest_version == current)
        if not can_auto_patch:
            reason = resolution.get("reason") or "auto patch target could not be resolved"
            raise SystemExit(
                "\n".join(
                    [
                        "target_version を自動採用できませんでした。",
                        f"現在バージョン: {current or '不明'}",
                        f"最新タグ: {latest or 'なし'}",
                        f"理由: {reason}",
                        "明示する場合: nilo release run --project "
                        f"{project_id} --target-version {patch_candidate or '<version>'}",
                    ]
                )
            )
        target_version = patch_candidate
        print(f"target_version: {target_version}")
        print("target_source: auto_patch")
    _release_prepare_or_resume(argparse.Namespace(**{**vars(args), "target_version": target_version}), resume=False)


def _release_prepare_or_resume(args: argparse.Namespace, *, resume: bool) -> None:
    project_id = args.project or Path.cwd().name
    cwd = Path.cwd()
    db_path = _resolved_db_path(args.db, cwd)
    store = Store(db_path)
    try:
        if not store.get("projects", project_id):
            raise SystemExit(f"project not found: {project_id}")
        run = _ensure_release_run(store, args, project_id, cwd, resume=resume)
        target_version = (getattr(args, "target_version", "") or release_version_from_run(run)).lstrip("v")
        if not target_version:
            raise SystemExit("target version could not be resolved")
        if not resume and _release_prepare_already_satisfied(run):
            print("release prepare: already satisfied")
            print("- full_check: already satisfied")
            print("- next_action: publish approval required")
            print(f"publish: {_release_publish_command(project_id, run)}")
            return

        managed_files = _managed_files_for_run(run, target_version)
        dirty = _classify_dirty_files(cwd, managed_files)
        if dirty["unmanaged_dirty"]:
            _pause_release_for_fix(
                store,
                run,
                reason="unmanaged_dirty",
                managed_release_dirty=dirty["managed_release_dirty"],
                unmanaged_dirty=dirty["unmanaged_dirty"],
            )
            _raise_unmanaged_dirty(dirty, project_id)
        changed = _update_release_managed_files(cwd, target_version)
        managed_files = release_managed_files(target_version)
        run = _store_release_metadata(store, run, {"target_version": target_version, "managed_release_files": managed_files})
        print("release_prepare:")
        print(f"- target_version: {target_version}")
        for path in changed:
            print(f"- updated: {path}")

        verification_row = reusable_full_verification_for_release(store, run["task_id"], cwd, target_version=target_version)
        verification_mode = "full"
        if verification_row:
            print(f"- verification_run: {verification_row['id']}")
            print(f"- verification_reused: {verification_row.get('reuse_reason', 'current_full_check')}")
            print("- full_check: reused")
        else:
            verification_row = _run_release_changed_check(store, run, cwd, args.timeout)
            verification_mode = "changed"
            print(f"- verification_run: {verification_row['id']}")
            print(f"- changed_check: {RELEASE_CHANGED_CHECK_COMMAND}")
            print(f"- changed_check_exit_code: {verification_row['exit_code']}")
            print("- full_check: deferred")
        print(f"- exit_code: {verification_row['exit_code']}")
        if verification_row.get("timed_out") or verification_row.get("exit_code") != 0:
            failure_reason = "full_check_failed" if verification_mode == "full" else "changed_check_failed"
            _pause_release_for_fix(
                store,
                run,
                reason=failure_reason,
                verification_row=verification_row,
                failed_verification_id=verification_row["id"],
                managed_release_dirty=sorted(_git_changed_files(cwd).intersection(managed_files)),
                unmanaged_dirty=sorted(_git_changed_files(cwd).difference(managed_files)),
            )
            print("- recipe_run: paused_for_fix")
            print(f"- reason: {failure_reason}")
            print(f"- failed_verification_id: {verification_row['id']}")
            print(f"resume: nilo release resume --project {shlex.quote(project_id)}")
            raise SystemExit(f"release {verification_mode} check failed; release recipe paused_for_fix")

        pre_commit_snapshot = current_git_snapshot(cwd)
        reuse_after_commit = verification_row.get("snapshot_relation") in {"verified_dirty_tree_committed", "release_metadata_only_changes"}
        if not reuse_after_commit and not _release_snapshot_matches_verification(verification_row, pre_commit_snapshot, cwd):
            raise SystemExit("verification snapshot changed before commit; rerun release prepare")
        dirty_files = _git_changed_files(cwd)
        unexpected = sorted(path for path in dirty_files if path not in managed_files)
        if unexpected:
            _pause_release_for_fix(
                store,
                run,
                reason="unmanaged_dirty",
                managed_release_dirty=sorted(set(dirty_files).intersection(managed_files)),
                unmanaged_dirty=unexpected,
            )
            _raise_unmanaged_dirty(_classify_dirty_files(cwd, managed_files), project_id)
        if not dirty_files:
            recovered = mark_release_commit_recorded(
                store,
                task_id=run["task_id"],
                commit_sha=_git_value(cwd, ["rev-parse", "HEAD"]),
                commit_message=_git_value(cwd, ["log", "-1", "--pretty=%s"]) or f"Release {target_version}",
                post_commit_snapshot=compact_snapshot(pre_commit_snapshot),
            )
            if recovered and recovered.get("status") == "waiting_public_approval":
                print("- recipe_run: waiting_public_approval")
                print(f"approval: {public_approval_text(recovered)}")
                print(f"publish: {_release_publish_command(project_id, recovered)}")
                return
            if recovered and recovered.get("status") == "active" and not (recovered.get("metadata") or {}).get("required_checks_passed"):
                print(f"- commit: {_git_value(cwd, ['rev-parse', 'HEAD'])}")
                print("- recipe_run: active")
                print("- required_checks: full_check_deferred")
                print("- pending_public_operations: none")
                return
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
        if _git_changed_files(cwd):
            raise SystemExit("working tree is dirty after release commit")
        post_commit_snapshot = _release_sanitize_snapshot(post_commit_snapshot)

        updated_run = mark_release_commit_recorded(
            store,
            task_id=run["task_id"],
            commit_sha=commit_sha,
            commit_message=commit_message,
            post_commit_snapshot=compact_snapshot(post_commit_snapshot),
        )
        if not updated_run:
            raise SystemExit("release commit was recorded but release run was not found")
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
                "post_commit_full_check_reused": verification_mode == "full",
                "release_prepare_check_mode": verification_mode,
                "required_full_check": _required_full_check_metadata(verification_row, reused=verification_mode == "full"),
            }
        )
        store.update("recipe_runs", updated_run["id"], {"metadata": metadata, "updated_at": now_iso()})
        updated_run = store.get("recipe_runs", updated_run["id"]) or updated_run
        if updated_run.get("status") != "waiting_public_approval":
            if updated_run.get("status") == "active" and not (updated_run.get("metadata") or {}).get("required_checks_passed"):
                _run_lightweight_post_commit_checks(project_id, cwd, str(db_path))
                print(f"- commit: {commit_sha}")
                print("- recipe_run: active")
                print("- required_checks: full_check_deferred")
                print("- pending_public_operations: none")
                return
            raise SystemExit("release commit was recorded but public approval gate did not open")

        _run_lightweight_post_commit_checks(project_id, cwd, str(db_path))
        final_run = store.get("recipe_runs", updated_run["id"])
        print(f"- commit: {commit_sha}")
        print("- recipe_run: waiting_public_approval")
        print("- pending_public_operations: created")
        print(f"approval: {public_approval_text(final_run)}")
        print(f"publish: {_release_publish_command(project_id, final_run)}")
    finally:
        store.close()


def _run_release_full_check(store: Store, run: dict[str, Any], cwd: Path, timeout: float, *, context: str = "prepare") -> dict[str, Any]:
    verification = run_local_verification(RELEASE_FULL_CHECK_COMMAND, cwd, timeout, snapshot_mode="full")
    verification.setdefault("metadata", {})["verification_mode"] = "full"
    verification["metadata"]["release_prepare"] = context == "prepare"
    verification["metadata"]["release_publish"] = context == "publish"
    verification["metadata"]["release_full_check_command"] = RELEASE_FULL_CHECK_COMMAND
    verification["metadata"]["release_target_version"] = release_version_from_run(run).lstrip("v")
    verification["metadata"]["release_effective_dirty_hash"] = _release_effective_worktree_hash(cwd)
    verification_row = {"id": make_id("verification"), "task_id": run["task_id"], "evidence_check_id": None, **verification}
    try:
        record_verification_run(store, run["task_id"], row=verification_row, actor="nilo")
    except TransitionError as exc:
        raise SystemExit(f"{exc.message}{(': ' + exc.remediation) if exc.remediation else ''}") from exc
    return verification_row


def _run_release_changed_check(store: Store, run: dict[str, Any], cwd: Path, timeout: float) -> dict[str, Any]:
    verification = run_local_verification(RELEASE_CHANGED_CHECK_COMMAND, cwd, timeout, snapshot_mode="fast")
    verification.setdefault("metadata", {})["verification_mode"] = "changed"
    verification["metadata"]["release_prepare"] = True
    verification["metadata"]["release_changed_check_command"] = RELEASE_CHANGED_CHECK_COMMAND
    verification["metadata"]["release_full_check_deferred"] = True
    verification["metadata"]["release_target_version"] = release_version_from_run(run).lstrip("v")
    verification["metadata"]["release_effective_dirty_hash"] = _release_effective_worktree_hash(cwd)
    verification_row = {"id": make_id("verification"), "task_id": run["task_id"], "evidence_check_id": None, **verification}
    try:
        record_verification_run(store, run["task_id"], row=verification_row, actor="nilo")
    except TransitionError as exc:
        raise SystemExit(f"{exc.message}{(': ' + exc.remediation) if exc.remediation else ''}") from exc
    return verification_row


def cmd_release_publish(args: argparse.Namespace) -> None:
    cwd = Path.cwd()
    store = Store(_resolved_db_path(args.db, cwd))
    try:
        if not store.get("projects", args.project):
            raise SystemExit(f"project not found: {args.project}")
        run = active_recipe_run(store, args.project)
        if not run or run.get("recipe_name") != "release":
            raise SystemExit("active release recipe run not found")
        validate_public_operation_approval(args.approval, release_version_from_run(run))
        _ensure_release_publish_full_check(store, run, cwd, getattr(args, "timeout", 600.0))
        try:
            run, logs = execute_pending_public_operations(
                store,
                project_id=args.project,
                approval=args.approval,
                release_url=args.release_url,
                cwd=cwd,
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


def _ensure_release_publish_full_check(store: Store, run: dict[str, Any], cwd: Path, timeout: float) -> dict[str, Any]:
    target_version = release_version_from_run(run).lstrip("v")
    verification_row = reusable_full_verification_for_release(store, run["task_id"], cwd, target_version=target_version)
    if verification_row:
        _record_release_publish_full_check(store, run, verification_row, reused=True)
        print(f"- full_check: reused {verification_row['id']}")
        if verification_row.get("reuse_reason") == "current_full_check":
            print("- reason: same HEAD, clean tree, same command, same mode, exit 0")
        else:
            print(f"- reason: {verification_row.get('reuse_reason', 'current_full_check')}")
        print(f"- verification_run: {verification_row['id']}")
        print(f"- verification_reused: {verification_row.get('reuse_reason', 'current_full_check')}")
        return verification_row

    verification_row = _run_release_full_check(store, run, cwd, timeout, context="publish")
    _record_release_publish_full_check(store, run, verification_row, reused=False)
    print(f"- full_check: {RELEASE_FULL_CHECK_COMMAND}")
    print(f"- full_check_exit_code: {verification_row['exit_code']}")
    print(f"- verification_run: {verification_row['id']}")
    if verification_row.get("timed_out") or verification_row.get("exit_code") != 0:
        _pause_release_for_fix(
            store,
            run,
            reason="full_check_failed",
            verification_row=verification_row,
            failed_verification_id=verification_row["id"],
            managed_release_dirty=[],
            unmanaged_dirty=[],
        )
        print("- recipe_run: paused_for_fix")
        print("- blocked_reason: failed_verification")
        print(f"- failed_verification_id: {verification_row['id']}")
        raise SystemExit("release publish full check failed; public operations not executed; release recipe paused_for_fix")
    return verification_row


def _record_release_publish_full_check(store: Store, run: dict[str, Any], verification_row: dict[str, Any], *, reused: bool) -> None:
    current = store.get("recipe_runs", run["id"]) or run
    metadata = {**(current.get("metadata") or {})}
    passed = not verification_row.get("timed_out") and verification_row.get("exit_code") in (0, "0")
    metadata.update(
        {
            "release_publish_full_check_id": verification_row["id"],
            "release_publish_full_check_command": verification_row.get("command") or RELEASE_FULL_CHECK_COMMAND,
            "release_publish_full_check_required": True,
            "release_publish_full_check_passed": passed,
            "release_publish_full_check_reused": reused,
            "required_full_check": _required_full_check_metadata(verification_row, reused=reused),
        }
    )
    if reused:
        metadata["release_publish_full_check_reuse_reason"] = verification_row.get("reuse_reason", "current_full_check")
    store.update("recipe_runs", current["id"], {"metadata": metadata, "updated_at": now_iso()})


def _ensure_release_run(store: Store, args: argparse.Namespace, project_id: str, cwd: Path, *, resume: bool = False) -> dict[str, Any]:
    run = active_recipe_run(store, project_id)
    if run:
        if run.get("recipe_name") != "release":
            raise SystemExit("another recipe is already active")
        if resume and run.get("status") != "paused_for_fix":
            raise SystemExit(f"release recipe is not paused_for_fix: {run.get('status')}")
        if not resume and run.get("status") == "paused_for_fix":
            raise SystemExit("release recipe is paused_for_fix; run nilo release resume --project " + project_id)
        run_version = release_version_from_run(run).lstrip("v")
        requested_version = (getattr(args, "target_version", "") or "").lstrip("v")
        if requested_version and run_version and requested_version != run_version:
            raise SystemExit(f"active release recipe target_version is {run_version}; got {requested_version}")
        return run
    if resume:
        raise SystemExit("paused release recipe run not found")
    data = discover_recipes(cwd)
    source = _find_recipe(data["effective_recipes"], "release")
    if not source:
        raise SystemExit("recipe not found: release")
    raw_vars = [f"target_version={args.target_version}"] if args.target_version else []
    rendered, variable_messages = _render_task_fields(source, project_id, raw_vars, "", cwd)
    for message in variable_messages:
        print(message)
    adopted = _adopt_release_work_task(store, project_id, source, rendered)
    if adopted:
        from ..workflow_context import create_recipe_run

        print(f"- adopted_task: {adopted['id']}")
        return create_recipe_run(store, project_id=project_id, task_id=adopted["id"], recipe_name="release", rendered_fields=rendered)
    recipe_args = argparse.Namespace(
        db=args.db,
        task_type="implementation",
        risk="medium",
        commitment="",
    )
    task_id = _create_recipe_task(recipe_args, project_id, source, rendered)
    from ..workflow_context import create_recipe_run

    return create_recipe_run(store, project_id=project_id, task_id=task_id, recipe_name="release", rendered_fields=rendered)


def _adopt_release_work_task(store: Store, project_id: str, source: Any, rendered: dict[str, Any]) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for task in store.list_where("tasks", "project_id=?", (project_id,)):
        if task.get("status") not in {"planned", "instruction_generated"}:
            continue
        if recipe_run_for_existing_task(store, task["id"]):
            continue
        acceptance = task.get("acceptance_criteria") or []
        if any(str(item).strip().lower() == "recipe: release" for item in acceptance):
            candidates.append(task)
    if len(candidates) != 1:
        return None
    task = candidates[0]
    updates = {
        "title": rendered["title"],
        "description": rendered["description"],
        "acceptance_criteria": rendered["acceptance"],
    }
    store.update("tasks", task["id"], updates)
    if not store.latest_for_task("recipe_task_provenance", task["id"]):
        store.insert(
            "recipe_task_provenance",
            {
                "id": make_id("recipe_provenance"),
                "task_id": task["id"],
                "recipe_name": source.name,
                "source_layer": source.layer,
                "source_id": source.source_id,
                "content_hash": source.content_hash,
                "rendered_fields": rendered,
                "recipe_snapshot": source.to_dict(),
                "created_at": now_iso(),
            },
        )
    return store.get("tasks", task["id"]) or {**task, **updates}


def recipe_run_for_existing_task(store: Store, task_id: str) -> dict[str, Any] | None:
    runs = store.list_where("recipe_runs", "task_id=?", (task_id,))
    return runs[0] if runs else None


def _resolved_db_path(db_path: Path | None, cwd: Path) -> Path:
    return resolve_project_boundary(cwd, db_path=db_path).db_path


def release_managed_files(version: str) -> list[str]:
    return [
        "pyproject.toml",
        "src/nilo/__init__.py",
        f"docs/releases/{version}.md",
    ]


def _managed_files_for_run(run: dict[str, Any], version: str) -> list[str]:
    metadata = run.get("metadata") or {}
    managed = metadata.get("managed_release_files") or metadata.get("release_prepare_managed_files") or []
    if managed:
        return [str(path).replace("\\", "/") for path in managed]
    return release_managed_files(version)


def _store_release_metadata(store: Store, run: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    current = store.get("recipe_runs", run["id"]) or run
    metadata = {**(current.get("metadata") or {}), **values}
    store.update("recipe_runs", current["id"], {"metadata": metadata, "updated_at": now_iso()})
    updated = store.get("recipe_runs", current["id"])
    return updated or {**current, "metadata": metadata}


def _pause_release_for_fix(
    store: Store,
    run: dict[str, Any],
    *,
    reason: str,
    verification_row: dict[str, Any] | None = None,
    failed_verification_id: str = "",
    managed_release_dirty: list[str] | None = None,
    unmanaged_dirty: list[str] | None = None,
) -> dict[str, Any]:
    current = store.get("recipe_runs", run["id"]) or run
    verification = verification_row or (store.get("verification_runs", failed_verification_id) if failed_verification_id else None) or {}
    verification_metadata = verification.get("metadata") or {}
    failed_summary_path = (
        verification_metadata.get("failed_summary_path")
        or verification_metadata.get("summary_path")
        or verification_metadata.get("summary_json")
        or ""
    )
    failed_shards = verification_metadata.get("failed_shards") or verification_metadata.get("failed_shard_ids") or []
    metadata = {
        **(current.get("metadata") or {}),
        "pause_reason": reason,
        "blocked_reason": "failed_verification" if failed_verification_id else reason,
        "failed_verification_id": failed_verification_id,
        "failed_verification": _failed_verification_metadata(verification),
        "failed_summary_path": failed_summary_path,
        "failed_shards": failed_shards or (["unknown"] if failed_verification_id else []),
        "managed_release_dirty": managed_release_dirty or [],
        "unmanaged_dirty": unmanaged_dirty or [],
        "resume_command": f"nilo release resume --project {current['project_id']}",
    }
    store.update(
        "recipe_runs",
        current["id"],
        {
            "status": "paused_for_fix",
            "current_step": "paused_for_fix",
            "pending_steps": ["fix_and_resume", "run_required_checks", "commit", "tag", "push_main", "push_tag", "create_github_release", "verify_release", "complete"],
            "pending_public_operations": [],
            "metadata": metadata,
            "updated_at": now_iso(),
        },
    )
    return store.get("recipe_runs", current["id"]) or current


def _release_prepare_already_satisfied(run: dict[str, Any]) -> bool:
    metadata = run.get("metadata") or {}
    required = metadata.get("required_full_check") or {}
    return (
        run.get("recipe_name") == "release"
        and run.get("status") == "waiting_public_approval"
        and bool(metadata.get("required_checks_passed"))
        and required.get("status") == "satisfied"
        and bool(metadata.get("commit_sha"))
    )


def _required_full_check_metadata(verification_row: dict[str, Any], *, reused: bool) -> dict[str, Any]:
    metadata = verification_row.get("metadata") or {}
    verification_mode = metadata.get("verification_mode") or metadata.get("snapshot_mode") or ""
    passed = not verification_row.get("timed_out") and verification_row.get("exit_code") in (0, "0")
    if verification_mode != "full":
        status = "deferred" if passed else "failed"
    else:
        status = "satisfied" if passed else "failed"
    return {
        "status": status,
        "verification_id": verification_row.get("id", ""),
        "reused": reused,
        "mode": verification_mode,
        "git_head": verification_row.get("git_head", ""),
        "git_diff_hash": verification_row.get("git_diff_hash", ""),
        "working_tree_dirty": bool(verification_row.get("working_tree_dirty")),
        "command": verification_row.get("command") or RELEASE_FULL_CHECK_COMMAND,
    }


def _failed_verification_metadata(verification_row: dict[str, Any]) -> dict[str, Any]:
    if not verification_row:
        return {}
    metadata = verification_row.get("metadata") or {}
    return {
        "verification_id": verification_row.get("id", ""),
        "command": verification_row.get("command", ""),
        "mode": metadata.get("verification_mode") or metadata.get("snapshot_mode") or "",
        "exit_code": verification_row.get("exit_code"),
        "git_head": verification_row.get("git_head", ""),
        "git_diff_hash": verification_row.get("git_diff_hash", ""),
        "working_tree_dirty": bool(verification_row.get("working_tree_dirty")),
        "failed_summary_path": metadata.get("failed_summary_path") or metadata.get("summary_path") or metadata.get("summary_json") or "",
        "failed_shards": metadata.get("failed_shards") or metadata.get("failed_shard_ids") or ["unknown"],
    }


def _classify_dirty_files(cwd: Path, managed_files: list[str]) -> dict[str, list[str]]:
    dirty = _git_changed_files(cwd)
    managed = set(managed_files)
    return {
        "managed_release_dirty": sorted(path for path in dirty if path in managed),
        "unmanaged_dirty": sorted(path for path in dirty if path not in managed),
    }


def _raise_unmanaged_dirty(classified: dict[str, list[str]], project_id: str) -> None:
    lines = ["release recipe paused_for_fix: unmanaged dirty files"]
    lines.append("release-managed dirty files:")
    for path in classified["managed_release_dirty"] or ["(none)"]:
        lines.append(f"- {path}")
    lines.append("unmanaged dirty files:")
    for path in classified["unmanaged_dirty"] or ["(none)"]:
        lines.append(f"- {path}")
    lines.append(f"suggested action: commit or revert unmanaged files, then run nilo release resume --project {project_id}")
    raise SystemExit("\n".join(lines))


def reusable_full_verification_for_release(store: Store, task_id: str, cwd: Path, *, target_version: str) -> dict[str, Any] | None:
    current = current_git_snapshot(cwd)
    for verification in store.list_where("verification_runs", "task_id=?", (task_id,)):
        metadata = verification.get("metadata") or {}
        if verification.get("timed_out") or verification.get("exit_code") not in (0, "0"):
            continue
        if metadata.get("verification_mode") != "full":
            continue
        verification_version = str(metadata.get("release_target_version") or "").lstrip("v")
        if verification_version and verification_version != target_version.lstrip("v"):
            continue
        if not _same_release_full_check(verification.get("command", "")):
            continue
        if _release_snapshot_matches_verification(verification, current, cwd):
            return {**verification, "reuse_reason": "current_full_check", "snapshot_relation": "current_snapshot"}
        relation = _verified_dirty_tree_committed_relation(cwd, verification, current)
        if relation:
            return {**verification, **relation}
        relation = _release_metadata_only_relation(cwd, verification, current, target_version=target_version)
        if relation:
            return {**verification, **relation}
    return None


def _same_release_full_check(command: str) -> bool:
    return command.strip() == RELEASE_FULL_CHECK_COMMAND


def _verified_dirty_tree_committed_relation(cwd: Path, verification: dict[str, Any], current: dict[str, Any]) -> dict[str, Any] | None:
    if not verification.get("working_tree_dirty") or current.get("working_tree_dirty"):
        return None
    verified_head = verification.get("git_head") or ""
    if not verified_head:
        return None
    parent = _git_value_or_empty(cwd, ["rev-parse", "HEAD^"])
    if parent != verified_head:
        return None
    metadata = verification.get("metadata") or {}
    verified_hash = metadata.get("release_effective_dirty_hash") or ""
    if not verified_hash:
        return None
    if verified_hash != _release_effective_commit_hash(cwd, "HEAD"):
        return None
    return {
        "reuse_reason": "verified_dirty_tree_matches_current_commit",
        "commit_sha": current.get("git_head") or "",
        "snapshot_relation": "verified_dirty_tree_committed",
    }


def _release_metadata_only_relation(cwd: Path, verification: dict[str, Any], current: dict[str, Any], *, target_version: str) -> dict[str, Any] | None:
    verified_head = verification.get("git_head") or ""
    if not verified_head:
        return None
    if not release_only_non_execution_changes(cwd, target_version, verified_head):
        return None
    return {
        "reuse_reason": "release_metadata_only_changes",
        "snapshot_relation": "release_metadata_only_changes",
        "release_metadata_only_base": verified_head,
        "commit_sha": current.get("git_head") or "",
    }


def release_only_non_execution_changes(cwd: Path, target_version: str, base_ref: str, head_ref: str | None = None) -> bool:
    changed_paths = _release_changed_paths_between(cwd, base_ref, head_ref)
    if not changed_paths:
        return False
    release_note = f"docs/releases/{target_version.lstrip('v')}.md"
    allowed_paths = {release_note, "pyproject.toml", "src/nilo/__init__.py"}
    if any(path not in allowed_paths for path in changed_paths):
        return False
    for path in changed_paths:
        if path == release_note:
            if _release_path_bytes(cwd, head_ref, path) is None:
                return False
            continue
        old_text = _release_path_text(cwd, base_ref, path)
        new_text = _release_path_text(cwd, head_ref, path)
        if old_text is None or new_text is None:
            return False
        if path == "pyproject.toml" and not _only_matching_lines_changed(old_text, new_text, r"^version\s*="):
            return False
        if path == "src/nilo/__init__.py" and not _only_matching_lines_changed(old_text, new_text, r"^__version__\s*="):
            return False
    return True


def _release_changed_paths_between(cwd: Path, base_ref: str, head_ref: str | None) -> set[str]:
    args = ["diff", "--name-only", base_ref]
    if head_ref is not None:
        args.append(head_ref)
    code, out, _ = _git_output(cwd, args)
    if code != 0:
        return set()
    paths = {line.strip().replace("\\", "/") for line in out.splitlines() if line.strip()}
    if head_ref is None:
        code, out, _ = _git_output(cwd, ["ls-files", "--others", "--exclude-standard"])
        if code == 0:
            paths.update(line.strip().replace("\\", "/") for line in out.splitlines() if line.strip())
    return {path for path in paths if not _release_ignored_dirty_path(path)}


def _release_path_text(cwd: Path, ref: str | None, path: str) -> str | None:
    content = _release_path_bytes(cwd, ref, path)
    if content is None:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _release_path_bytes(cwd: Path, ref: str | None, path: str) -> bytes | None:
    if ref is None:
        file_path = cwd / path
        return file_path.read_bytes() if file_path.is_file() else None
    completed = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    return completed.stdout if completed.returncode == 0 else None


def _only_matching_lines_changed(old_text: str, new_text: str, pattern: str) -> bool:
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    if len(old_lines) != len(new_lines):
        return False
    changed = [(old, new) for old, new in zip(old_lines, new_lines) if old != new]
    if not changed:
        return False
    return all(re.match(pattern, old) and re.match(pattern, new) for old, new in changed)


def _release_snapshot_matches_verification(verification: dict[str, Any], current: dict[str, Any], cwd: Path) -> bool:
    if compact_snapshot(verification) == compact_snapshot(current):
        return True
    if (verification.get("git_head") or "") != (current.get("git_head") or ""):
        return False
    if bool(verification.get("working_tree_dirty")) != bool(current.get("working_tree_dirty")):
        return False
    metadata = verification.get("metadata") or {}
    verified_hash = metadata.get("release_effective_dirty_hash") or ""
    return bool(verified_hash) and verified_hash == _release_effective_worktree_hash(cwd)


def _without_release_ignored_paths(paths: list[Any]) -> set[str]:
    return {str(path).replace("\\", "/") for path in paths if not _release_ignored_dirty_path(str(path).replace("\\", "/"))}


def _release_sanitize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    paths = sorted(_without_release_ignored_paths(snapshot.get("observed_paths") or []))
    if paths:
        return {**snapshot, "observed_paths": paths}
    return {**snapshot, "working_tree_dirty": False, "git_diff_hash": "", "git_status_porcelain": "", "observed_paths": []}


def _release_effective_worktree_hash(cwd: Path) -> str:
    return _release_content_hash(cwd, _git_changed_files(cwd), old_ref="HEAD", new_ref=None)


def _release_effective_commit_hash(cwd: Path, commit: str) -> str:
    parent = _git_value_or_empty(cwd, ["rev-parse", f"{commit}^"])
    if not parent:
        return ""
    code, out, _ = _git_output(cwd, ["diff-tree", "--no-commit-id", "--name-only", "-r", commit])
    if code != 0:
        return ""
    paths = {path.strip().replace("\\", "/") for path in out.splitlines() if path.strip()}
    paths = {path for path in paths if not _release_ignored_dirty_path(path)}
    return _release_content_hash(cwd, paths, old_ref=parent, new_ref=commit)


def _release_content_hash(cwd: Path, paths: set[str], *, old_ref: str, new_ref: str | None) -> str:
    hasher = hashlib.sha256()
    for path in sorted(paths):
        hasher.update(f"path:{path}\n".encode())
        hasher.update(b"old:\0")
        hasher.update(_git_blob_bytes(cwd, old_ref, path))
        hasher.update(b"\nnew:\0")
        if new_ref is None:
            file_path = cwd / path
            hasher.update(file_path.read_bytes() if file_path.is_file() else b"__NILO_FILE_MISSING__")
        else:
            hasher.update(_git_blob_bytes(cwd, new_ref, path))
        hasher.update(b"\n")
    return hasher.hexdigest()


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
        release_note.write_text(_release_note_template(cwd, version), encoding="utf-8")
        changed.append(release_note.relative_to(cwd).as_posix())
    return changed


def _replace_version_line(path: Path, pattern: str, version: str) -> bool:
    text = path.read_text(encoding="utf-8")
    updated = re.sub(pattern, rf"\g<1>{version}\2", text, count=1, flags=re.MULTILINE)
    if updated == text:
        return False
    path.write_text(updated, encoding="utf-8")
    return True


def _release_note_template(cwd: Path, version: str) -> str:
    evidence_ja, evidence_en = _release_note_evidence(cwd)
    return f"""# v{version}

## リリースノート（日本語）

このファイルは release prepare が作成する下書きです。公開前に、このリリースで変わった機能、修正、検証証跡を具体的に記入してください。

### 変更点

- 具体的な変更点を記入する。

### 検証

- 実行した full check / changed check と結果を記入する。
- {evidence_ja}
- 公開は人間承認後に tag / push / GitHub release を実行する。

## Release Notes (English)

This file is a draft created by release prepare. Before publishing, replace this text with the concrete feature changes, fixes, and verification evidence for this release.

### Changes

- Describe the concrete changes in this release.

### Verification

- Record the full check / changed check command and result.
- {evidence_en}
- Publishing remains gated by explicit human approval for tag, push, and GitHub release creation.
"""


def _release_note_evidence(cwd: Path) -> tuple[str, str]:
    code, latest_tag, _ = _git_output(cwd, ["describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"])
    if code != 0 or not latest_tag.strip():
        return (
            "変更範囲: 直近の release tag を自動検出できませんでした。",
            "Change range: latest release tag could not be detected automatically.",
        )
    tag = latest_tag.strip()
    code, log_text, _ = _git_output(cwd, ["log", "--oneline", "--no-decorate", f"{tag}..HEAD"])
    commits = [line.strip() for line in log_text.splitlines() if line.strip()] if code == 0 else []
    if not commits:
        return (
            f"変更範囲: `{tag}..HEAD` に release note へ反映する commit はまだありません。",
            f"Change range: `{tag}..HEAD` has no commits to summarize yet.",
        )
    commit_text = "; ".join(commits[:12])
    suffix = " ..." if len(commits) > 12 else ""
    return (
        f"変更範囲: `{tag}..HEAD`; 対象 commit: {commit_text}{suffix}",
        f"Change range: `{tag}..HEAD`; commits: {commit_text}{suffix}",
    )


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
            normalized = path.replace("\\", "/")
            if _release_ignored_dirty_path(normalized):
                continue
            files.add(normalized)
    return files


def _release_ignored_dirty_path(path: str) -> bool:
    return path == ".nilo/nilo.db" or path.startswith(".nilo/nilo.db-")


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


def _git_value_or_empty(cwd: Path, args: list[str]) -> str:
    code, out, _ = _git_output(cwd, args)
    return out.strip() if code == 0 else ""


def _git_blob_bytes(cwd: Path, ref: str, path: str) -> bytes:
    completed = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    return completed.stdout if completed.returncode == 0 else b"__NILO_FILE_MISSING__"


def _git_output(cwd: Path, args: list[str]) -> tuple[int, str, str]:
    completed = subprocess.run(["git", *args], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    return completed.returncode, completed.stdout.rstrip("\n"), completed.stderr.rstrip("\n")
