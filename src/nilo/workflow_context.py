from __future__ import annotations

import re
from typing import Any

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

ACTIVE_RECIPE_RUN_STATUSES = {"active", "waiting_public_approval"}
PUBLIC_OPERATION_APPROVAL_TEXT = '"v{version} を tag/push/release して"'


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
        "project_id=? AND status IN ('active', 'waiting_public_approval')",
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
    checks_passed = release_required_checks_passed(store, task_id)
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


def release_required_checks_passed(store, task_id: str) -> bool:
    verification = store.latest_for_task("verification_runs", task_id)
    if not verification:
        return False
    return not verification.get("timed_out") and verification.get("exit_code") in (0, "0")


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
    expected_tag = f"v{version.lstrip('v')}" if version else ""
    lowered = approval.lower()
    if expected_tag and expected_tag.lower() not in lowered:
        raise ValueError(f"approval must mention {expected_tag}")
    if not all(word in lowered for word in ("tag", "push", "release")):
        raise ValueError("approval must explicitly mention tag, push, and release")
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
    store.update(
        "recipe_runs",
        run["id"],
        {
            "status": "completed",
            "current_step": "complete",
            "completed_steps": RELEASE_STEPS,
            "pending_steps": [],
            "pending_public_operations": [],
            "metadata": metadata,
            "updated_at": now_iso(),
        },
    )
    return store.get("recipe_runs", run["id"])


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
        "current_step": run["current_step"],
        "next_step": next_step_for_recipe_run(run),
        "completed_steps": run.get("completed_steps") or [],
        "pending_steps": run.get("pending_steps") or [],
        "pending_public_operations": run.get("pending_public_operations") or [],
    }
    if run["status"] == "waiting_public_approval":
        context["approval_prompt"] = public_approval_prompt(run)
    return context


def next_step_for_recipe_run(run: dict[str, Any]) -> str:
    if run.get("status") == "waiting_public_approval":
        return "await_public_operation_confirmation"
    pending = run.get("pending_steps") or []
    return pending[0] if pending else "complete"


def public_approval_prompt(run: dict[str, Any]) -> str:
    version = release_version_from_run(run)
    text = PUBLIC_OPERATION_APPROVAL_TEXT.format(version=version.lstrip("v") if version else "<target_version>")
    return f"To proceed, explicitly say: {text}"


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
