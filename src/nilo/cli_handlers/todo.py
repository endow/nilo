from __future__ import annotations

import argparse
import io
from contextlib import redirect_stdout

from ..cli_support import make_id
from ..store import Store
from ..timeutil import now_iso
from .task import cmd_task_create


TODO_KINDS = ["user_request", "discovered_issue", "follow_up", "cleanup", "question", "roadmap_candidate"]
TODO_STATUSES = [
    "open",
    "triaged",
    "ready",
    "ad_hoc_approved",
    "requires_roadmap",
    "blocked",
    "converted_to_task",
    "deferred",
    "rejected",
    "superseded",
]
TODO_PRIORITIES = ["low", "normal", "high"]
TRIAGE_TODO_STATUSES = {"triaged", "ready", "ad_hoc_approved", "requires_roadmap", "blocked", "deferred", "rejected"}
STARTABLE_TODO_STATUSES = {"ready", "ad_hoc_approved"}
PROMOTABLE_TODO_STATUSES = {"requires_roadmap"}


def _require_project(store: Store, project_id: str) -> None:
    if not store.get("projects", project_id):
        raise SystemExit(f"project not found: {project_id}")


def _require_accepted_commitment(store: Store, commitment_id: str, project_id: str) -> dict:
    commitment = store.get("roadmap_commitments", commitment_id)
    if not commitment or commitment["project_id"] != project_id or commitment["status"] != "accepted":
        raise SystemExit(f"accepted roadmap commitment not found: {commitment_id}")
    return commitment


def _print_todo(todo: dict) -> None:
    print(f"id: {todo['id']}")
    print(f"project_id: {todo['project_id']}")
    print(f"title: {todo['title']}")
    print(f"kind: {todo['kind']}")
    print(f"status: {todo['status']}")
    print(f"priority: {todo['priority']}")
    if todo["description"]:
        print("description:")
        print(todo["description"])
    if todo["acceptance_hint"]:
        print("acceptance_hint:")
        print(todo["acceptance_hint"])
    if todo["source_type"]:
        print(f"source_type: {todo['source_type']}")
    if todo["source_task_id"]:
        print(f"source_task_id: {todo['source_task_id']}")
    if todo["roadmap_commitment_id"]:
        print(f"roadmap_commitment_id: {todo['roadmap_commitment_id']}")
    if todo["roadmap_revision_id"]:
        print(f"roadmap_revision_id: {todo['roadmap_revision_id']}")
    if todo["converted_task_id"]:
        print(f"converted_task_id: {todo['converted_task_id']}")
    if todo["triaged_at"]:
        print(f"triaged_at: {todo['triaged_at']}")
    if todo["triage_reason"]:
        print("triage_reason:")
        print(todo["triage_reason"])
    print(f"created_at: {todo['created_at']}")


def cmd_todo_add(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        _require_project(store, args.project)
        created_at = now_iso()
        row = {
            "id": args.id or make_id("todo"),
            "project_id": args.project,
            "title": args.title,
            "kind": args.kind,
            "status": "open",
            "description": "\n".join(args.description or []),
            "acceptance_hint": args.acceptance_hint or "",
            "priority": args.priority,
            "source_type": args.source_type or "",
            "source_task_id": args.source_task or "",
            "roadmap_commitment_id": "",
            "roadmap_revision_id": "",
            "converted_task_id": "",
            "created_at": created_at,
            "triaged_at": "",
            "triage_reason": "",
        }
        store.insert("todos", row)
        print(f"todo: {row['id']}")
        print("status: open")
    finally:
        store.close()


def cmd_todo_list(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        _require_project(store, args.project)
        where = "project_id=?"
        values: tuple[str, ...] = (args.project,)
        if args.status:
            where += " AND status=?"
            values = (args.project, args.status)
        todos = store.list_where("todos", where, values)
        for todo in reversed(todos):
            print("\t".join([todo["id"], todo["status"], todo["kind"], todo["priority"], todo["title"], todo["created_at"]]))
    finally:
        store.close()


def cmd_todo_show(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        todo = store.get("todos", args.item)
        if not todo:
            raise SystemExit(f"todo not found: {args.item}")
        _print_todo(todo)
    finally:
        store.close()


def cmd_todo_triage(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        todo = store.get("todos", args.item)
        if not todo:
            raise SystemExit(f"todo not found: {args.item}")
        if args.status not in TRIAGE_TODO_STATUSES:
            allowed = ", ".join(sorted(TRIAGE_TODO_STATUSES))
            raise SystemExit(f"todo status is not triage-settable: {args.status} (allowed: {allowed})")
        values = {
            "status": args.status,
            "triaged_at": now_iso(),
            "triage_reason": args.reason,
        }
        if args.commitment:
            values["roadmap_commitment_id"] = args.commitment
        if args.roadmap_revision:
            values["roadmap_revision_id"] = args.roadmap_revision
        store.update("todos", args.item, values)
        print(f"todo: {args.item}")
        print(f"status: {args.status}")
        if args.commitment:
            print(f"roadmap_commitment_id: {args.commitment}")
    finally:
        store.close()


def cmd_todo_start(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        todo = store.get("todos", args.item)
        if not todo:
            raise SystemExit(f"todo not found: {args.item}")
        if todo["status"] not in STARTABLE_TODO_STATUSES:
            allowed = ", ".join(sorted(STARTABLE_TODO_STATUSES))
            raise SystemExit(f"todo is not startable: {todo['status']} (allowed: {allowed})")
        commitment_id = todo["roadmap_commitment_id"]
        task_id = make_id("task")
        title = args.title or todo["title"]
        description = todo["description"]
        acceptance = [todo["acceptance_hint"]] if todo["acceptance_hint"] else []
    finally:
        store.close()

    create_args = argparse.Namespace(
        db=args.db,
        project=todo["project_id"],
        title=title,
        description=[description] if description else [],
        acceptance=acceptance,
        id=task_id,
        parent_task=None,
        split_index=None,
        commitment=commitment_id,
        roadmap_item="",
        model="",
        degradation="normal",
        mode="normal",
        task_type=args.task_type,
        risk=args.risk,
        requires_understanding_check=False,
    )
    with redirect_stdout(io.StringIO()):
        cmd_task_create(create_args)

    store = Store(args.db)
    try:
        store.update(
            "todos",
            args.item,
            {
                "status": "converted_to_task",
                "converted_task_id": task_id,
                "triaged_at": now_iso(),
                "triage_reason": f"converted to task {task_id}",
            },
        )
    finally:
        store.close()
    print(f"todo: {args.item}")
    print("status: converted_to_task")
    print(f"task: {task_id}")
    print(f"next: nilo next --task {task_id}")
    print(f"instruct: nilo instruct --task {task_id}")


def _roadmap_proposal_from_todo(todo: dict, title: str) -> str:
    description = todo["description"] or todo["title"]
    acceptance = todo["acceptance_hint"] or "Human-defined success criteria are required before autonomous execution."
    return "\n".join(
        [
            f"# {title}",
            "",
            "## Intent",
            description,
            "",
            "## Success Criteria",
            f"- {acceptance}",
            "",
            "## Non Goals",
            "- This proposal does not accept or close the roadmap commitment.",
            "",
            "## Autonomy Scope",
            "- Create concrete tasks only after this proposal is accepted.",
            "",
            "## Review Gates",
            "- Human acceptance is required before implementation tasks are created.",
            "",
            "## Evidence Policy",
            "- Record verification commands and results on each task created from the accepted commitment.",
            "",
        ]
    )


def cmd_todo_promote(args: argparse.Namespace) -> None:
    if args.to != "roadmap-proposal":
        raise SystemExit(f"unsupported promotion target: {args.to}")
    store = Store(args.db)
    try:
        todo = store.get("todos", args.item)
        if not todo:
            raise SystemExit(f"todo not found: {args.item}")
        if todo["status"] not in PROMOTABLE_TODO_STATUSES:
            allowed = ", ".join(sorted(PROMOTABLE_TODO_STATUSES))
            raise SystemExit(f"todo is not promotable: {todo['status']} (allowed: {allowed})")
        project = store.get("projects", todo["project_id"])
        if not project:
            raise SystemExit(f"project not found: {todo['project_id']}")
        created_at = now_iso()
        title = args.title or todo["title"]
        body = _roadmap_proposal_from_todo(todo, title)
        commitment_id = make_id("commitment")
        revision_id = make_id("roadmap_rev")
        store.insert(
            "roadmap_commitments",
            {
                "id": commitment_id,
                "project_id": project["id"],
                "title": title,
                "intent": todo["description"] or todo["title"],
                "success_criteria": [todo["acceptance_hint"]] if todo["acceptance_hint"] else [],
                "non_goals": ["This proposal does not accept or close the roadmap commitment."],
                "autonomy_scope": ["Create concrete tasks only after this proposal is accepted."],
                "review_gates": ["Human acceptance is required before implementation tasks are created."],
                "evidence_policy": ["Record verification commands and results on each task created from the accepted commitment."],
                "status": "pending",
                "accepted_by": "",
                "accepted_at": "",
                "created_at": created_at,
            },
        )
        store.insert(
            "roadmap_revisions",
            {
                "id": revision_id,
                "project_id": project["id"],
                "proposed_commitment_id": commitment_id,
                "status": "pending",
                "body_md": body,
                "source_path": f"todo:{todo['id']}",
                "reason": args.reason,
                "accepted_at": "",
                "created_at": created_at,
            },
        )
        store.update(
            "todos",
            args.item,
            {
                "status": "superseded",
                "roadmap_revision_id": revision_id,
                "triaged_at": created_at,
                "triage_reason": args.reason,
            },
        )
        print(f"todo: {args.item}")
        print("status: superseded")
        print(f"roadmap_revision: {revision_id}")
        print(f"proposed_commitment: {commitment_id}")
        print(f"next: nilo roadmap status --project {project['id']}")
    finally:
        store.close()
