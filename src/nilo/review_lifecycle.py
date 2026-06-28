from __future__ import annotations

from typing import Any

from .store import Store
from .timeutil import now_iso


def insert_review_request(store: Store, row: dict[str, Any]) -> dict:
    store.insert("review_requests", row)
    return row


def update_review_request(store: Store, review_id: str, values: dict[str, Any]) -> dict:
    store.update("review_requests", review_id, values)
    return store.get("review_requests", review_id)


def set_review_request_status(store: Store, review_id: str, status: str, **values: Any) -> dict:
    payload = {"status": status, "updated_at": values.pop("updated_at", now_iso()), **values}
    return update_review_request(store, review_id, payload)
