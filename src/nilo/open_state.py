from __future__ import annotations

from typing import Any

from .state_audit import audit_project
from .store import Store


def open_state_detector(store: Store, project_id: str, *, verbose: bool = False) -> dict[str, Any]:
    task_ids = [task["id"] for task in store.list_where("tasks", "project_id=?", (project_id,))]
    task_id_set = set(task_ids)
    accepted_commitments = store.list_where("roadmap_commitments", "project_id=? AND status='accepted'", (project_id,))
    pending_revisions = store.list_where("roadmap_revisions", "project_id=? AND status='pending'", (project_id,))
    open_failures = store.list_where("failure_logs", "project_id=? AND status='open'", (project_id,))
    unresolved_findings = [
        finding for finding in store.list_where("review_findings", "status='unresolved'") if finding["task_id"] in task_id_set
    ]
    evidence_issues = [
        evidence
        for evidence in store.list_where("evidence_checks", "status IN ('needs_human_review', 'evidence_missing')")
        if evidence["task_id"] in task_id_set
    ]
    review_dispatches = store.list_where(
        "review_dispatches",
        "project_id=? AND status NOT IN ('review_completed', 'review_failed')",
        (project_id,),
    )
    overdrive_runs = store.list_where(
        "overdrive_runs",
        "project_id=? AND status NOT IN ('completed', 'closed', 'cancelled')",
        (project_id,),
    )
    audit_findings = audit_project(store, project_id)
    invalid_completion_findings = [
        item
        for item in audit_findings
        if item["code"].startswith("completion_") and item["severity"] == "error"
    ]
    data: dict[str, Any] = {
        "roadmap_commitments": len(accepted_commitments),
        "pending_roadmap_revisions": len(pending_revisions),
        "failures": len(open_failures),
        "review_findings": len(unresolved_findings),
        "evidence_issues": len(evidence_issues),
        "invalid_completions": len(invalid_completion_findings),
        "review_dispatches": len(review_dispatches),
        "overdrive_runs": len(overdrive_runs),
    }
    if verbose:
        data["details"] = {
            "roadmap_commitments": [{"id": item["id"], "title": item["title"]} for item in accepted_commitments],
            "pending_roadmap_revisions": [{"id": item["id"], "proposed_commitment_id": item["proposed_commitment_id"]} for item in pending_revisions],
            "failures": [{"id": item["id"], "task_id": item["task_id"], "severity": item["severity"], "category": item["category"]} for item in open_failures],
            "review_findings": [{"id": item["id"], "task_id": item["task_id"], "severity": item["severity"], "title": item["title"]} for item in unresolved_findings],
            "evidence_issues": [{"id": item["id"], "task_id": item["task_id"], "status": item["status"]} for item in evidence_issues],
            "invalid_completions": invalid_completion_findings,
            "review_dispatches": [{"id": item["id"], "task_id": item["task_id"], "reviewer": item["reviewer"], "status": item["status"]} for item in review_dispatches],
            "overdrive_runs": [{"id": item["id"], "status": item["status"], "roadmap_commitment_id": item["roadmap_commitment_id"]} for item in overdrive_runs],
        }
    return data
