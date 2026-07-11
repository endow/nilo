from __future__ import annotations

from pathlib import Path

from .cli_support import make_id
from .failure import record_failure_log
from .guard import evaluate_evidence
from .report import claimed_status, extract_changed_files
from .secret import mask_secrets
from .snapshot import compact_snapshot, current_git_snapshot
from .store import Store
from .timeutil import now_iso


def validate_agent_report(store: Store, task: dict, markdown: str, cwd: Path, evaluate_func=evaluate_evidence) -> dict:
    if not markdown.strip():
        raise ValueError("report body is empty")

    files = extract_changed_files(markdown)
    status, issues, metadata = evaluate_func(markdown, files, task["base_commit"], cwd, task.get("base_snapshot", {}))
    issues = _ignore_release_committed_file_extras(store, task["id"], issues)
    status = _status_after_issue_filter(status, issues)
    return {
        "status": "present" if status == "evidence_submitted" else "failed",
        "issues": issues,
        "changed_files": files,
        "metadata": metadata,
    }


def import_agent_report(store: Store, task: dict, markdown: str, agent: str, cwd: Path, evaluate_func=evaluate_evidence) -> dict:
    if not markdown.strip():
        raise ValueError("report body is empty")

    files = extract_changed_files(markdown)
    created_at = now_iso()
    report = {
        "id": make_id("report"),
        "task_id": task["id"],
        "agent": agent,
        "claimed_status": claimed_status(markdown),
        "changed_files": files,
        "body_md": mask_secrets(markdown),
        "created_at": created_at,
    }
    store.insert("agent_reports", report)

    status, issues, metadata = evaluate_func(markdown, files, task["base_commit"], cwd, task.get("base_snapshot", {}))
    issues = _ignore_release_committed_file_extras(store, task["id"], issues)
    status = _status_after_issue_filter(status, issues)
    snapshot = compact_snapshot(current_git_snapshot(cwd))
    display_status = "present" if status == "evidence_submitted" else "failed"
    check = {
        "id": "",
        "task_id": task["id"],
        "report_id": report["id"],
        "status": display_status,
        "issues": issues,
        "metadata": metadata,
        "created_at": now_iso(),
    }

    if issues:
        for issue in issues:
            if issue.startswith("secret detected"):
                category = "secret_detected"
            elif issue.startswith("changed_files") or issue.startswith("git metadata"):
                category = "metadata_mismatch"
            else:
                category = "evidence_missing"
            severity = "high" if category in ("metadata_mismatch", "secret_detected") else "medium"
            record_failure_log(
                store,
                task["project_id"],
                task["id"],
                report["id"],
                category,
                issue,
                severity,
                source="report_import",
                actor="nilo",
                related_id=report["id"],
                snapshot=snapshot,
                status="open",
            )

    return {"report": report, "evidence_status": check, "evidence_check": check}


def _status_after_issue_filter(status: str, issues: list[str]) -> str:
    if status == "needs_human_review" and not any(
        issue.startswith("changed_files") or issue.startswith("git metadata") or issue.startswith("secret detected") for issue in issues
    ):
        return "evidence_missing" if issues else "evidence_submitted"
    return status


def _ignore_release_committed_file_extras(store: Store, task_id: str, issues: list[str]) -> list[str]:
    allowed = _release_committed_files(store, task_id)
    if not allowed:
        return issues
    filtered: list[str] = []
    prefix = "changed_files contains non-local changes: "
    for issue in issues:
        if issue == "git metadata warning: base_commit is missing; committed task changes cannot be compared":
            continue
        if not issue.startswith(prefix):
            filtered.append(issue)
            continue
        listed = issue[len(prefix) :].split(". このタスクで", 1)[0]
        files = {item.strip() for item in listed.split(",") if item.strip()}
        remaining = sorted(files - allowed)
        if not remaining:
            continue
        filtered.append(issue.replace(", ".join(sorted(files)), ", ".join(remaining), 1))
    return filtered


def _release_committed_files(store: Store, task_id: str) -> set[str]:
    runs = store.list_where("recipe_runs", "task_id=? AND recipe_name='release'", (task_id,))
    if not runs:
        return set()
    metadata = runs[0].get("metadata") or {}
    if not metadata.get("commit_sha"):
        return set()
    files = metadata.get("committed_files") or metadata.get("release_prepare_managed_files") or []
    return {str(path).replace("\\", "/") for path in files}
