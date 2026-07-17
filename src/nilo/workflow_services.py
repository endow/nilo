from __future__ import annotations

from pathlib import Path
from typing import Callable

from .cli import requires_understanding_gate, understanding_approved
from .cli_support import make_id
from .gitmeta import EMPTY_TREE_COMMIT, head_commit, task_base_snapshot
from .instruction import build_instruction, build_understanding_prompt
from .project_boundary import project_boundary_prompt, resolve_project_boundary
from .store import Store
from .timeutil import now_iso


def create_instruction(
    store: Store,
    task_id: str,
    *,
    plan: bool,
    db_path: Path,
    cwd: Path | None = None,
    head_provider: Callable[[Path], str] = head_commit,
    snapshot_provider: Callable[[Path], dict] = task_base_snapshot,
) -> str:
    root = cwd or Path.cwd()
    task = store.get("tasks", task_id)
    if not task:
        raise LookupError(f"task not found: {task_id}")
    project = store.get("projects", task["project_id"])
    if not project:
        raise LookupError(f"project not found: {task['project_id']}")
    if requires_understanding_gate(task) and not understanding_approved(
        store, task["id"]
    ):
        raise PermissionError(
            "understanding check approval required before instruction generation"
        )
    if task.get("base_commit") != EMPTY_TREE_COMMIT:
        store.update(
            "tasks",
            task["id"],
            {
                "base_commit": head_provider(root),
                "base_snapshot": task.get("base_snapshot") or snapshot_provider(root),
            },
        )
        task = store.get("tasks", task["id"])
    boundary = resolve_project_boundary(db_path=db_path)
    body, report_format = build_instruction(project, task, plan=plan)
    body = f"{project_boundary_prompt(boundary)}\n\n{body}"
    store.insert(
        "instructions",
        {
            "id": make_id("instruction"),
            "task_id": task["id"],
            "applied_rule_ids": [],
            "degradation_mode": task["degradation_mode"],
            "body_md": body,
            "report_format_md": report_format,
            "created_at": now_iso(),
        },
    )
    return body


def prepare_understanding(store: Store, task_id: str) -> str:
    task = store.get("tasks", task_id)
    if not task:
        raise LookupError(f"task not found: {task_id}")
    body = build_understanding_prompt(task)
    store.insert(
        "understanding_checks",
        {
            "id": make_id("understanding"),
            "task_id": task_id,
            "status": "understanding_required",
            "body_md": body,
            "created_at": now_iso(),
        },
    )
    return body


def import_understanding(store: Store, task_id: str, body: str) -> str:
    if not store.get("tasks", task_id):
        raise LookupError(f"task not found: {task_id}")
    if not body.strip():
        raise ValueError("understanding body is empty")
    row_id = make_id("understanding")
    store.insert(
        "understanding_checks",
        {
            "id": row_id,
            "task_id": task_id,
            "status": "understanding_reported",
            "body_md": body,
            "created_at": now_iso(),
        },
    )
    return row_id
