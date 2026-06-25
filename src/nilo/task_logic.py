from __future__ import annotations

from .snapshot import current_git_snapshot, evidence_status
from pathlib import Path


COMPLETED_STATUSES = {"completed_by_user", "completed_by_ai"}


def outcome_status(decision: str) -> str:
    return {
        "accepted": "accepted_by_user",
        "rejected": "rejected_by_user",
        "accepted_with_concerns": "accepted_with_concerns",
        "partial_accept": "reviewed_by_user",
        "rework_required": "rework_required",
    }.get(decision, "reviewed_by_user")


def projected_task_status(store, task: dict) -> str:
    latest = store.latest_task_status_event(task["id"])
    if not latest:
        return task["status"]
    return latest["status"]


def is_task_completed_status(status: str) -> bool:
    return status in COMPLETED_STATUSES


def completion_status(actor: str) -> str:
    return "completed_by_ai" if actor == "ai" else "completed_by_user"


def ai_completion_has_evidence(store, task_id: str) -> bool:
    verification_run = store.latest_for_task("verification_runs", task_id)
    return evidence_status(verification_run, current_git_snapshot(Path.cwd())) == "current"


def unresolved_blocking_review_findings(store, task_id: str) -> list[dict]:
    return store.list_where(
        "review_findings",
        "task_id=? AND status='unresolved' AND blocking=1",
        (task_id,),
    )


def unresolved_review_findings(store, task_id: str) -> list[dict]:
    return store.list_where("review_findings", "task_id=? AND status='unresolved'", (task_id,))


def require_ai_completion_evidence(store, task_id: str) -> None:
    unresolved = unresolved_review_findings(store, task_id)
    if unresolved:
        ids = ", ".join(item["id"] for item in unresolved[:5])
        raise SystemExit(f"AI completion blocked by unresolved review findings: {ids}")
    if ai_completion_has_evidence(store, task_id):
        return
    raise SystemExit(
        "AI completion requires a current verification run with exit_code=0 "
        "for the current git snapshot"
    )


def split_task_specs(task: dict) -> list[tuple[str, str]]:
    title = task["title"]
    if task["task_type"] in ("research", "design", "review", "verification"):
        return [(task["task_type"], title)]
    return [
        ("research", f"{title} の現状と影響範囲を調査する"),
        ("design", f"{title} の実装方針と完了条件を整理する"),
        ("implementation", title),
        ("verification", f"{title} の検証結果を記録する"),
    ]
