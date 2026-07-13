from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable

from .cli_support import make_id
from .timeutil import now_iso


RELEASE_STEPS = [
    "prepare_version",
    "run_required_checks",
    "commit",
    "tag",
    "push_main",
    "push_tag",
    "create_github_release",
    "verify_release",
    "complete",
]

ACTIVE_RECIPE_RUN_STATUSES = {"active", "paused_for_fix", "waiting_public_approval"}
PUBLIC_OPERATION_APPROVAL_TEXT = '"v{version} を tag/push/release して"'


def _atomic_store_operation(func):
    def wrapper(store, *args, **kwargs):
        with store.transaction():
            return func(store, *args, **kwargs)

    return wrapper


def create_recipe_run(
    store,
    *,
    project_id: str,
    task_id: str,
    recipe_name: str,
    rendered_fields: dict[str, Any],
) -> dict[str, Any]:
    now = now_iso()
    if recipe_name == "release":
        pending_steps = RELEASE_STEPS[1:]
        current_step = RELEASE_STEPS[0]
    else:
        pending_steps = []
        current_step = "task_created"
    row = {
        "id": make_id("recipe_run"),
        "project_id": project_id,
        "task_id": task_id,
        "recipe_name": recipe_name,
        "status": "active",
        "current_step": current_step,
        "completed_steps": [],
        "pending_steps": pending_steps,
        "pending_public_operations": [],
        "metadata": {"rendered_fields": rendered_fields},
        "created_at": now,
        "updated_at": now,
    }
    store.insert("recipe_runs", row)
    return row


def active_recipe_run(store, project_id: str) -> dict[str, Any] | None:
    runs = store.list_where(
        "recipe_runs",
        "project_id=? AND status IN ('active', 'paused_for_fix', 'waiting_public_approval')",
        (project_id,),
    )
    return runs[0] if runs else None


def recipe_run_for_task(store, task_id: str, recipe_name: str = "") -> dict[str, Any] | None:
    where = "task_id=?"
    args: tuple[Any, ...] = (task_id,)
    if recipe_name:
        where += " AND recipe_name=?"
        args = (task_id, recipe_name)
    runs = store.list_where("recipe_runs", where, args)
    return runs[0] if runs else None


def release_version_from_run(run: dict[str, Any]) -> str:
    metadata = run.get("metadata") or {}
    rendered = metadata.get("rendered_fields") or {}
    for value in (rendered.get("title"), rendered.get("description"), rendered.get("instruction")):
        if isinstance(value, str):
            match = re.search(r"\bv?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\b", value)
            if match:
                return match.group(1)
    return str(metadata.get("target_version") or "").lstrip("v")


def public_operations_for_release(version: str, branch: str = "main") -> list[dict[str, str]]:
    tag = f"v{version.lstrip('v')}" if version else "v<target_version>"
    return [
        {"operation": "create_tag", "target": tag},
        {"operation": "push_branch", "target": branch},
        {"operation": "push_tag", "target": tag},
        {"operation": "create_github_release", "target": tag},
    ]


def mark_release_commit_recorded(
    store,
    *,
    task_id: str,
    commit_sha: str,
    commit_message: str,
    post_commit_snapshot: dict[str, Any],
) -> dict[str, Any] | None:
    run = recipe_run_for_task(store, task_id, "release")
    if not run:
        return None
    version = release_version_from_run(run)
    metadata = {
        **(run.get("metadata") or {}),
        "commit_sha": commit_sha,
        "commit_message": commit_message,
        "post_commit_snapshot": post_commit_snapshot,
    }
    checks_passed = release_required_checks_passed(store, task_id, run_metadata=metadata)
    if not checks_passed:
        store.update(
            "recipe_runs",
            run["id"],
            {
                "status": "active",
                "current_step": "run_required_checks",
                "completed_steps": ["commit"],
                "pending_steps": ["run_required_checks", "tag", "push_main", "push_tag", "create_github_release", "verify_release", "complete"],
                "pending_public_operations": [],
                "metadata": {**metadata, "required_checks_passed": False},
                "updated_at": now_iso(),
            },
        )
        return store.get("recipe_runs", run["id"])
    store.update(
        "recipe_runs",
        run["id"],
        {
            "status": "waiting_public_approval",
            "current_step": "public_release",
            "completed_steps": ["prepare_version", "run_required_checks", "commit"],
            "pending_steps": ["tag", "push_main", "push_tag", "create_github_release", "verify_release", "complete"],
            "pending_public_operations": public_operations_for_release(version),
            "metadata": {**metadata, "required_checks_passed": True},
            "updated_at": now_iso(),
        },
    )
    return store.get("recipe_runs", run["id"])


def release_commit_transition_metadata(store, task_id: str) -> dict[str, Any]:
    from .snapshot import compact_snapshot

    run = recipe_run_for_task(store, task_id, "release")
    if not run:
        return {}
    metadata = run.get("metadata") or {}
    if (metadata.get("required_full_check") or {}).get("snapshot_relation") == "release_metadata_only_changes":
        return {}
    if not metadata.get("commit_sha"):
        return {}
    verification = release_required_full_verification(store, task_id, run_metadata=metadata)
    verification_snapshot = metadata.get("verification_snapshot") or compact_snapshot(verification or {})
    pre_commit_snapshot = metadata.get("pre_commit_snapshot") or verification_snapshot
    post_commit_snapshot = metadata.get("post_commit_snapshot") or {}
    if not verification_snapshot or not pre_commit_snapshot or not post_commit_snapshot:
        return {}
    return {
        "verified_snapshot": verification_snapshot,
        "pre_commit_snapshot": pre_commit_snapshot,
        "post_commit_snapshot": post_commit_snapshot,
        "commit_sha": metadata.get("commit_sha", ""),
        "commit_message": metadata.get("commit_message", ""),
        "committed_from_verified_dirty_tree": True,
        "verified_diff_hash": verification_snapshot.get("git_diff_hash", ""),
        "committed_tree_hash": metadata.get("committed_tree_hash", ""),
        "committed_files": metadata.get("committed_files") or [],
    }


def release_commit_aware_evidence_status(
    store,
    task_id: str,
    verification_run: dict[str, Any] | None,
    current_snapshot: dict[str, Any],
    *,
    strict: bool = True,
) -> str:
    from .snapshot import commit_transition_evidence_status, evidence_status

    run = recipe_run_for_task(store, task_id, "release")
    if _release_metadata_only_required_check_is_current(run, verification_run, current_snapshot):
        return "current"
    transition = release_commit_transition_metadata(store, task_id)
    if not transition:
        return evidence_status(verification_run, current_snapshot, strict=strict)
    status = commit_transition_evidence_status(verification_run, current_snapshot, transition, strict=strict)
    if status == "current":
        return status
    required = release_required_full_verification(store, task_id, run_metadata=(run.get("metadata") or {}) if run else {})
    if required and (not verification_run or required.get("id") != verification_run.get("id")):
        required_status = commit_transition_evidence_status(required, current_snapshot, transition, strict=strict)
        if required_status == "current":
            return required_status
    return status


def release_commit_verified_verification(
    store,
    task_id: str,
    verification_run: dict[str, Any] | None,
    current_snapshot: dict[str, Any],
) -> dict[str, Any] | None:
    from .snapshot import commit_transition_evidence_status

    run = recipe_run_for_task(store, task_id, "release")
    required = release_required_full_verification(store, task_id, run_metadata=(run.get("metadata") or {}) if run else {})
    if required and _release_metadata_only_required_check_is_current(run, required, current_snapshot):
        return required
    transition = release_commit_transition_metadata(store, task_id)
    if not transition:
        return None
    if commit_transition_evidence_status(verification_run, current_snapshot, transition) == "current":
        return verification_run
    if required and commit_transition_evidence_status(required, current_snapshot, transition) == "current":
        return required
    return None


def _release_metadata_only_required_check_is_current(
    run: dict[str, Any] | None,
    verification_run: dict[str, Any] | None,
    current_snapshot: dict[str, Any],
) -> bool:
    if not run or not verification_run:
        return False
    required = (run.get("metadata") or {}).get("required_full_check") or {}
    return bool(
        required.get("status") == "satisfied"
        and required.get("snapshot_relation") == "release_metadata_only_changes"
        and required.get("verification_id") == verification_run.get("id")
        and required.get("reuse_commit_sha")
        and required.get("reuse_commit_sha") == current_snapshot.get("git_head")
        and not current_snapshot.get("working_tree_dirty")
    )


def release_required_checks_passed(store, task_id: str, *, run_metadata: dict[str, Any] | None = None) -> bool:
    return release_required_full_verification(store, task_id, run_metadata=run_metadata) is not None


def release_required_full_verification(store, task_id: str, *, run_metadata: dict[str, Any] | None = None) -> dict[str, Any] | None:
    required = (run_metadata or {}).get("required_full_check") or {}
    required_id = required.get("verification_id") or ""
    if required.get("status") == "satisfied" and required_id:
        verification = store.get("verification_runs", required_id)
        if (
            verification
            and verification.get("task_id") == task_id
            and _release_verification_satisfies_required_full_check(verification)
            and _release_required_full_check_matches_metadata(required, verification, run_metadata or {})
        ):
            return verification
    latest = store.latest_for_task("verification_runs", task_id)
    if latest and _release_verification_satisfies_required_full_check(latest) and _release_required_full_check_matches_metadata(required, latest, run_metadata or {}):
        return latest
    return None


def _release_required_full_check_matches_metadata(required: dict[str, Any], verification: dict[str, Any], run_metadata: dict[str, Any]) -> bool:
    for key in ("git_head", "git_diff_hash", "working_tree_dirty"):
        if key in required and required.get(key) != verification.get(key):
            return False
    # Prefer the verification snapshot because release metadata-only commits can
    # legitimately make the pre-commit snapshot differ from the reused full check.
    expected_snapshot = run_metadata.get("verification_snapshot") or run_metadata.get("pre_commit_snapshot") or {}
    for key in ("git_head", "git_diff_hash", "working_tree_dirty"):
        if key in expected_snapshot and expected_snapshot.get(key) != verification.get(key):
            return False
    return True


def _release_verification_satisfies_required_full_check(verification: dict[str, Any]) -> bool:
    metadata = verification.get("metadata") or {}
    return (
        not verification.get("timed_out")
        and verification.get("exit_code") in (0, "0")
        and metadata.get("verification_mode") == "full"
    )


def approve_pending_public_operations(
    store,
    *,
    project_id: str,
    approval: str,
    release_url: str = "",
    executed: bool = False,
) -> dict[str, Any]:
    run = active_recipe_run(store, project_id)
    if not run or run.get("recipe_name") != "release":
        raise ValueError("active release recipe run not found")
    metadata = run.get("metadata") or {}
    pending = run.get("pending_public_operations") or []
    if executed and not pending:
        pending = metadata.get("public_operations_approved") or []
    if not pending:
        raise ValueError("release recipe has no pending public operations")
    version = release_version_from_run(run)
    validate_public_operation_approval(approval, version)
    metadata = {
        **metadata,
        "public_operations_approved_by": approval,
        "public_operations_approved": pending,
    }
    if not executed:
        store.update(
            "recipe_runs",
            run["id"],
            {
                "status": "active",
                "current_step": "verify_release",
                "completed_steps": [*(run.get("completed_steps") or []), "public_operations_approved"],
                "pending_steps": ["verify_release", "complete"],
                "pending_public_operations": [],
                "metadata": metadata,
                "updated_at": now_iso(),
            },
        )
        return store.get("recipe_runs", run["id"])
    if not release_url:
        raise ValueError("release_url is required when recording executed public operations")
    metadata = {
        **metadata,
        "github_release_url": release_url,
        "public_operations_completed": pending,
    }
    store.update("recipe_runs", run["id"], {"metadata": metadata, "updated_at": now_iso()})
    from .transitions import complete_recipe_run

    complete_recipe_run(
        store,
        recipe_run_id=run["id"],
        actor="human",
        reason="release publish completed",
        decision_source="human_explicit",
        human_confirmed=True,
        decision_note=approval,
        cwd=Path.cwd(),
    )
    return store.get("recipe_runs", run["id"])


approve_pending_public_operations = _atomic_store_operation(approve_pending_public_operations)


def validate_public_operation_approval(approval: str, version: str) -> None:
    expected_tag = f"v{version.lstrip('v')}" if version else ""
    lowered = approval.lower()
    if expected_tag and expected_tag.lower() not in lowered:
        raise ValueError(f"approval must mention {expected_tag}")
    if not all(word in lowered for word in ("tag", "push", "release")):
        raise ValueError("approval must explicitly mention tag, push, and release")


def execute_pending_public_operations(
    store,
    *,
    project_id: str,
    approval: str,
    cwd: Path,
    release_url: str = "",
    branch: str = "main",
    preflight: Callable[[Any, dict[str, Any], Path], None] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    run = active_recipe_run(store, project_id)
    if not run or run.get("recipe_name") != "release":
        raise ValueError("active release recipe run not found")
    pending = run.get("pending_public_operations") or (run.get("metadata") or {}).get("public_operations_approved") or []
    if not pending:
        run = recover_missing_release_public_operations(store, project_id=project_id, cwd=cwd)
        pending = run.get("pending_public_operations") or (run.get("metadata") or {}).get("public_operations_approved") or []
    if not pending:
        raise ValueError("release recipe has no pending public operations and automatic recovery was not possible")
    version = release_version_from_run(run)
    validate_public_operation_approval(approval, version)

    tag = _release_tag_from_operations(pending) or (f"v{version.lstrip('v')}" if version else "")
    if not tag:
        raise ValueError("release tag could not be resolved")

    code, out, err = _run_command(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd)
    if code != 0:
        raise ValueError(err or "git status failed")
    if out.strip():
        raise ValueError("working tree must be clean before executing public release operations")

    if preflight is None:
        from .cli_handlers.release import _ensure_release_note_ready_for_publish

        preflight = _ensure_release_note_ready_for_publish
    preflight(store, run, cwd)

    if run.get("pending_public_operations"):
        # Record explicit approval immediately before crossing public-operation gates.
        run = approve_pending_public_operations(
            store,
            project_id=project_id,
            approval=approval,
            release_url=release_url,
            executed=False,
        )
        pending = (run.get("metadata") or {}).get("public_operations_approved") or pending

    logs: list[dict[str, Any]] = []
    for operation in pending:
        name = operation.get("operation")
        target = operation.get("target") or tag
        if name == "create_tag":
            logs.append(_ensure_git_tag(cwd, target))
        elif name == "push_branch":
            logs.append(_run_checked(["git", "push", "origin", target or branch], cwd))
        elif name == "push_tag":
            logs.append(_run_checked(["git", "push", "origin", target], cwd))
        elif name == "create_github_release":
            notes_file = Path("docs") / "releases" / f"{version}.md"
            command = ["gh", "release", "create", target, "--title", target]
            if (cwd / notes_file).exists():
                command.extend(["--notes-file", str(notes_file)])
            release_log = _run_checked(command, cwd)
            logs.append(release_log)
            if not release_url:
                release_url = _extract_release_url(str(release_log.get("stdout") or ""))
        else:
            raise ValueError(f"unsupported public release operation: {name}")

    if not release_url:
        view_log = _run_checked(["gh", "release", "view", tag, "--json", "url", "-q", ".url"], cwd)
        logs.append(view_log)
        release_url = str(view_log.get("stdout") or "").strip()
    if not release_url:
        raise ValueError("GitHub release URL could not be determined")

    completed = approve_pending_public_operations(
        store,
        project_id=project_id,
        approval=approval,
        release_url=release_url,
        executed=True,
    )
    return completed, logs


def recover_missing_release_public_operations(store, *, project_id: str, cwd: Path) -> dict[str, Any]:
    run = active_recipe_run(store, project_id)
    if not run or run.get("recipe_name") != "release":
        raise ValueError("active release recipe run not found")
    metadata = run.get("metadata") or {}
    if run.get("pending_public_operations"):
        return run
    if metadata.get("public_operations_approved"):
        return run
    if not release_required_checks_passed(store, run["task_id"], run_metadata=metadata):
        raise ValueError("release recipe has no pending public operations and required checks have not passed")

    code, out, err = _run_command(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd)
    if code != 0:
        raise ValueError(err or "git status failed")
    if out.strip():
        raise ValueError("release recipe has no pending public operations and working tree is dirty")

    version = release_version_from_run(run)
    tag = f"v{version.lstrip('v')}" if version else ""
    if tag:
        tag_code, _, _ = _run_command(["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"], cwd)
        if tag_code == 0:
            raise ValueError(f"release recipe has no pending public operations and tag already exists: {tag}")

    commit_sha = metadata.get("commit_sha") or _git_value_or_empty(cwd, ["rev-parse", "HEAD"])
    if not commit_sha:
        raise ValueError("release recipe has no pending public operations and release commit could not be resolved")
    commit_message = metadata.get("commit_message") or _git_value_or_empty(cwd, ["log", "-1", "--pretty=%s"]) or f"Release {version}"
    from .snapshot import compact_snapshot, current_git_snapshot

    recovered = mark_release_commit_recorded(
        store,
        task_id=run["task_id"],
        commit_sha=commit_sha,
        commit_message=commit_message,
        post_commit_snapshot=compact_snapshot(current_git_snapshot(cwd)),
    )
    if not recovered or not recovered.get("pending_public_operations"):
        raise ValueError("release recipe pending public operations could not be recovered")
    recovered_metadata = recovered.get("metadata") or {}
    recovered_metadata["public_operations_recovered"] = True
    store.update("recipe_runs", recovered["id"], {"metadata": recovered_metadata, "updated_at": now_iso()})
    return store.get("recipe_runs", recovered["id"])


def _git_value_or_empty(cwd: Path, args: list[str]) -> str:
    code, out, _ = _run_command(["git", *args], cwd)
    return out.strip() if code == 0 else ""


def _ensure_git_tag(cwd: Path, tag: str) -> dict[str, Any]:
    code, _, _ = _run_command(["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"], cwd)
    if code == 0:
        return {"command": ["git", "tag", tag], "exit_code": 0, "stdout": "already exists", "stderr": "", "skipped": True}
    return _run_checked(["git", "tag", tag], cwd)


def _run_checked(command: list[str], cwd: Path) -> dict[str, Any]:
    code, out, err = _run_command(command, cwd)
    log = {"command": command, "exit_code": code, "stdout": out, "stderr": err}
    if code != 0:
        display = " ".join(command)
        raise ValueError(err or f"command failed: {display}")
    return log


def _run_command(command: list[str], cwd: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return proc.returncode, proc.stdout.rstrip("\n"), proc.stderr.rstrip("\n")


def _extract_release_url(output: str) -> str:
    match = re.search(r"https?://\S+", output)
    return match.group(0) if match else ""


def workflow_context(store, project_id: str) -> dict[str, Any]:
    run = active_recipe_run(store, project_id)
    if not run:
        latest_release = store.list_where(
            "recipe_runs",
            "project_id=? AND recipe_name='release' AND status='completed'",
            (project_id,),
        )
        if latest_release:
            return {
                "type": "project",
                "status": "no_active_recipe",
                "latest_completed_release": release_completion_summary(store, latest_release[0]),
            }
        return {"type": "project", "status": "no_active_recipe"}
    context = {
        "type": "recipe_run",
        "recipe_run_id": run["id"],
        "recipe_name": run["recipe_name"],
        "task_id": run["task_id"],
        "status": run["status"],
        "target_version": release_version_from_run(run) if run["recipe_name"] == "release" else "",
        "current_step": run["current_step"],
        "next_step": next_step_for_recipe_run(run),
        "completed_steps": run.get("completed_steps") or [],
        "pending_steps": run.get("pending_steps") or [],
        "pending_public_operations": run.get("pending_public_operations") or [],
    }
    if run["status"] == "waiting_public_approval":
        context["approval_prompt"] = public_approval_prompt(run)
        context["public_execution_command"] = public_execution_command(project_id, run)
        context["release_publish_command"] = release_publish_command(project_id, run)
        context["required_approval_text"] = public_approval_text(run).strip('"')
    elif run["status"] == "paused_for_fix":
        metadata = run.get("metadata") or {}
        context["reason"] = metadata.get("pause_reason", "")
        context["blocked_reason"] = metadata.get("blocked_reason", metadata.get("pause_reason", ""))
        context["failed_verification_id"] = metadata.get("failed_verification_id", "")
        context["failed_verification"] = metadata.get("failed_verification") or {}
        context["failed_summary_path"] = metadata.get("failed_summary_path", "")
        context["failed_shards"] = metadata.get("failed_shards") or []
        context["resume_command"] = release_resume_command(project_id)
        context["managed_release_dirty"] = metadata.get("managed_release_dirty") or []
        context["unmanaged_dirty"] = metadata.get("unmanaged_dirty") or []
    elif run["recipe_name"] == "release":
        context["release_prepare_command"] = release_prepare_command(project_id, run)
    return context


def next_step_for_recipe_run(run: dict[str, Any]) -> str:
    if run.get("status") == "waiting_public_approval":
        return "await_public_operation_confirmation"
    if run.get("status") == "paused_for_fix":
        return "fix_and_resume"
    pending = run.get("pending_steps") or []
    return pending[0] if pending else "complete"


def public_approval_prompt(run: dict[str, Any]) -> str:
    return f"To proceed, explicitly say: {public_approval_text(run)}"


def public_approval_text(run: dict[str, Any]) -> str:
    version = release_version_from_run(run)
    return PUBLIC_OPERATION_APPROVAL_TEXT.format(version=version.lstrip("v") if version else "<target_version>")


def public_execution_command(project_id: str, run: dict[str, Any]) -> str:
    approval = public_approval_text(run)
    return " ".join(
        [
            "nilo",
            "recipe",
            "approve-public",
            "--project",
            shlex.quote(project_id),
            "--approval",
            shlex.quote(approval),
            "--execute",
        ]
    )


def release_prepare_command(project_id: str, run: dict[str, Any]) -> str:
    version = release_version_from_run(run)
    parts = ["nilo", "release", "prepare", "--project", shlex.quote(project_id)]
    if version:
        parts.extend(["--target-version", shlex.quote(version)])
    return " ".join(parts)


def release_publish_command(project_id: str, run: dict[str, Any]) -> str:
    approval = public_approval_text(run).strip('"')
    return " ".join(["nilo", "release", "publish", "--project", shlex.quote(project_id), "--approval", shlex.quote(approval)])


def release_resume_command(project_id: str) -> str:
    return " ".join(["nilo", "release", "resume", "--project", shlex.quote(project_id)])


def release_completion_summary(store, run: dict[str, Any]) -> dict[str, Any]:
    metadata = run.get("metadata") or {}
    snapshot = metadata.get("post_commit_snapshot") or {}
    pending = run.get("pending_public_operations") or []
    return {
        "recipe_run_id": run["id"],
        "task_id": run["task_id"],
        "status": run["status"],
        "commit": metadata.get("commit_sha", ""),
        "tag": _release_tag_from_operations(metadata.get("public_operations_completed") or pending),
        "pushed": ["main", _release_tag_from_operations(metadata.get("public_operations_completed") or pending)] if run["status"] == "completed" else [],
        "github_release": metadata.get("github_release_url", ""),
        "working_tree": "dirty" if snapshot.get("working_tree_dirty") else "clean",
        "release_task": "completed" if _release_task_completed(store, run["task_id"]) else "not_completed",
        "pending_public_operations": pending,
    }


def _release_task_completed(store, task_id: str) -> bool:
    from .task_logic import active_task_completion

    return active_task_completion(store, task_id) is not None


def _release_tag_from_operations(operations: list[dict[str, Any]]) -> str:
    for operation in operations:
        if operation.get("operation") in {"create_tag", "push_tag", "create_github_release"}:
            return str(operation.get("target") or "")
    return ""
