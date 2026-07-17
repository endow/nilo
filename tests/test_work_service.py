from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nilo.work_projection import (
    CompletionState,
    EvidenceState,
    NextAction,
    NextActionCode,
    ReviewState,
    WorkPhase,
    WorkProjection,
)
from nilo.work_service import WorkActionTaken, WorkRequest, decide_work_operation, run_work_usecase


def projection(code: NextActionCode, *, task_id: str | None = None) -> WorkProjection:
    return WorkProjection(
        project_id="demo",
        scope="task" if task_id else "project",
        active_task_id=task_id,
        phase=WorkPhase.WORKING if task_id else WorkPhase.IDLE,
        next_action=NextAction(code, task_id=task_id),
        blocker=None,
        evidence_state=EvidenceState.NOT_REQUIRED,
        review_state=ReviewState.NOT_REQUIRED,
        completion_state=CompletionState.NOT_READY,
    )


def request(*, user_request: str | None = "実装する", allow_creation: bool = True) -> WorkRequest:
    return WorkRequest("demo", user_request, "ai", Path.cwd(), allow_task_creation=allow_creation)


def test_continue_work_never_requests_a_mutation() -> None:
    operation = decide_work_operation(projection(NextActionCode.CONTINUE_WORK, task_id="task-1"), request())
    assert operation.action_taken is WorkActionTaken.CONTINUED_EXISTING_TASK
    assert operation.mutates is False


def test_waiting_review_does_not_create_a_task() -> None:
    operation = decide_work_operation(projection(NextActionCode.WAIT_FOR_REVIEW, task_id="task-1"), request())
    assert operation.action_taken is WorkActionTaken.WAITING
    assert operation.mutates is False


def test_ai_completion_remains_waiting() -> None:
    operation = decide_work_operation(projection(NextActionCode.ACCEPT_COMPLETION, task_id="task-1"), request())
    assert operation.action_taken is WorkActionTaken.WAITING
    assert operation.mutates is False


def test_start_can_be_gated_by_adapter_context() -> None:
    work_request = WorkRequest("demo", "続ける", "ai", Path.cwd(), allow_task_start=False)
    operation = decide_work_operation(projection(NextActionCode.START_TASK, task_id="task-1"), work_request)
    assert operation.action_taken is WorkActionTaken.WAITING
    assert operation.reason == "task_start_requires_fresh_context"


def test_creation_requires_explicit_permission_and_request() -> None:
    idle = projection(NextActionCode.NONE)
    assert decide_work_operation(idle, request()).action_taken is WorkActionTaken.CREATED_TASK
    assert decide_work_operation(idle, request(allow_creation=False)).action_taken is WorkActionTaken.NO_ACTION
    assert decide_work_operation(idle, request(user_request=None)).action_taken is WorkActionTaken.NO_ACTION


def test_explicit_task_must_belong_to_requested_project() -> None:
    store = MagicMock()
    store.get.return_value = {"id": "task-1", "project_id": "other"}
    work_request = WorkRequest("demo", "続ける", "ai", Path.cwd(), task_id="task-1")
    with (
        patch("nilo.work_service.current_git_snapshot", return_value={}),
        pytest.raises(ValueError, match="task project mismatch"),
    ):
        run_work_usecase(store, work_request)
