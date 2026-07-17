from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping

from . import work_projection as work_projection_module
from .cli_support import make_id
from .failure import deterministic_id
from .gitmeta import EMPTY_TREE_COMMIT, head_commit, task_base_snapshot
from .instruction import build_instruction
from .project_boundary import project_boundary_prompt, resolve_project_boundary
from .snapshot import current_git_snapshot
from .store import Store
from .timeutil import now_iso
from .work_projection import NextActionCode, WorkProjection


class WorkActionTaken(StrEnum):
    NO_ACTION = "no_action"
    CREATED_TASK = "created_task"
    STARTED_TASK = "started_task"
    CONTINUED_EXISTING_TASK = "continued_existing_task"
    WAITING = "waiting"
    DIAGNOSTIC = "diagnostic"


@dataclass(frozen=True)
class WorkRequest:
    project_id: str
    user_request: str | None
    actor: str
    cwd: Path
    task_id: str | None = None
    allow_task_creation: bool = True
    allow_task_start: bool = True
    format: Literal["human", "ai", "json"] = "human"
    acceptance_criteria: tuple[str, ...] = ("依頼内容が満たされている", "変更内容と検証結果が記録されている")
    task_type: str = "implementation"
    risk_level: str = "medium"
    degradation_mode: str = "normal"
    mode: str = "normal"


@dataclass(frozen=True)
class WorkOperation:
    action_taken: WorkActionTaken
    mutates: bool = False
    reason: str = ""


@dataclass(frozen=True)
class WorkResult:
    before: WorkProjection
    after: WorkProjection
    action_taken: WorkActionTaken
    task_id: str | None
    instruction: str | None
    acceptance_criteria: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    diagnostics: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_taken": self.action_taken.value,
            "task_id": self.task_id,
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "instruction": self.instruction,
            "acceptance_criteria": list(self.acceptance_criteria),
            "warnings": list(self.warnings),
            "diagnostics": dict(self.diagnostics),
        }


_WAITING_ACTIONS = {
    NextActionCode.CONFIRM_UNDERSTANDING,
    NextActionCode.RUN_VERIFICATION,
    NextActionCode.RERUN_VERIFICATION,
    NextActionCode.WAIT_FOR_REVIEW,
    NextActionCode.ACCEPT_COMPLETION,
}
_DIAGNOSTIC_ACTIONS = {
    NextActionCode.TRIAGE_TODO,
    NextActionCode.REVIEW_ROADMAP,
    NextActionCode.APPROVE_ROADMAP,
    NextActionCode.IMPORT_AGENT_REPORT,
    NextActionCode.REQUEST_REVIEW,
    NextActionCode.RESOLVE_REVIEW_FINDINGS,
    NextActionCode.REASSESS_STATE,
    NextActionCode.RESOLVE_BLOCKER,
}


def decide_work_operation(projection: WorkProjection, request: WorkRequest) -> WorkOperation:
    code = projection.next_action.code
    if code is NextActionCode.NONE:
        if request.user_request and request.allow_task_creation and not projection.active_task_id:
            return WorkOperation(WorkActionTaken.CREATED_TASK, mutates=True, reason="explicit_request_without_active_work")
        return WorkOperation(WorkActionTaken.NO_ACTION, reason="no_work_requested")
    if code is NextActionCode.CREATE_TASK:
        return WorkOperation(WorkActionTaken.DIAGNOSTIC, reason="approved_roadmap_task_creation_requires_provenance")
    if code is NextActionCode.START_TASK:
        if not request.allow_task_start:
            return WorkOperation(WorkActionTaken.WAITING, reason="task_start_requires_fresh_context")
        return WorkOperation(WorkActionTaken.STARTED_TASK, mutates=True, reason="task_ready")
    if code is NextActionCode.CONTINUE_WORK:
        return WorkOperation(WorkActionTaken.CONTINUED_EXISTING_TASK, reason="active_task")
    if code in _WAITING_ACTIONS:
        return WorkOperation(WorkActionTaken.WAITING, reason=code.value)
    if code in _DIAGNOSTIC_ACTIONS:
        return WorkOperation(WorkActionTaken.DIAGNOSTIC, reason=code.value)
    return WorkOperation(WorkActionTaken.DIAGNOSTIC, reason=f"unsupported:{code.value}")


def _task_details(store: Store, task_id: str | None) -> tuple[str | None, tuple[str, ...]]:
    if not task_id:
        return None, ()
    task = store.get("tasks", task_id)
    if not task:
        return None, ()
    instruction = store.latest_for_task("instructions", task_id)
    return (instruction.get("body_md") if instruction else None), tuple(task.get("acceptance_criteria") or ())


def _create_task(store: Store, request: WorkRequest) -> str:
    created_at = now_iso()
    title = (request.user_request or "")[:80]
    task_id = deterministic_id("task", [request.project_id, request.user_request or "", created_at])
    store.insert(
        "tasks",
        {
            "id": task_id,
            "project_id": request.project_id,
            "title": title,
            "description": request.user_request or "",
            "acceptance_criteria": list(request.acceptance_criteria),
            "parent_task_id": None,
            "split_index": None,
            "task_type": request.task_type,
            "risk_level": request.risk_level,
            "requires_understanding_check": False,
            "roadmap_commitment_id": "",
            "roadmap_item_id": "",
            "status": "planned",
            "assigned_model_profile": "",
            "degradation_mode": request.degradation_mode,
            "mode": request.mode,
            "base_commit": head_commit(request.cwd) or EMPTY_TREE_COMMIT,
            "base_snapshot": task_base_snapshot(request.cwd),
            "created_at": created_at,
        },
    )
    return task_id


def _start_task(store: Store, request: WorkRequest, task_id: str) -> None:
    if store.latest_for_task("instructions", task_id):
        return
    task = store.get("tasks", task_id)
    project = store.get("projects", request.project_id)
    if not task or not project or task.get("requires_understanding_check"):
        return
    body, report_format = build_instruction(project, task, plan=None)
    boundary = resolve_project_boundary(db_path=store.path)
    store.insert(
        "instructions",
        {
            "id": make_id("instruction"),
            "task_id": task_id,
            "applied_rule_ids": [],
            "degradation_mode": task["degradation_mode"],
            "body_md": f"{project_boundary_prompt(boundary)}\n\n{body}",
            "report_format_md": report_format,
            "created_at": now_iso(),
        },
    )


def run_work_usecase(store: Store, request: WorkRequest) -> WorkResult:
    """Run the shared projection-driven work entrypoint.

    The projection is read before any mutation, only the selected operation may
    write, and the result is projected again after a write.
    """
    if not store.get("projects", request.project_id):
        raise ValueError(f"project not found: {request.project_id}")
    snapshot = current_git_snapshot(request.cwd, mode="full")
    if request.task_id:
        task = store.get("tasks", request.task_id)
        if task and task["project_id"] != request.project_id:
            raise ValueError(
                f"task project mismatch: {request.task_id} belongs to {task['project_id']}, not {request.project_id}"
            )
        before = work_projection_module.task_work_projection(store, request.task_id, current_snapshot=snapshot)
    else:
        before = work_projection_module.project_work_projection(store, request.project_id, current_snapshot=snapshot)
    task_id = before.active_task_id or before.next_action.task_id or request.task_id
    operation = decide_work_operation(before, request)
    if operation.action_taken is WorkActionTaken.CREATED_TASK:
        with store.transaction():
            task_id = _create_task(store, request)
    elif operation.action_taken is WorkActionTaken.STARTED_TASK and task_id:
        with store.transaction():
            _start_task(store, request, task_id)
    after = before
    if operation.mutates:
        after = work_projection_module.task_work_projection(store, task_id, current_snapshot=snapshot)
    instruction, acceptance = _task_details(store, task_id)
    diagnostics = MappingProxyType({"operation_reason": operation.reason, "mutated": operation.mutates})
    return WorkResult(
        before=before,
        after=after,
        action_taken=operation.action_taken,
        task_id=task_id,
        instruction=instruction,
        acceptance_criteria=acceptance,
        warnings=before.warnings,
        diagnostics=diagnostics,
    )
