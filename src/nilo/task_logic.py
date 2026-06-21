from __future__ import annotations

from .failure import unresolved_recurrence_completion_issues


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
    if latest["source"] == "outcome":
        return outcome_status(latest["status"])
    return latest["status"]


def is_task_completed_status(status: str) -> bool:
    return status in COMPLETED_STATUSES


def completion_status(actor: str) -> str:
    return "completed_by_ai" if actor == "ai" else "completed_by_user"


def ai_completion_has_evidence(store, task_id: str) -> bool:
    check = store.latest_for_task("evidence_checks", task_id)
    if check and check["status"] == "evidence_submitted":
        return True
    verification_run = store.latest_for_task("verification_runs", task_id)
    return bool(verification_run and not verification_run["timed_out"] and verification_run["exit_code"] == 0)


def unresolved_blocking_review_findings(store, task_id: str) -> list[dict]:
    return store.list_where(
        "review_findings",
        "task_id=? AND status='unresolved' AND blocking=1",
        (task_id,),
    )


def require_ai_completion_evidence(store, task_id: str) -> None:
    blocking = unresolved_blocking_review_findings(store, task_id)
    if blocking:
        ids = ", ".join(item["id"] for item in blocking[:5])
        raise SystemExit(f"AI completion blocked by unresolved review findings: {ids}")
    recurrence_issues = unresolved_recurrence_completion_issues(store, task_id)
    if recurrence_issues:
        raise SystemExit(
            "AI completion blocked by recurrence prevention rule: "
            + "; ".join(recurrence_issues[:5])
        )
    if ai_completion_has_evidence(store, task_id):
        return
    raise SystemExit(
        "AI completion requires latest evidence_check=evidence_submitted "
        "or a recorded verification run with exit_code=0"
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
