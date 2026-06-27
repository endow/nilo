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


def projected_task_status(store, task: dict, *, current_snapshot: dict | None = None) -> str:
    latest = store.latest_task_status_event(task["id"])
    if not latest:
        return task["status"]
    if latest["source"] == "completion":
        if completion_structural_issues(store, task):
            return "completion_needs_review"
        if current_snapshot is not None:
            from .state_audit import task_completion_invalid

            if task_completion_invalid(store, task["id"], current_snapshot=current_snapshot):
                return "completion_needs_review"
    if latest["source"] == "review_finding_update" and not unresolved_review_findings(store, task["id"]):
        latest_review = store.latest_for_task("review_results", task["id"])
        if latest_review and latest_review["verdict"] == "approved":
            return "review_approved"
        return "review_commented"
    return latest["status"]


def is_task_completed_status(status: str) -> bool:
    return status in COMPLETED_STATUSES


def completion_status(actor: str) -> str:
    return "completed_by_ai" if actor == "ai" else "completed_by_user"


def active_task_completion(store, task_id: str) -> dict | None:
    completions = store.list_where(
        "task_completions",
        "task_id=? AND COALESCE(invalidated_at, '')=''",
        (task_id,),
    )
    return completions[0] if completions else None


def completion_structural_issues(store, task: dict) -> list[str]:
    completion = active_task_completion(store, task["id"])
    if not completion:
        return []
    issues: list[str] = []
    actor = completion.get("actor") or completion.get("completed_by") or ""
    if actor == "human" and not (completion.get("human_decision_note") or "").strip():
        issues.append("human_decision_note_missing")
    if actor == "ai" and unresolved_review_findings(store, task["id"]):
        issues.append("ai_unresolved_review_findings")
    if task.get("task_type") == "implementation" and not completion.get("accepted_verification_run_ids"):
        issues.append("missing_accepted_verification")
    high_failures = store.list_where(
        "failure_logs",
        "task_id=? AND status='open' AND severity='high'",
        (task["id"],),
    )
    if high_failures:
        issues.append("open_high_failure")
    return issues


def human_completion_note_is_suspicious(completion: dict) -> bool:
    if completion.get("actor") != "human":
        return False
    note = (completion.get("human_decision_note") or "").strip()
    if not note:
        return True
    lowered = note.lower()
    suspicious_fragments = [
        "human accepted",
        "verification evidence accepted",
        "daily workflow accepted",
        "accepted investigation report",
        "accepted refreshed",
        "superseded by completed",
    ]
    return any(fragment in lowered for fragment in suspicious_fragments)


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


def completion_audit_issues(store, task: dict, *, cwd: Path | None = None, current_snapshot: dict | None = None) -> list[str]:
    completion = active_task_completion(store, task["id"])
    if not completion:
        return []
    cwd = cwd or Path.cwd()
    verification_run = store.latest_for_task("verification_runs", task["id"])
    current_snapshot = current_snapshot or current_git_snapshot(cwd)
    evidence = evidence_status(verification_run, current_snapshot)
    unresolved = unresolved_review_findings(store, task["id"])
    issues: list[str] = []
    if evidence != "current":
        issues.append(f"evidence_{evidence}")
    if unresolved:
        issues.append(f"unresolved_review_findings:{len(unresolved)}")
    if human_completion_note_is_suspicious(completion):
        issues.append("suspicious_human_completion")
    accepted_verifications = completion.get("accepted_verification_run_ids") or []
    if task.get("task_type") == "implementation" and not accepted_verifications:
        issues.append("missing_accepted_verification")
    completed_snapshot = completion.get("completed_snapshot") or {}
    if completed_snapshot and completed_snapshot.get("git_diff_hash") != current_snapshot.get("git_diff_hash"):
        issues.append("completion_snapshot_changed")
    return issues


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
