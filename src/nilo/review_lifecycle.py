from __future__ import annotations

from typing import Any

from .store import Store
from .timeutil import now_iso


ACTIVE_REVIEW_REQUEST_STATUSES = frozenset({"requested", "running"})
NON_BLOCKING_REVIEW_REQUEST_STATUSES = frozenset({"deferred"})
TERMINAL_REVIEW_REQUEST_STATUSES = frozenset({"completed", "failed", "cancelled", "stale", "superseded", "withdrawn"})
LEGACY_ACTIVE_REVIEW_REQUEST_STATUSES = frozenset({"reviewer_unavailable", "claimed", "in_progress"})
VALID_REVIEW_REQUEST_STATUSES = (
    ACTIVE_REVIEW_REQUEST_STATUSES
    | NON_BLOCKING_REVIEW_REQUEST_STATUSES
    | TERMINAL_REVIEW_REQUEST_STATUSES
    | LEGACY_ACTIVE_REVIEW_REQUEST_STATUSES
)

ACTIVE_REVIEW_ATTEMPT_STATUSES = frozenset({"starting", "running"})
TERMINAL_REVIEW_ATTEMPT_STATUSES = frozenset(
    {"succeeded", "rate_limited", "quota_exhausted", "timed_out", "failed", "cancelled", "stale"}
)
VALID_REVIEW_ATTEMPT_STATUSES = ACTIVE_REVIEW_ATTEMPT_STATUSES | TERMINAL_REVIEW_ATTEMPT_STATUSES
REVIEW_ATTEMPT_TRANSITIONS = {
    "starting": VALID_REVIEW_ATTEMPT_STATUSES - {"starting"},
    "running": TERMINAL_REVIEW_ATTEMPT_STATUSES,
}


def review_request_is_active(status: str) -> bool:
    return status in ACTIVE_REVIEW_REQUEST_STATUSES or status in LEGACY_ACTIVE_REVIEW_REQUEST_STATUSES


def review_request_is_non_blocking(status: str) -> bool:
    return status in NON_BLOCKING_REVIEW_REQUEST_STATUSES or status in TERMINAL_REVIEW_REQUEST_STATUSES


def review_attempt_is_active(status: str) -> bool:
    return status in ACTIVE_REVIEW_ATTEMPT_STATUSES


def insert_review_request(store: Store, row: dict[str, Any]) -> dict:
    store.insert("review_requests", row)
    return row


def update_review_request(store: Store, review_id: str, values: dict[str, Any]) -> dict:
    store.update("review_requests", review_id, values)
    return store.get("review_requests", review_id)


def set_review_request_status(store: Store, review_id: str, status: str, **values: Any) -> dict:
    if status not in VALID_REVIEW_REQUEST_STATUSES:
        raise ValueError(f"invalid review request status: {status}")
    payload = {"status": status, "updated_at": values.pop("updated_at", now_iso()), **values}
    return update_review_request(store, review_id, payload)


def insert_review_attempt(store: Store, row: dict[str, Any]) -> dict:
    status = str(row.get("status") or "")
    if status not in VALID_REVIEW_ATTEMPT_STATUSES:
        raise ValueError(f"invalid review attempt status: {status}")
    store.insert("review_attempts", row)
    return row


def update_review_attempt(store: Store, attempt_id: str, values: dict[str, Any]) -> dict:
    status = values.get("status")
    if status is not None and status not in VALID_REVIEW_ATTEMPT_STATUSES:
        raise ValueError(f"invalid review attempt status: {status}")
    attempt = store.get("review_attempts", attempt_id)
    if not attempt:
        raise ValueError(f"review attempt not found: {attempt_id}")
    current_status = attempt["status"]
    if status is not None and status != current_status and status not in REVIEW_ATTEMPT_TRANSITIONS.get(current_status, frozenset()):
        raise ValueError(f"invalid review attempt transition: {current_status} -> {status}")
    store.update("review_attempts", attempt_id, values)
    return store.get("review_attempts", attempt_id)


def set_review_attempt_status(store: Store, attempt_id: str, status: str, **values: Any) -> dict:
    payload = {"status": status, "updated_at": values.pop("updated_at", now_iso()), **values}
    if status in TERMINAL_REVIEW_ATTEMPT_STATUSES and not payload.get("finished_at"):
        payload["finished_at"] = payload["updated_at"]
    return update_review_attempt(store, attempt_id, payload)
