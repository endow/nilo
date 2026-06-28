from __future__ import annotations

from pathlib import Path
from typing import Any

from .snapshot import commit_aware_evidence_status, completion_commit_metadata, current_git_snapshot, evidence_status, review_result_status
from .store import Store
from .task_logic import active_task_completion, unresolved_review_findings


def _finding(code: str, message: str, *, severity: str = "warning", entity_type: str = "", entity_id: str = "", remediation: str = "") -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "severity": severity,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "remediation": remediation,
    }


def audit_task(
    store: Store,
    task_id: str,
    *,
    cwd: Path | None = None,
    current_snapshot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    cwd = cwd or Path.cwd()
    task = store.get("tasks", task_id)
    if not task:
        return [_finding("task_missing", f"task not found: {task_id}", severity="error", entity_type="task", entity_id=task_id)]
    findings: list[dict[str, Any]] = []
    completion = active_task_completion(store, task_id)
    if not completion:
        return findings
    snapshot = current_snapshot or current_git_snapshot(cwd)
    verification = store.latest_for_task("verification_runs", task_id)
    evidence = commit_aware_evidence_status(verification, snapshot, completion)
    unresolved = unresolved_review_findings(store, task_id)
    actor = completion.get("actor") or completion.get("completed_by") or ""
    if actor == "human":
        if not (completion.get("human_decision_note") or "").strip():
            findings.append(_finding("completion_human_decision_note_missing", "human completion has no decision note", severity="error", entity_type="task_completion", entity_id=completion["id"]))
        if not completion.get("decision_source"):
            findings.append(_finding("completion_human_decision_source_missing", "human completion has no decision source", severity="warning", entity_type="task_completion", entity_id=completion["id"]))
        if not completion.get("human_confirmed"):
            findings.append(_finding("completion_human_confirm_missing", "human completion has no human confirmation", severity="warning", entity_type="task_completion", entity_id=completion["id"]))
    if actor == "ai":
        if evidence != "current":
            findings.append(_finding("completion_ai_evidence_not_current", f"AI completion evidence is {evidence}", severity="error", entity_type="task_completion", entity_id=completion["id"]))
        if unresolved:
            findings.append(_finding("completion_ai_unresolved_review_findings", f"AI completion has {len(unresolved)} unresolved review finding(s)", severity="error", entity_type="task_completion", entity_id=completion["id"]))
        if verification and verification.get("source") == "agent_reported" and not (verification.get("metadata") or {}).get("trusted_runner"):
            findings.append(_finding("completion_agent_reported_verification_untrusted", "agent_reported verification used without trusted runner metadata", severity="error", entity_type="task_completion", entity_id=completion["id"]))
    if task.get("task_type") == "implementation" and evidence in {"missing", "stale", "failed", "timed_out"}:
        findings.append(_finding("completion_latest_verification_not_usable", f"completed task latest verification is {evidence}", severity="error", entity_type="task_completion", entity_id=completion["id"]))
    if task.get("task_type") == "implementation" and not completion.get("accepted_verification_run_ids"):
        findings.append(_finding("completion_accepted_verifications_empty", "completed implementation task has no accepted verification run ids", severity="error", entity_type="task_completion", entity_id=completion["id"]))
    latest_review = store.latest_for_task("review_results", task_id)
    if latest_review and review_result_status(latest_review, snapshot) == "stale":
        findings.append(_finding("completion_review_result_stale", "completed task accepted review result is stale", severity="warning", entity_type="task_completion", entity_id=completion["id"]))
    completed_snapshot = completion.get("completed_snapshot") or {}
    commit_metadata = completion_commit_metadata(completion)
    if commit_metadata:
        if not commit_metadata.get("commit_sha"):
            findings.append(_finding("completion_commit_metadata_missing_sha", "completion commit metadata has no commit sha", severity="error", entity_type="task_completion", entity_id=completion["id"]))
        if commit_metadata.get("verified_diff_hash") != (commit_metadata.get("pre_commit_snapshot") or {}).get("git_diff_hash"):
            findings.append(_finding("completion_commit_verified_diff_mismatch", "completion commit metadata does not match the verified dirty tree", severity="error", entity_type="task_completion", entity_id=completion["id"]))
        if evidence == "current":
            completed_snapshot = {}
    if completed_snapshot and completed_snapshot.get("git_diff_hash") != snapshot.get("git_diff_hash"):
        findings.append(_finding("completion_snapshot_changed", "completion snapshot differs from current snapshot", severity="error", entity_type="task_completion", entity_id=completion["id"]))
    high_failures = store.list_where("failure_logs", "task_id=? AND status='open' AND severity='high'", (task_id,))
    if high_failures:
        findings.append(_finding("completion_open_high_failure", f"completed task has {len(high_failures)} open high failure(s)", severity="error", entity_type="task", entity_id=task_id))
    return findings


def task_completion_invalid(
    store: Store,
    task_id: str,
    *,
    cwd: Path | None = None,
    current_snapshot: dict[str, Any] | None = None,
) -> bool:
    return any(item["severity"] == "error" for item in audit_task(store, task_id, cwd=cwd, current_snapshot=current_snapshot))


def audit_project(store: Store, project_id: str, *, cwd: Path | None = None) -> list[dict[str, Any]]:
    cwd = cwd or Path.cwd()
    findings: list[dict[str, Any]] = []
    tasks = store.list_where("tasks", "project_id=?", (project_id,))
    task_ids = {task["id"] for task in tasks}
    for task in tasks:
        findings.extend(audit_task(store, task["id"], cwd=cwd))
    for completion in store.list_where("task_completions", "COALESCE(invalidated_at, '')<>''"):
        if completion["task_id"] in task_ids:
            findings.append(_finding("completion_invalidated", "invalidated completion exists", entity_type="task_completion", entity_id=completion["id"], remediation=f"review task {completion['task_id']}"))
    for commitment in store.list_where("roadmap_commitments", "project_id=? AND status IN ('accepted', 'closed')", (project_id,)):
        related = [task for task in tasks if task.get("roadmap_commitment_id") == commitment["id"]]
        if not related:
            findings.append(_finding("roadmap_commitment_without_related_task", "accepted/closed roadmap commitment has no related task", severity="warning", entity_type="roadmap_commitment", entity_id=commitment["id"]))
        if commitment["status"] == "closed":
            if commitment.get("human_confirmed") and not commitment.get("decision_note"):
                findings.append(_finding("roadmap_force_close_note_missing", "roadmap close human decision has no decision note", severity="error", entity_type="roadmap_commitment", entity_id=commitment["id"]))
            for task in related:
                if task_completion_invalid(store, task["id"], cwd=cwd):
                    findings.append(_finding("roadmap_closed_with_invalid_task_completion", "closed roadmap commitment has invalid related task completion", severity="error", entity_type="roadmap_commitment", entity_id=commitment["id"]))
    for revision in store.list_where("roadmap_revisions", "project_id=? AND status IN ('accepted', 'rejected')", (project_id,)):
        if revision.get("decided_by") == "human" and (not revision.get("human_confirmed") or not revision.get("decision_note")):
            findings.append(_finding("roadmap_human_decision_evidence_missing", "roadmap decision is human-like but lacks confirmation or note", severity="warning", entity_type="roadmap_revision", entity_id=revision["id"]))
    for failure in store.list_where("failure_logs", "project_id=? AND status IN ('resolved', 'ignored')", (project_id,)):
        if not (failure.get("resolution_note") or "").strip():
            findings.append(_finding("failure_resolution_note_missing", "resolved/ignored failure has no resolution note", severity="warning", entity_type="failure", entity_id=failure["id"]))
        if failure["status"] == "ignored" and failure.get("resolved_by") == "ai":
            findings.append(_finding("failure_ignored_by_ai", "failure was ignored by AI", severity="error", entity_type="failure", entity_id=failure["id"]))
        if failure.get("resolved_by") == "human" and not failure.get("human_confirmed"):
            findings.append(_finding("failure_human_confirm_missing", "failure resolved by human without confirmation", severity="warning", entity_type="failure", entity_id=failure["id"]))
    for finding in store.list_where("review_findings", "1=1"):
        if finding["task_id"] not in task_ids:
            continue
        if finding["status"] == "accepted-risk":
            updates = store.list_where("review_finding_updates", "finding_id=?", (finding["id"],))
            latest = updates[0] if updates else {}
            if latest.get("actor") == "ai":
                findings.append(_finding("review_finding_accepted_risk_by_ai", "accepted-risk finding was recorded by AI", severity="error", entity_type="review_finding", entity_id=finding["id"]))
            if finding.get("blocking") and not latest.get("human_confirmed"):
                findings.append(_finding("review_blocking_accepted_risk_without_human_note", "blocking accepted-risk finding lacks human confirmation", severity="error", entity_type="review_finding", entity_id=finding["id"]))
    for request in store.list_where("review_requests", "task_id IN (%s)" % ",".join("?" for _ in task_ids), tuple(task_ids)) if task_ids else []:
        results = store.list_where("review_results", "review_request_id=?", (request["id"],))
        import_events = store.list_where("transition_events", "transition='import_review_result' AND entity_id=?", (request["id"],))
        if results and not import_events:
            findings.append(_finding("review_result_import_without_transition", "review result exists without transition import audit event", severity="warning", entity_type="review_request", entity_id=request["id"]))
        for event in import_events:
            if event.get("previous_state") not in {"claimed", "in_progress"}:
                findings.append(_finding("review_result_imported_without_claim", "review result was imported from a request state that was not claimed/in_progress", severity="error", entity_type="review_request", entity_id=request["id"]))
        for result in results:
            if result["reviewer"] != request["reviewer"]:
                findings.append(_finding("review_result_reviewer_mismatch", "review result reviewer differs from request reviewer", severity="error", entity_type="review_result", entity_id=result["id"]))
            if review_result_status(result, current_git_snapshot(cwd)) == "stale":
                findings.append(_finding("review_result_snapshot_stale", "review result snapshot is stale", severity="warning", entity_type="review_result", entity_id=result["id"]))
    for check in store.list_where("understanding_checks", "1=1"):
        if check["task_id"] in task_ids and check["status"] == "approved_to_implement" and not check.get("human_confirmed"):
            findings.append(_finding("understanding_approved_without_human_confirm", "understanding approved without human confirmation", severity="error", entity_type="understanding_check", entity_id=check["id"]))
    for todo in store.list_where("todos", "project_id=?", (project_id,)):
        if todo["status"] in {"rejected", "deferred", "superseded"} and not (todo.get("triage_reason") or "").strip():
            findings.append(_finding("todo_closed_without_decision_note", "todo closed without decision note", severity="warning", entity_type="todo", entity_id=todo["id"]))
        if todo["status"] == "converted_to_task" and (not todo.get("converted_task_id") or not store.get("tasks", todo.get("converted_task_id"))):
            findings.append(_finding("todo_converted_task_missing", "todo converted_to_task but converted task does not exist", severity="error", entity_type="todo", entity_id=todo["id"]))
        if todo["status"] in {"rejected", "deferred", "superseded"} and todo["kind"] in {"user_request", "discovered_issue"}:
            linked = todo.get("converted_task_id") or todo.get("roadmap_commitment_id") or todo.get("roadmap_revision_id") or todo.get("superseded_by_id")
            if not linked and not todo.get("decision_source", "").startswith("human"):
                findings.append(_finding("todo_user_request_closed_without_successor", "user/discovered todo closed without human decision or successor", severity="warning", entity_type="todo", entity_id=todo["id"]))
    return findings


def audit_workflow(store: Store, project_id: str, *, cwd: Path | None = None) -> list[dict[str, Any]]:
    cwd = cwd or Path.cwd()
    findings: list[dict[str, Any]] = []
    active_runs = store.list_where(
        "recipe_runs",
        "project_id=? AND status IN ('active', 'waiting_public_approval')",
        (project_id,),
    )
    for run in active_runs:
        if run["status"] == "waiting_public_approval" and not (run.get("pending_public_operations") or []):
            findings.append(_finding("recipe_waiting_public_without_pending_operations", "recipe run waits for public approval but has no pending operations", severity="error", entity_type="recipe_run", entity_id=run["id"]))
        tasks = store.list_where("tasks", "project_id=?", (project_id,))
        if tasks:
            from .project_logic import project_tasks_and_statuses, project_level_next_actions, project_design_residue, accepted_roadmap_commitments, pending_roadmap_revisions

            all_tasks, statuses = project_tasks_and_statuses(store, project_id)
            actions = project_level_next_actions(
                store,
                all_tasks,
                statuses,
                project_design_residue(cwd),
                accepted_roadmap_commitments(store, project_id),
                pending_roadmap_revisions(store, project_id),
                project_id,
            )
            if actions and run["task_id"] not in actions[0] and "release recipe" not in actions[0]:
                findings.append(_finding("recipe_run_active_project_next_can_leak", "active recipe run exists; project next must not lead with unrelated task state", severity="error", entity_type="recipe_run", entity_id=run["id"]))
        if run["recipe_name"] == "release":
            completion = active_task_completion(store, run["task_id"])
            metadata = run.get("metadata") or {}
            if metadata.get("commit_sha") and run["status"] != "completed" and not (run.get("pending_public_operations") or []):
                findings.append(_finding("release_commit_without_pending_or_completed_run", "release commit is recorded but recipe run is neither completed nor waiting on public operations", severity="error", entity_type="recipe_run", entity_id=run["id"]))
            if completion and not metadata.get("commit_sha"):
                findings.append(_finding("release_task_completed_without_commit_metadata", "release task is completed but release commit metadata is missing", severity="warning", entity_type="recipe_run", entity_id=run["id"]))
    completed_release_runs = store.list_where(
        "recipe_runs",
        "project_id=? AND recipe_name='release' AND status='completed'",
        (project_id,),
    )
    for run in completed_release_runs:
        snapshot = current_git_snapshot(cwd)
        if snapshot.get("working_tree_dirty"):
            findings.append(_finding("release_recipe_completed_with_dirty_tree", "release recipe is completed but working tree is dirty", severity="error", entity_type="recipe_run", entity_id=run["id"]))
        if run.get("pending_public_operations"):
            findings.append(_finding("release_recipe_completed_with_pending_public_operations", "release recipe is completed but pending public operations remain", severity="error", entity_type="recipe_run", entity_id=run["id"]))
    for completion in store.list_where("task_completions", "COALESCE(invalidated_at, '')=''"):
        metadata = completion_commit_metadata(completion)
        if not metadata:
            continue
        if not metadata.get("commit_sha"):
            findings.append(_finding("task_completion_commit_metadata_missing_sha", "task completion commit metadata has no commit sha", severity="error", entity_type="task_completion", entity_id=completion["id"]))
        if not metadata.get("committed_from_verified_dirty_tree"):
            findings.append(_finding("task_completion_commit_not_from_verified_dirty_tree", "task completion commit was not recorded from the verified dirty tree", severity="error", entity_type="task_completion", entity_id=completion["id"]))
        evidence = commit_aware_evidence_status(store.latest_for_task("verification_runs", completion["task_id"]), current_git_snapshot(cwd), completion)
        if evidence == "stale" and metadata.get("committed_from_verified_dirty_tree"):
            findings.append(_finding("commit_aware_evidence_still_stale", "commit-aware evidence is still stale despite verified dirty-tree commit metadata", severity="error", entity_type="task_completion", entity_id=completion["id"]))
    return findings


def doctor_state(store: Store, project_id: str, *, cwd: Path | None = None) -> dict[str, Any]:
    findings = [*audit_project(store, project_id, cwd=cwd), *audit_workflow(store, project_id, cwd=cwd)]
    counts: dict[str, int] = {}
    for item in findings:
        counts[item["code"]] = counts.get(item["code"], 0) + 1
    return {"project_id": project_id, "count": len(findings), "counts": counts, "findings": findings}
