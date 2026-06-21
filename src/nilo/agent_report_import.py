from __future__ import annotations

from pathlib import Path

from .cli_support import make_id
from .failure import derived_rule_from_failure, matched_failure_patterns_for_task, recurrence_evidence_issues
from .guard import evaluate_evidence
from .report import claimed_status, extract_changed_files
from .secret import mask_secrets
from .store import Store
from .success_logic import record_rule_successes
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
    rule = derived_rule_from_failure(project_id, failure)
    existing = store.get("derived_rules", rule["id"])
    if existing:
        source_ids = sorted(set(existing["source_failure_ids"] + [failure["id"]]))
        store.update(
            "derived_rules",
            existing["id"],
            {
                "source_failure_ids": source_ids,
                "recurrence_count": existing["recurrence_count"] + 1,
                "last_seen_at": failure["created_at"],
                "state": "active",
            },
        )
    else:
        store.insert("derived_rules", rule)


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
    recurrence_patterns = matched_failure_patterns_for_task(store, task["id"])
    recurrence_issues = recurrence_evidence_issues(markdown, recurrence_patterns)
    if recurrence_issues:
        issues = [*issues, *recurrence_issues]
        metadata = {
            **metadata,
            "matched_failure_patterns": [pattern["id"] for pattern in recurrence_patterns],
            "recurrence_prevention_issue_count": len(recurrence_issues),
        }
        if status == "evidence_submitted":
            status = "evidence_missing"
    elif recurrence_patterns:
        metadata = {
            **metadata,
            "matched_failure_patterns": [pattern["id"] for pattern in recurrence_patterns],
            "recurrence_prevention_issue_count": 0,
        }
    check = {
        "id": make_id("evidence"),
        "task_id": task["id"],
        "report_id": report["id"],
        "status": status,
        "issues": issues,
        "metadata": metadata,
        "created_at": now_iso(),
    }
    store.insert("evidence_checks", check)

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
    else:
        record_rule_successes(store, task["id"])

    return {"report": report, "evidence_check": check}
