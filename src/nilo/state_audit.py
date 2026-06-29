from __future__ import annotations

from pathlib import Path
from typing import Any

from .snapshot import commit_aware_evidence_status, completion_commit_metadata, current_git_snapshot, evidence_status, review_result_status
from .store import Store
from .task_logic import active_task_completion, unresolved_review_findings


VALID_TASK_STATUSES = {"planned"}
VALID_TASK_TYPES = {"research", "design", "implementation", "test_addition", "verification", "review", "refactor", "documentation"}
VALID_RISK_LEVELS = {"low", "medium", "high"}
VALID_DECISION_ACTORS = {"human", "ai"}
VALID_REVIEW_REQUEST_STATUSES = {"requested", "reviewer_unavailable", "claimed", "in_progress", "stale", "completed", "withdrawn", "superseded", "failed"}
VALID_REVIEW_VERDICTS = {"approved", "commented", "changes_requested", "rejected"}
VALID_FINDING_STATUSES = {"unresolved", "addressed", "accepted-risk"}
VALID_FINDING_SEVERITIES = {"critical", "high", "medium", "low", "info"}
VALID_FAILURE_STATUSES = {"open", "resolved", "ignored"}
VALID_TODO_STATUSES = {"open", "ready", "triaged", "blocked", "requires_roadmap", "deferred", "rejected", "superseded", "converted_to_task", "ad_hoc_approved"}
VALID_ROADMAP_STATUSES = {"proposed", "pending", "accepted", "rejected", "closed"}
VALID_UNDERSTANDING_STATUSES = {"understanding_required", "understanding_reported", "approved_to_implement"}
VALID_TRANSITION_ACTORS = {"human", "ai", "nilo", "codex", "claude-code", "transition_audit"}


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
        if evidence not in {"current", "recorded"}:
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


def _boolish_valid(value: Any) -> bool:
    return value in {0, 1, False, True}


def _project_task_ids(store: Store, project_id: str) -> tuple[list[dict[str, Any]], set[str]]:
    tasks = store.list_where("tasks", "project_id=?", (project_id,))
    return tasks, {task["id"] for task in tasks}


def _task_scoped_rows(store: Store, table: str, task_ids: set[str]) -> list[dict[str, Any]]:
    if not task_ids:
        return []
    return store.list_where(table, "task_id IN (%s)" % ",".join("?" for _ in task_ids), tuple(task_ids))


def _related_id_values(value: Any) -> set[str]:
    if isinstance(value, dict):
        values: set[str] = set()
        for item in value.values():
            values.update(_related_id_values(item))
        return values
    if isinstance(value, list):
        values = set()
        for item in value:
            values.update(_related_id_values(item))
        return values
    if isinstance(value, str):
        return {part.strip() for part in value.split(",") if part.strip()}
    return set()


def audit_schema_invariants(store: Store, project_id: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    tasks, task_ids = _project_task_ids(store, project_id)
    completions = _task_scoped_rows(store, "task_completions", task_ids)
    review_requests = _task_scoped_rows(store, "review_requests", task_ids)
    review_results = _task_scoped_rows(store, "review_results", task_ids)
    review_findings = _task_scoped_rows(store, "review_findings", task_ids)
    failures = store.list_where("failure_logs", "project_id=?", (project_id,))
    todos = store.list_where("todos", "project_id=?", (project_id,))
    roadmap_commitments = store.list_where("roadmap_commitments", "project_id=?", (project_id,))
    roadmap_revisions = store.list_where("roadmap_revisions", "project_id=?", (project_id,))
    understanding_checks = _task_scoped_rows(store, "understanding_checks", task_ids)
    transition_scope_ids = set(task_ids)
    for rows in (completions, review_requests, review_results, review_findings, failures, todos, roadmap_commitments, roadmap_revisions, understanding_checks):
        transition_scope_ids.update(row["id"] for row in rows)
    for task in tasks:
        if task["status"] not in VALID_TASK_STATUSES:
            findings.append(_finding("task_invalid_status", f"invalid task status: {task['status']}", entity_type="task", entity_id=task["id"]))
        if task["task_type"] not in VALID_TASK_TYPES:
            findings.append(_finding("task_invalid_type", f"invalid task type: {task['task_type']}", entity_type="task", entity_id=task["id"]))
        if task["risk_level"] not in VALID_RISK_LEVELS:
            findings.append(_finding("task_invalid_risk_level", f"invalid risk level: {task['risk_level']}", entity_type="task", entity_id=task["id"]))
    for completion in completions:
        actor = completion.get("actor") or completion.get("completed_by") or ""
        if actor not in VALID_DECISION_ACTORS:
            findings.append(_finding("completion_invalid_actor", f"invalid completion actor: {actor}", entity_type="task_completion", entity_id=completion["id"]))
        if not _boolish_valid(completion.get("human_confirmed")):
            findings.append(_finding("completion_invalid_human_confirmed", "completion has invalid human_confirmed value", entity_type="task_completion", entity_id=completion["id"]))
        if actor == "human" and not completion.get("human_confirmed"):
            findings.append(_finding("completion_human_without_confirmation", "human completion lacks human confirmation", entity_type="task_completion", entity_id=completion["id"]))
        events = store.list_where("transition_events", "transition='complete_task' AND entity_id=?", (completion["task_id"],))
        if not any((event.get("related_ids") or {}).get("completion") == completion["id"] for event in events):
            findings.append(_finding("completion_transition_event_missing", "task completion exists without matching transition audit event", entity_type="task_completion", entity_id=completion["id"]))
    for request in review_requests:
        if request["status"] not in VALID_REVIEW_REQUEST_STATUSES:
            findings.append(_finding("review_request_invalid_status", f"invalid review request status: {request['status']}", entity_type="review_request", entity_id=request["id"]))
    for result in review_results:
        if result["verdict"] not in VALID_REVIEW_VERDICTS:
            findings.append(_finding("review_result_invalid_verdict", f"invalid review verdict: {result['verdict']}", entity_type="review_result", entity_id=result["id"]))
    for finding in review_findings:
        if finding["status"] not in VALID_FINDING_STATUSES:
            findings.append(_finding("review_finding_invalid_status", f"invalid review finding status: {finding['status']}", entity_type="review_finding", entity_id=finding["id"]))
        if finding["severity"] not in VALID_FINDING_SEVERITIES:
            findings.append(_finding("review_finding_invalid_severity", f"invalid review finding severity: {finding['severity']}", entity_type="review_finding", entity_id=finding["id"]))
        if not _boolish_valid(finding.get("blocking")):
            findings.append(_finding("review_finding_invalid_blocking", "review finding has invalid blocking value", entity_type="review_finding", entity_id=finding["id"]))
    for failure in failures:
        if failure["status"] not in VALID_FAILURE_STATUSES:
            findings.append(_finding("failure_invalid_status", f"invalid failure status: {failure['status']}", entity_type="failure", entity_id=failure["id"]))
        if failure.get("resolved_by") and failure["status"] in {"resolved", "ignored"} and failure["resolved_by"] not in VALID_DECISION_ACTORS:
            findings.append(_finding("failure_invalid_resolved_by", f"invalid failure resolved_by: {failure['resolved_by']}", entity_type="failure", entity_id=failure["id"]))
        if not _boolish_valid(failure.get("human_confirmed")):
            findings.append(_finding("failure_invalid_human_confirmed", "failure has invalid human_confirmed value", entity_type="failure", entity_id=failure["id"]))
    for todo in todos:
        if todo["status"] not in VALID_TODO_STATUSES:
            findings.append(_finding("todo_invalid_status", f"invalid todo status: {todo['status']}", entity_type="todo", entity_id=todo["id"]))
    for commitment in roadmap_commitments:
        if commitment["status"] not in VALID_ROADMAP_STATUSES:
            findings.append(_finding("roadmap_commitment_invalid_status", f"invalid roadmap commitment status: {commitment['status']}", entity_type="roadmap_commitment", entity_id=commitment["id"]))
        if not _boolish_valid(commitment.get("human_confirmed")):
            findings.append(_finding("roadmap_commitment_invalid_human_confirmed", "roadmap commitment has invalid human_confirmed value", entity_type="roadmap_commitment", entity_id=commitment["id"]))
    for revision in roadmap_revisions:
        if revision["status"] not in VALID_ROADMAP_STATUSES:
            findings.append(_finding("roadmap_revision_invalid_status", f"invalid roadmap revision status: {revision['status']}", entity_type="roadmap_revision", entity_id=revision["id"]))
        if not _boolish_valid(revision.get("human_confirmed")):
            findings.append(_finding("roadmap_revision_invalid_human_confirmed", "roadmap revision has invalid human_confirmed value", entity_type="roadmap_revision", entity_id=revision["id"]))
    for check in understanding_checks:
        if check["status"] not in VALID_UNDERSTANDING_STATUSES:
            findings.append(_finding("understanding_invalid_status", f"invalid understanding status: {check['status']}", entity_type="understanding_check", entity_id=check["id"]))
        if not _boolish_valid(check.get("human_confirmed")):
            findings.append(_finding("understanding_invalid_human_confirmed", "understanding check has invalid human_confirmed value", entity_type="understanding_check", entity_id=check["id"]))
    for event in store.list_where("transition_events", "1=1"):
        related = event["entity_id"] in transition_scope_ids or bool(_related_id_values(event.get("related_ids")) & transition_scope_ids)
        if not related:
            continue
        if not event.get("transition") or not event.get("entity_type") or not event.get("entity_id"):
            findings.append(_finding("transition_event_empty_required_field", "transition event has empty required audit field", entity_type="transition_event", entity_id=event["id"]))
        actor = event.get("actor") or ""
        if not actor or (actor not in VALID_TRANSITION_ACTORS and not actor.startswith("reviewer:")):
            findings.append(_finding("transition_event_invalid_actor", f"invalid transition actor: {actor}", entity_type="transition_event", entity_id=event["id"]))
        if not _boolish_valid(event.get("human_confirmed")):
            findings.append(_finding("transition_event_invalid_human_confirmed", "transition event has invalid human_confirmed value", entity_type="transition_event", entity_id=event["id"]))
    return findings


def audit_project(store: Store, project_id: str, *, cwd: Path | None = None) -> list[dict[str, Any]]:
    cwd = cwd or Path.cwd()
    findings: list[dict[str, Any]] = audit_schema_invariants(store, project_id)
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
