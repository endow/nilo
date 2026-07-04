from __future__ import annotations

from .human_status import human_next_action_text, human_task_status
from .store import Store


class ProjectNotFoundError(ValueError):
    pass


def build_project_status(store: Store, project_id: str) -> dict:
    from . import project_logic as p

    project = store.get("projects", project_id)
    if not project:
        raise ProjectNotFoundError(f"project not found: {project_id}")
    tasks, statuses = p.project_tasks_and_statuses(store, project_id)
    return project_status_from_inputs(store, project, tasks, statuses)


def project_status_from_inputs(store: Store, project: dict, tasks: list[dict], statuses: dict[str, str]) -> dict:
    from . import project_logic as p
    from . import verification_summary as verification_render
    from .workflow_context import workflow_context as build_workflow_context

    project_id = project["id"]
    active_tasks, commitments = p.roadmap_prioritized_project_active_tasks(store, project_id, tasks, statuses)
    active_summaries = []
    unexecuted = []
    design_residue = p.project_design_residue()
    closed_commitments = p.closed_roadmap_commitments(store, project_id)
    pending_revisions = p.pending_roadmap_revision_summaries(store, project_id)
    for task in active_tasks:
        status = statuses[task["id"]]
        verification_run = store.latest_for_task("verification_runs", task["id"])
        pending_review = p.latest_pending_review_request(store, task["id"])
        blocking_findings = p.unresolved_blocking_review_findings(store, task["id"])
        recipe_provenance = p.recipe_provenance_summary(store, task["id"])
        working_tree_state = verification_render.verification_working_tree_state(verification_run)
        active_summaries.append(
            {
                "id": task["id"],
                "title": task["title"],
                "status": status,
                "human_status": human_task_status(status, task, {"verification_run": verification_run}),
                "task_type": task["task_type"],
                "risk_level": task["risk_level"],
                "latest_verification_run": verification_render.verification_summary(verification_run),
                "verification_working_tree": verification_render.verification_working_tree_summary(verification_run),
                "verification_working_tree_dirty": working_tree_state["dirty"],
                "verification_working_tree_available": working_tree_state["available"],
                "verification_working_tree_files": working_tree_state["files"],
                "verification_snapshot_policy": verification_render.verification_snapshot_policy_summary(verification_run),
                "pending_review_request": pending_review["id"] if pending_review else "",
                "pending_review_reviewer": pending_review["reviewer"] if pending_review else "",
                "pending_review_status": pending_review["status"] if pending_review else "",
                "unresolved_blocking_review_findings": [finding["id"] for finding in blocking_findings],
                "recipe_provenance": recipe_provenance,
            }
        )
        for item in p.unexecuted_verifications_for_task(status, verification_run):
            unexecuted.append({"task_id": task["id"], "issue": item})
    agent_state = p.roadmap_agent_state(store, project_id, tasks, statuses)
    workflow = build_workflow_context(store, project_id)
    base_next_actions = p.project_level_next_actions(
        store,
        tasks,
        statuses,
        design_residue,
        commitments,
        pending_revisions,
        project_id,
    )
    next_actions = base_next_actions
    if workflow.get("type") == "recipe_run":
        if workflow.get("status") == "waiting_public_approval":
            operations = ", ".join(f"{item['operation']}:{item['target']}" for item in workflow.get("pending_public_operations") or [])
            action = f"release recipe waiting for explicit public operation approval: {operations}"
            if workflow.get("public_execution_command"):
                action += f"; after approval run: {workflow['public_execution_command']}"
            next_actions = [action]
        elif workflow.get("status") == "paused_for_fix" and workflow.get("failed_verification_id"):
            next_actions = [f"release recipe blocked by failed verification; create a separate bugfix task, then resume with: {workflow.get('resume_command', '')}"]
        else:
            next_actions = [f"continue active {workflow.get('recipe_name')} recipe step: {workflow.get('next_step')}"]
    elif not active_tasks:
        next_actions = p.no_active_task_next_actions(store, project_id, base_next_actions)
    return {
        "project_id": project_id,
        "project_name": project["name"],
        "roadmap_position": p.project_roadmap_position(tasks, statuses, design_residue, commitments),
        "roadmap_commitments": commitments,
        "closed_roadmap_commitments": closed_commitments,
        "pending_roadmap_revisions": pending_revisions,
        "roadmap_assessments": p.roadmap_assessments(store, project_id, tasks, statuses),
        "roadmap_agent_state": agent_state,
        "workflow_context": workflow,
        "roadmap_agent_next_actions": p.roadmap_agent_next_actions(store, project_id, agent_state),
        "work_state": p.project_work_state(tasks, statuses),
        "current_phase": p.project_current_phase(tasks, statuses),
        "next_actions": next_actions,
        "human_next_actions": [human_next_action_text(action) for action in next_actions],
        "todo_status_counts": p.todo_status_counts(store, project_id),
        "task_status_counts": p.task_status_counts(tasks, statuses),
        "recent_history": p.recent_project_history(store, tasks),
        "active_tasks": active_summaries,
        "unexecuted_verifications": unexecuted,
        "commit_mapping": p.project_commit_mapping(store, tasks),
        "design_residue": design_residue,
    }


def project_status_view(summary: dict, project_boundary: dict) -> dict:
    return {
        "project_id": summary["project_id"],
        "project_name": summary["project_name"],
        "project_boundary": project_boundary,
        "roadmap_position": summary["roadmap_position"],
        "work_state": summary["work_state"],
        "human_work_state": summary["work_state"],
        "current_phase": summary["current_phase"],
        "roadmap_agent_state": summary["roadmap_agent_state"],
        "workflow_context": summary.get("workflow_context", {"type": "project", "status": "no_active_recipe"}),
        "roadmap_agent_next_actions": summary["roadmap_agent_next_actions"],
        "active_tasks": summary["active_tasks"],
        "next_actions": summary["next_actions"],
        "human_next_actions": summary["human_next_actions"],
        "unexecuted_verifications": summary["unexecuted_verifications"],
    }
