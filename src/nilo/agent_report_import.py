from __future__ import annotations

from pathlib import Path

from .cli_support import make_id
from .guard import evaluate_evidence
from .report import claimed_status, extract_changed_files
from .secret import mask_secrets
from .store import Store
from .timeutil import now_iso


def record_failure_and_rule(store: Store, project_id: str, task_id: str, report_id: str, category: str, message: str, severity: str) -> None:
    failure = {
        "id": make_id("failure"),
        "project_id": project_id,
        "task_id": task_id,
        "report_id": report_id,
        "category": category,
        "message": message,
        "severity": severity,
        "created_at": now_iso(),
    }
    store.insert("failure_logs", failure)


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

    status, issues, metadata = evaluate_func(markdown, files, task["base_commit"], cwd)
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
            record_failure_and_rule(store, task["project_id"], task["id"], report["id"], category, issue, severity)

    return {"report": report, "evidence_status": check, "evidence_check": check}
