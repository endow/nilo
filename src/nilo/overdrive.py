from __future__ import annotations

from .cli_support import make_id
from .store import Store
from .task_logic import is_task_completed_status
from .timeutil import now_iso


MODES = ("normal", "overdrive")
APPROVAL_GATES = (
    "instruction_generation",
    "task_progression",
    "evidence_acceptance",
    "roadmap_assessment",
)
SAFETY_GATES = (
    "destructive_operation",
    "secret_or_credential_access",
    "billing_or_external_publication",
    "delete_operation",
    "max_failure_exceeded",
    "out_of_scope_design_change",
    "ambiguous_specification",
    "unexpected_dirty_working_tree",
)


def validate_mode(mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode}")
    return mode


def overdrive_gate_decision(mode: str, gate: str) -> dict:
    validate_mode(mode)
    if gate in SAFETY_GATES:
        return {"gate": gate, "gate_type": "safety", "decision": "stop", "bypassed": False}
    if gate in APPROVAL_GATES and mode == "overdrive":
        return {"gate": gate, "gate_type": "approval", "decision": "bypass", "bypassed": True}
    if gate in APPROVAL_GATES:
        return {"gate": gate, "gate_type": "approval", "decision": "require_approval", "bypassed": False}
    return {"gate": gate, "gate_type": "unknown", "decision": "stop", "bypassed": False}


def task_cursor_for_commitment(store: Store, project_id: str, commitment_id: str) -> dict | None:
    from . import cli as c

    tasks, statuses = c.project_tasks_and_statuses(store, project_id)
    candidates = [
        task
        for task in tasks
        if task.get("roadmap_commitment_id") == commitment_id and not is_task_completed_status(statuses[task["id"]])
    ]
    if not candidates:
        return None
    priority = {"planned": 0, "instruction_generated": 1, "agent_reported": 2, "verification_failed": 3}
    return sorted(candidates, key=lambda task: (priority.get(statuses[task["id"]], 50), task["created_at"], task["id"]))[0]


def accepted_commitment_for_project(store: Store, project_id: str) -> dict:
    commitments = store.list_where("roadmap_commitments", "project_id=? AND status='accepted'", (project_id,))
    if not commitments:
        raise SystemExit(f"accepted roadmap commitment not found for project: {project_id}")
    if len(commitments) > 1:
        ids = ", ".join(commitment["id"] for commitment in commitments[:5])
        raise SystemExit(f"multiple accepted commitments; pass --commitment explicitly: {ids}")
    return commitments[0]


def append_overdrive_event(
    store: Store,
    run: dict,
    event_type: str,
    message: str,
    metadata: dict | None = None,
    task_id: str = "",
) -> dict:
    event = {
        "id": make_id("overdrive_event"),
        "run_id": run["id"],
        "project_id": run["project_id"],
        "task_id": task_id,
        "event_type": event_type,
        "message": message,
        "metadata": metadata or {},
        "created_at": now_iso(),
    }
    store.insert("overdrive_events", event)
    return event


def start_overdrive_run(
    store: Store,
    project_id: str,
    commitment_id: str = "",
    max_failures: int = 3,
) -> dict:
    if max_failures < 1:
        raise SystemExit("max failures must be at least 1")
    project = store.get("projects", project_id)
    if not project:
        raise SystemExit(f"project not found: {project_id}")
    commitment = store.get("roadmap_commitments", commitment_id) if commitment_id else accepted_commitment_for_project(store, project_id)
    if not commitment or commitment["project_id"] != project_id or commitment["status"] != "accepted":
        raise SystemExit(f"accepted roadmap commitment not found: {commitment_id}")
    cursor = task_cursor_for_commitment(store, project_id, commitment["id"])
    created_at = now_iso()
    run = {
        "id": make_id("overdrive"),
        "project_id": project_id,
        "roadmap_commitment_id": commitment["id"],
        "mode": "overdrive",
        "status": "ready" if cursor else "awaiting_human_review",
        "cursor_task_id": cursor["id"] if cursor else "",
        "max_failures": max_failures,
        "failure_count": 0,
        "summary_json": {
            "executed_tasks": [],
            "changed_files": [],
            "commands": [],
            "test_results": [],
            "failures": [],
            "unresolved_concerns": [],
            "human_review_points": [
                "Review the final Overdrive report before closing the roadmap commitment.",
            ],
        },
        "created_at": created_at,
        "updated_at": created_at,
    }
    store.insert("overdrive_runs", run)
    append_overdrive_event(
        store,
        run,
        "mode_selected",
        "Overdrive Mode selected; approval gates are bypassable but safety gates remain enforced.",
        {"mode": "overdrive", "max_failures": max_failures},
    )
    for gate in APPROVAL_GATES:
        append_overdrive_event(store, run, "approval_gate_bypassed", f"approval gate bypassed: {gate}", overdrive_gate_decision("overdrive", gate))
    for gate in SAFETY_GATES:
        append_overdrive_event(store, run, "safety_gate_retained", f"safety gate retained: {gate}", overdrive_gate_decision("overdrive", gate))
    if cursor:
        append_overdrive_event(store, run, "cursor_selected", f"roadmap cursor selected task: {cursor['id']}", {"title": cursor["title"]}, cursor["id"])
    else:
        append_overdrive_event(
            store,
            run,
            "human_review_checkpoint_required",
            "no incomplete task remains; final human review checkpoint is required",
            {"commitment_id": commitment["id"]},
        )
    return run
