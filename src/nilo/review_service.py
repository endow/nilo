from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .cli_support import make_id
from .review_adapter_registry import ReviewAdapterRegistry, ReviewAdapterResolutionError
from .review_lifecycle import (
    insert_review_attempt,
    insert_review_request,
    set_review_attempt_status,
    set_review_request_status,
)
from .review_ports import (
    ReviewAdapterError,
    ReviewAdapterErrorCode,
    ReviewCancelResult,
    ReviewDispatchHandle,
    ReviewDispatchOutcome,
    ReviewDispatchRequest,
    SnapshotRef,
)
from .secret import mask_secrets
from .snapshot import compact_snapshot, current_git_snapshot
from .store import Store
from .timeutil import now_iso
from .transitions import TransitionError, import_review_result as transition_import_review_result


def _masked(value: Any) -> Any:
    if isinstance(value, str):
        return mask_secrets(value)
    if isinstance(value, Mapping):
        return {str(key): _masked(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_masked(item) for item in value]
    return value


ATTEMPT_STATUS_BY_OUTCOME = {
    "accepted": "running",
    "running": "running",
    "completed": "succeeded",
    "failed": "failed",
    "timed_out": "timed_out",
    "cancelled": "cancelled",
    "unavailable": "failed",
}

REQUEST_STATUS_BY_OUTCOME = {
    "accepted": "running",
    "running": "running",
    "completed": "completed",
    "failed": "failed",
    "timed_out": "failed",
    "cancelled": "cancelled",
    "unavailable": "reviewer_unavailable",
}


class ReviewService:
    """Core review use-cases; adapters own transport and vendor operations."""

    def __init__(self, store: Store, registry: ReviewAdapterRegistry, *, cwd: Path | None = None) -> None:
        self.store = store
        self.registry = registry
        self.cwd = cwd or Path.cwd()
        self._handles: dict[str, ReviewDispatchHandle] = {}

    def request_review(
        self,
        *,
        task_id: str,
        requester: str,
        reviewer: str,
        reason: str,
        request_id: str = "",
        snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.store.get("tasks", task_id):
            raise ValueError(f"task not found: {task_id}")
        created_at = now_iso()
        latest_event = self.store.latest_task_status_event(task_id)
        row = {
            "id": request_id or make_id("review"),
            "task_id": task_id,
            "requester": requester,
            "reviewer": reviewer,
            "status": "requested",
            "reason": reason,
            "based_on_event_id": latest_event["event_id"] if latest_event else "",
            "based_on_snapshot": dict(snapshot or compact_snapshot(current_git_snapshot(self.cwd))),
            "created_at": created_at,
            "updated_at": created_at,
        }
        insert_review_request(self.store, row)
        return row

    def dispatch_review(
        self,
        request_id: str,
        *,
        capability: str = "review",
        adapter_kind: str = "",
        prompt_path: Path | None = None,
        prompt_text: str | None = None,
        timeout_seconds: float = 120.0,
        metadata: Mapping[str, Any] | None = None,
    ) -> ReviewDispatchOutcome:
        request = self._request(request_id)
        if request["status"] in {"completed", "cancelled", "superseded", "withdrawn"}:
            raise ValueError(f"review request cannot be dispatched: {request_id} [{request['status']}]")
        try:
            adapter = self.registry.resolve(request["reviewer"], capability, kind=adapter_kind)
        except ReviewAdapterResolutionError as exc:
            outcome = ReviewDispatchOutcome(
                status="unavailable",
                error=ReviewAdapterError(ReviewAdapterErrorCode.UNAVAILABLE, str(exc)),
                diagnostics={"capability": capability, "adapter_kind": adapter_kind},
            )
            self._record_outcome(request, adapter_kind or "unresolved", outcome)
            return outcome
        dispatch_request = ReviewDispatchRequest(
            request_id=request_id,
            task_id=request["task_id"],
            reviewer=request["reviewer"],
            snapshot=SnapshotRef.from_mapping(request["based_on_snapshot"]),
            prompt_path=self._validated_prompt_path(prompt_path),
            prompt_text=prompt_text,
            timeout_seconds=timeout_seconds,
            metadata=dict(metadata or {}),
        )
        try:
            outcome = adapter.dispatch(dispatch_request)
        except Exception as exc:  # adapters must not leak vendor exceptions into core
            outcome = ReviewDispatchOutcome(
                status="failed",
                error=ReviewAdapterError(ReviewAdapterErrorCode.INTERNAL_ERROR, mask_secrets(str(exc))),
            )
        outcome = self._normalized_outcome(outcome)
        attempt = self._record_outcome(request, adapter.kind, outcome)
        handle = ReviewDispatchHandle(
            request_id=request_id,
            adapter_kind=adapter.kind,
            external_id=outcome.external_id,
            metadata={"attempt_id": attempt["id"]},
        )
        self._handles[request_id] = handle
        if outcome.status == "completed" and outcome.result_payload:
            self._complete_outcome(request, attempt["id"], outcome.result_payload)
        return outcome

    def poll_review(self, request_id: str) -> ReviewDispatchOutcome:
        request = self._request(request_id)
        handle = self._handle(request_id)
        adapter = self.registry.resolve(request["reviewer"], kind=handle.adapter_kind)
        outcome = self._normalized_outcome(adapter.poll(handle))
        if outcome.status == "completed" and outcome.result_payload:
            self._complete_outcome(request, str(handle.metadata.get("attempt_id") or ""), outcome.result_payload)
        else:
            self._update_attempt_from_outcome(handle, outcome)
        return outcome

    def import_result(
        self,
        request_id: str,
        *,
        body_md: str,
        reviewer: str | None = None,
        last_seen_event_id: str | None = None,
    ) -> dict[str, Any]:
        request = self._request(request_id)
        existing = self.store.list_where("review_results", "review_request_id=?", (request_id,))
        if existing:
            return existing[-1]
        if request["status"] in {"requested", "reviewer_unavailable", "failed", "stale"}:
            request = set_review_request_status(self.store, request_id, "running")
        try:
            transition = transition_import_review_result(
                self.store,
                request["task_id"],
                request_id,
                body_md=body_md,
                reviewer=reviewer or request["reviewer"],
                last_seen_event_id=last_seen_event_id if last_seen_event_id is not None else request["based_on_event_id"],
                cwd=self.cwd,
            )
        except TransitionError:
            raise
        return self.store.get("review_results", transition.created_ids["review_result"])

    def cancel_review(self, request_id: str) -> ReviewCancelResult:
        request = self._request(request_id)
        handle = self._handles.get(request_id)
        if handle is None:
            outcome = ReviewDispatchOutcome(status="cancelled")
            result = ReviewCancelResult(cancelled=True, outcome=outcome)
        else:
            adapter = self.registry.resolve(request["reviewer"], kind=handle.adapter_kind)
            try:
                result = adapter.cancel(handle)
            except Exception as exc:
                outcome = ReviewDispatchOutcome(
                    status="failed",
                    error=ReviewAdapterError(ReviewAdapterErrorCode.INTERNAL_ERROR, mask_secrets(str(exc))),
                )
                return ReviewCancelResult(cancelled=False, outcome=outcome)
            self._update_attempt_from_outcome(handle, result.outcome)
        if result.cancelled:
            set_review_request_status(self.store, request_id, "cancelled")
        return result

    def _request(self, request_id: str) -> dict[str, Any]:
        request = self.store.get("review_requests", request_id)
        if not request:
            raise ValueError(f"review request not found: {request_id}")
        return request

    def _record_outcome(self, request: dict[str, Any], adapter_kind: str, outcome: ReviewDispatchOutcome) -> dict[str, Any]:
        attempts = self.store.list_where("review_attempts", "review_request_id=?", (request["id"],))
        attempt_number = max((int(row["attempt_number"]) for row in attempts), default=0) + 1
        created_at = now_iso()
        error = outcome.error
        row = {
            "id": make_id("review_attempt"),
            "task_id": request["task_id"],
            "review_request_id": request["id"],
            "reviewer": request["reviewer"],
            "backend_kind": adapter_kind,
            "transport": adapter_kind,
            "status": "starting",
            "attempt_number": attempt_number,
            "idempotency_key": f"{request['id']}:{attempt_number}",
            "based_on_event_id": request["based_on_event_id"],
            "based_on_snapshot": request["based_on_snapshot"],
            "error_class": error.code.value if error else "",
            "error_code": error.code.value if error else "",
            "diagnostics": _masked(outcome.diagnostics),
            "started_at": created_at,
            "created_at": created_at,
            "updated_at": created_at,
        }
        insert_review_attempt(self.store, row)
        attempt_status = "running" if outcome.status == "completed" and outcome.result_payload else ATTEMPT_STATUS_BY_OUTCOME[outcome.status]
        set_review_attempt_status(
            self.store,
            row["id"],
            attempt_status,
            error_class=row["error_class"],
            error_code=row["error_code"],
            diagnostics=row["diagnostics"],
        )
        request_status = "running" if outcome.status == "completed" and outcome.result_payload else REQUEST_STATUS_BY_OUTCOME[outcome.status]
        set_review_request_status(self.store, request["id"], request_status)
        return self.store.get("review_attempts", row["id"])

    def _update_attempt_from_outcome(self, handle: ReviewDispatchHandle, outcome: ReviewDispatchOutcome) -> None:
        attempt_id = str(handle.metadata.get("attempt_id") or "")
        if not attempt_id:
            return
        attempt = self.store.get("review_attempts", attempt_id)
        if attempt and attempt["status"] in {"starting", "running"}:
            set_review_attempt_status(self.store, attempt_id, ATTEMPT_STATUS_BY_OUTCOME[outcome.status], diagnostics=_masked(outcome.diagnostics))
        request = self._request(handle.request_id)
        if outcome.status != "completed" or not outcome.result_payload:
            set_review_request_status(self.store, request["id"], REQUEST_STATUS_BY_OUTCOME[outcome.status])

    def _import_payload(self, request: dict[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        body_md = str(payload.get("body_md") or payload.get("body") or "")
        if not body_md:
            set_review_request_status(self.store, request["id"], "failed")
            raise ValueError("completed review adapter outcome has no result body")
        return self.import_result(request["id"], body_md=body_md, reviewer=request["reviewer"])

    def _complete_outcome(self, request: dict[str, Any], attempt_id: str, payload: Mapping[str, Any]) -> None:
        try:
            self._import_payload(request, payload)
        except Exception as exc:
            stale = isinstance(exc, TransitionError) and exc.code in {"stale_review_context", "stale_review_snapshot"}
            terminal_status = "stale" if stale else "failed"
            if attempt_id:
                set_review_attempt_status(
                    self.store,
                    attempt_id,
                    terminal_status,
                    error_class=ReviewAdapterErrorCode.INVALID_RESPONSE.value,
                    error_code=ReviewAdapterErrorCode.INVALID_RESPONSE.value,
                    diagnostics={"message": mask_secrets(str(exc))},
                )
            set_review_request_status(self.store, request["id"], terminal_status)
            raise
        if attempt_id:
            set_review_attempt_status(self.store, attempt_id, "succeeded")

    def _validated_prompt_path(self, prompt_path: Path | None) -> Path | None:
        if prompt_path is None:
            return None
        root = self.cwd.resolve()
        candidate = (root / prompt_path).resolve() if not prompt_path.is_absolute() else prompt_path.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"review prompt path is outside repository boundary: {prompt_path}")
        return candidate

    @staticmethod
    def _normalized_outcome(outcome: ReviewDispatchOutcome) -> ReviewDispatchOutcome:
        if outcome.status == "completed" and not outcome.result_payload:
            return ReviewDispatchOutcome(
                status="failed",
                external_id=outcome.external_id,
                error=ReviewAdapterError(
                    ReviewAdapterErrorCode.INVALID_RESPONSE,
                    "review adapter completed without a result payload",
                ),
                diagnostics=_masked(outcome.diagnostics),
            )
        return outcome

    def _handle(self, request_id: str) -> ReviewDispatchHandle:
        handle = self._handles.get(request_id)
        if handle:
            return handle
        attempts = self.store.list_where("review_attempts", "review_request_id=?", (request_id,))
        if not attempts:
            raise ValueError(f"review dispatch handle not found: {request_id}")
        attempt = max(attempts, key=lambda row: int(row["attempt_number"]))
        return ReviewDispatchHandle(
            request_id=request_id,
            adapter_kind=attempt["backend_kind"],
            metadata={"attempt_id": attempt["id"]},
        )
