from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from .cli_support import make_id
from .review_lifecycle import insert_review_attempt, insert_review_request, set_review_attempt_status, set_review_request_status
from .secret import mask_secrets
from .snapshot import compact_snapshot, current_git_snapshot
from .store import Store
from .timeutil import now_iso


class ErrorClass(StrEnum):
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    AUTHENTICATION = "authentication"
    CONFIGURATION = "configuration"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    INVALID_OUTPUT = "invalid_output"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ReviewContext:
    task_id: str
    review_request_id: str
    attempt_id: str
    reviewer: str
    based_on_event_id: str
    based_on_snapshot: dict[str, Any]
    cwd: Path


@dataclass(frozen=True)
class ReviewExecutionOutput:
    body: str
    diagnostics: dict[str, Any] = field(default_factory=dict)


class ReviewAdapter(Protocol):
    reviewer: str
    backend_kind: str
    transport: str

    def readiness(self, context: ReviewContext) -> bool: ...

    def execute(self, context: ReviewContext) -> ReviewExecutionOutput: ...

    def finalize(self, store: Store, context: ReviewContext, output: ReviewExecutionOutput) -> None: ...

    def cancel(self, attempt_id: str) -> None: ...


class ReviewBackendError(RuntimeError):
    def __init__(
        self,
        error_class: ErrorClass,
        message: str,
        *,
        error_code: str = "",
        retry_after: str = "",
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.error_code = error_code
        self.retry_after = retry_after
        self.diagnostics = diagnostics or {}


@dataclass(frozen=True)
class CoordinationResult:
    status: str
    review_request: dict[str, Any]
    review_attempt: dict[str, Any]
    output: ReviewExecutionOutput | None = None


@dataclass(frozen=True)
class ReviewExecutionPolicy:
    fallback_reviewers: tuple[str, ...] = ()
    max_attempts: int = 1
    retryable_error_classes: frozenset[ErrorClass] = frozenset({ErrorClass.RATE_LIMITED, ErrorClass.QUOTA_EXHAUSTED})


ATTEMPT_STATUS_BY_ERROR = {
    ErrorClass.RATE_LIMITED: "rate_limited",
    ErrorClass.QUOTA_EXHAUSTED: "quota_exhausted",
    ErrorClass.TIMEOUT: "timed_out",
}

REQUEST_STATUS_BY_ERROR = {
    ErrorClass.RATE_LIMITED: "deferred",
    ErrorClass.QUOTA_EXHAUSTED: "deferred",
    ErrorClass.TIMEOUT: "failed",
    ErrorClass.AUTHENTICATION: "failed",
    ErrorClass.CONFIGURATION: "failed",
    ErrorClass.TRANSPORT: "failed",
    ErrorClass.INVALID_OUTPUT: "failed",
    ErrorClass.UNKNOWN: "failed",
}


def _masked(value: Any) -> Any:
    if isinstance(value, str):
        return mask_secrets(value)
    if isinstance(value, dict):
        return {str(key): _masked(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_masked(item) for item in value]
    return value


def _snapshot_matches(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    return expected.get("git_head") == actual.get("git_head") and expected.get("git_diff_hash") == actual.get("git_diff_hash")


def _attempt_number(store: Store, request_id: str) -> int:
    rows = store.list_where("review_attempts", "review_request_id=?", (request_id,))
    return max((int(row["attempt_number"]) for row in rows), default=0) + 1


def coordinate_review(
    store: Store,
    *,
    task_id: str,
    requester: str,
    reason: str,
    adapter: ReviewAdapter,
    cwd: Path | None = None,
    existing_request_id: str = "",
) -> CoordinationResult:
    cwd = cwd or Path.cwd()
    task = store.get("tasks", task_id)
    if not task:
        raise ValueError(f"task not found: {task_id}")
    created_at = now_iso()
    latest_event = store.latest_task_status_event(task_id)
    request_id = existing_request_id or make_id("review")
    existing_request = store.get("review_requests", request_id) if existing_request_id else None
    if existing_request_id and not existing_request:
        raise ValueError(f"review request not found: {existing_request_id}")
    if existing_request and existing_request["status"] in {"completed", "cancelled", "stale", "superseded", "withdrawn"}:
        raise ValueError(f"review request cannot be retried: {request_id} [{existing_request['status']}]")
    snapshot = existing_request["based_on_snapshot"] if existing_request else compact_snapshot(current_git_snapshot(cwd))
    attempt_id = make_id("review_attempt")
    request = existing_request or {
        "id": request_id,
        "task_id": task_id,
        "requester": requester,
        "reviewer": adapter.reviewer,
        "status": "running",
        "reason": reason,
        "based_on_event_id": latest_event["event_id"] if latest_event else "",
        "based_on_snapshot": snapshot,
        "created_at": created_at,
        "updated_at": created_at,
    }
    attempt_number = _attempt_number(store, request_id)
    attempt = {
        "id": attempt_id,
        "task_id": task_id,
        "review_request_id": request_id,
        "reviewer": adapter.reviewer,
        "backend_kind": adapter.backend_kind,
        "transport": adapter.transport,
        "status": "starting",
        "attempt_number": attempt_number,
        "idempotency_key": f"{request_id}:{attempt_number}:{adapter.reviewer}:{snapshot.get('git_diff_hash', '')}",
        "based_on_event_id": request["based_on_event_id"],
        "based_on_snapshot": snapshot,
        "diagnostics": {},
        "started_at": created_at,
        "created_at": created_at,
        "updated_at": created_at,
    }
    with store.transaction():
        if existing_request:
            request = set_review_request_status(store, request_id, "running")
        else:
            insert_review_request(store, request)
        insert_review_attempt(store, attempt)

    context = ReviewContext(
        task_id=task_id,
        review_request_id=request_id,
        attempt_id=attempt_id,
        reviewer=adapter.reviewer,
        based_on_event_id=request["based_on_event_id"],
        based_on_snapshot=snapshot,
        cwd=cwd,
    )
    try:
        if not adapter.readiness(context):
            raise ReviewBackendError(ErrorClass.CONFIGURATION, f"reviewer is not ready: {adapter.reviewer}")
        set_review_attempt_status(store, attempt_id, "running")
        output = adapter.execute(context)
        actual_snapshot = compact_snapshot(current_git_snapshot(cwd))
        if not _snapshot_matches(snapshot, actual_snapshot):
            with store.transaction():
                set_review_attempt_status(store, attempt_id, "stale", diagnostics={"reason": "review snapshot changed"})
                set_review_request_status(store, request_id, "stale")
            return CoordinationResult("stale", store.get("review_requests", request_id), store.get("review_attempts", attempt_id))
        with store.transaction():
            adapter.finalize(store, context, output)
            set_review_attempt_status(store, attempt_id, "succeeded", diagnostics=_masked(output.diagnostics))
            set_review_request_status(store, request_id, "completed")
        return CoordinationResult("completed", store.get("review_requests", request_id), store.get("review_attempts", attempt_id), output)
    except ReviewBackendError as exc:
        attempt_status = ATTEMPT_STATUS_BY_ERROR.get(exc.error_class, "failed")
        request_status = REQUEST_STATUS_BY_ERROR[exc.error_class]
        diagnostics = _masked({"message": str(exc), **exc.diagnostics})
        with store.transaction():
            set_review_attempt_status(
                store,
                attempt_id,
                attempt_status,
                error_class=exc.error_class.value,
                error_code=mask_secrets(exc.error_code),
                retry_after=mask_secrets(exc.retry_after),
                diagnostics=diagnostics,
            )
            set_review_request_status(store, request_id, request_status)
        return CoordinationResult(request_status, store.get("review_requests", request_id), store.get("review_attempts", attempt_id))
    except Exception as exc:
        with store.transaction():
            set_review_attempt_status(
                store,
                attempt_id,
                "failed",
                error_class=ErrorClass.UNKNOWN.value,
                diagnostics={"message": mask_secrets(str(exc))},
            )
            set_review_request_status(store, request_id, "failed")
        return CoordinationResult("failed", store.get("review_requests", request_id), store.get("review_attempts", attempt_id))


def coordinate_review_with_fallback(
    store: Store,
    *,
    task_id: str,
    requester: str,
    reason: str,
    adapters: list[ReviewAdapter],
    policy: ReviewExecutionPolicy,
    cwd: Path | None = None,
) -> CoordinationResult:
    if not adapters:
        raise ValueError("at least one review adapter is required")
    reviewer_names = [adapter.reviewer for adapter in adapters]
    if len(reviewer_names) != len(set(reviewer_names)):
        raise ValueError("fallback reviewer cycle or duplicate detected")
    allowed_names = [adapters[0].reviewer, *policy.fallback_reviewers]
    if reviewer_names != allowed_names[: len(reviewer_names)]:
        raise ValueError("adapters must match the explicit fallback reviewer order")
    if policy.max_attempts < 1 or len(adapters) > policy.max_attempts:
        raise ValueError("review attempt limit exceeded")

    request_id = ""
    result: CoordinationResult | None = None
    for index, adapter in enumerate(adapters):
        result = coordinate_review(
            store,
            task_id=task_id,
            requester=requester,
            reason=reason,
            adapter=adapter,
            cwd=cwd,
            existing_request_id=request_id,
        )
        request_id = result.review_request["id"]
        if result.status == "completed":
            return result
        error_value = result.review_attempt.get("error_class") or ""
        error_class = ErrorClass(error_value) if error_value in ErrorClass._value2member_map_ else ErrorClass.UNKNOWN
        if error_class not in policy.retryable_error_classes or index + 1 >= len(adapters):
            return result
    if result is None:
        raise RuntimeError("review coordination produced no result")
    return result
