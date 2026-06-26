from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..cli_support import make_id, read_text_or_exit
from ..roadmap_render import (
    render_human_roadmap_markdown,
    render_human_roadmap_summary_markdown,
    render_roadmap_assess_markdown,
    render_roadmap_discuss_markdown,
    render_roadmap_task_plan_markdown,
)
from ..store import Store
from ..timeutil import now_iso


def is_roadmap_discussion_context(markdown: str) -> bool:
    first_heading = next((line.strip() for line in markdown.splitlines() if line.strip().startswith("#")), "")
    return first_heading.lower() == "# roadmap discussion context"


def normalize_commitment_text(value: str) -> str:
    return " ".join(value.casefold().split())


def duplicate_commitments(store: Store, commitment: dict) -> list[dict]:
    candidates = store.list_where(
        "roadmap_commitments",
        "project_id=? AND status IN ('accepted', 'closed', 'rejected') AND id<>?",
        (commitment["project_id"], commitment["id"]),
    )
    title = normalize_commitment_text(commitment["title"])
    criteria = {normalize_commitment_text(item) for item in commitment["success_criteria"]}
    duplicates: list[dict] = []
    for candidate in candidates:
        candidate_title = normalize_commitment_text(candidate["title"])
        candidate_criteria = {normalize_commitment_text(item) for item in candidate["success_criteria"]}
        if candidate_title == title or (criteria and criteria == candidate_criteria):
            duplicates.append(candidate)
    return duplicates


def comparable_roadmap_source_path(value: str) -> str:
    if not value:
        return ""
    if value.startswith("todo:"):
        return value
    try:
        return str(Path(value).resolve()).casefold()
    except OSError:
        return value.replace("\\", "/").casefold()


def render_and_write_human_roadmap(store: Store, project: dict, output_path: str | None = None) -> Path:
    from .. import cli as c

    tasks, statuses = c.project_tasks_and_statuses(store, project["id"])
    summary = c.project_summary_data(store, project, tasks, statuses)
    language = c.handson_language()
    if language == "ja":
        summary = {
            **summary,
            "roadmap_position": c.render_handson_roadmap_position(summary["roadmap_position"], language),
            "next_actions": [c.render_handson_next_action(action, language) for action in summary["next_actions"]],
        }
    body = render_human_roadmap_markdown(summary, language)
    output = Path(output_path or c.human_roadmap_path_for_project(project["id"]))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(body, encoding="utf-8", newline="\n")
    return output


def cmd_roadmap_import(args: argparse.Namespace) -> None:
    from .. import cli as c

    store = Store(args.db)
    try:
        project = store.get("projects", args.project)
        if not project:
            raise SystemExit(f"project not found: {args.project}")
        source_path = str(Path(args.file)) if args.file else ""
        markdown = read_text_or_exit(Path(args.file)) if args.file else sys.stdin.read()
        if not markdown.strip():
            raise SystemExit("roadmap proposal body is empty")
        if is_roadmap_discussion_context(markdown):
            raise SystemExit("roadmap import rejected discussion context; use a RoadmapProposal file instead")
        proposal = c.parse_roadmap_proposal(markdown)
        if not proposal["title"]:
            raise SystemExit("roadmap import rejected malformed proposal: missing top-level # title")
        created_at = now_iso()
        commitment_id = make_id("commitment")
        revision_id = make_id("roadmap_rev")
        candidate_commitment = {
            "id": commitment_id,
            "project_id": project["id"],
            "title": proposal["title"],
            "success_criteria": proposal["success_criteria"],
        }
        duplicates = duplicate_commitments(store, candidate_commitment)
        if duplicates:
            details = ", ".join(f"{item['id']} [{item['status']}] {item['title']}" for item in duplicates)
            raise SystemExit(f"duplicate roadmap commitment detected before import: {details}")
        store.insert(
            "roadmap_commitments",
            {
                "id": commitment_id,
                "project_id": project["id"],
                "title": proposal["title"],
                "intent": proposal["intent"],
                "success_criteria": proposal["success_criteria"],
                "non_goals": proposal["non_goals"],
                "autonomy_scope": proposal["autonomy_scope"],
                "review_gates": proposal["review_gates"],
                "evidence_policy": proposal["evidence_policy"],
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
                "body_md": markdown,
                "source_path": source_path,
                "reason": "",
                "accepted_at": "",
                "created_at": created_at,
            },
        )
        print(f"roadmap_revision: {revision_id}")
        print(f"proposed_commitment: {commitment_id}")
    finally:
        store.close()


def cmd_roadmap_adopt(args: argparse.Namespace) -> None:
    from .. import cli as c

    store = Store(args.db)
    try:
        project = store.get("projects", args.project)
        if not project:
            raise SystemExit(f"project not found: {args.project}")
        source_path = str(Path(args.file))
        markdown = read_text_or_exit(Path(args.file))
        if not markdown.strip():
            raise SystemExit("roadmap proposal body is empty")
        if is_roadmap_discussion_context(markdown):
            raise SystemExit("roadmap adopt rejected discussion context; use a RoadmapProposal file instead")
        proposal = c.parse_roadmap_proposal(markdown)
        if not proposal["title"]:
            raise SystemExit("roadmap adopt rejected malformed proposal: missing top-level # title")
        created_at = now_iso()
        commitment_id = make_id("commitment")
        revision_id = make_id("roadmap_rev")
        candidate_commitment = {
            "id": commitment_id,
            "project_id": project["id"],
            "title": proposal["title"],
            "success_criteria": proposal["success_criteria"],
        }
        duplicates = duplicate_commitments(store, candidate_commitment)
        if duplicates:
            details = ", ".join(f"{item['id']} [{item['status']}] {item['title']}" for item in duplicates)
            raise SystemExit(f"duplicate roadmap commitment detected before adopt: {details}")
        store.insert(
            "roadmap_commitments",
            {
                "id": commitment_id,
                "project_id": project["id"],
                "title": proposal["title"],
                "intent": proposal["intent"],
                "success_criteria": proposal["success_criteria"],
                "non_goals": proposal["non_goals"],
                "autonomy_scope": proposal["autonomy_scope"],
                "review_gates": proposal["review_gates"],
                "evidence_policy": proposal["evidence_policy"],
                "status": "accepted",
                "accepted_by": args.actor,
                "accepted_at": created_at,
                "created_at": created_at,
            },
        )
        store.insert(
            "roadmap_revisions",
            {
                "id": revision_id,
                "project_id": project["id"],
                "proposed_commitment_id": commitment_id,
                "status": "accepted",
                "body_md": markdown,
                "source_path": source_path,
                "reason": args.reason,
                "decided_by": args.actor,
                "accepted_at": created_at,
                "created_at": created_at,
            },
        )
        output = render_and_write_human_roadmap(store, project, args.roadmap_file)
        print(f"accepted_revision: {revision_id}")
        print(f"accepted_commitment: {commitment_id}")
        print(f"accepted_by: {args.actor}")
        print(f"written: {output}")
    finally:
        store.close()


def cmd_roadmap_accept(args: argparse.Namespace) -> None:
    from .. import cli as c

    store = Store(args.db)
    try:
        revision = store.get("roadmap_revisions", args.revision)
        if not revision:
            raise SystemExit(f"roadmap revision not found: {args.revision}")
        if revision["status"] != "pending":
            raise SystemExit(f"roadmap revision is not pending: {args.revision}")
        commitment = store.get("roadmap_commitments", revision["proposed_commitment_id"])
        if not commitment:
            raise SystemExit(f"roadmap commitment not found: {revision['proposed_commitment_id']}")
        duplicates = duplicate_commitments(store, commitment)
        if duplicates:
            details = ", ".join(f"{item['id']} [{item['status']}] {item['title']}" for item in duplicates)
            raise SystemExit(f"duplicate roadmap commitment detected before accept: {details}")
        accepted_at = now_iso()
        store.update(
            "roadmap_revisions",
            revision["id"],
            {"status": "accepted", "reason": args.reason, "decided_by": args.actor, "accepted_at": accepted_at},
        )
        store.update(
            "roadmap_commitments",
            commitment["id"],
            {"status": "accepted", "accepted_by": args.actor, "accepted_at": accepted_at},
        )
        print(f"accepted_revision: {revision['id']}")
        print(f"accepted_commitment: {commitment['id']}")
        print(f"accepted_by: {args.actor}")
    finally:
        store.close()


def cmd_roadmap_reject(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        revision = store.get("roadmap_revisions", args.revision)
        if not revision:
            raise SystemExit(f"roadmap revision not found: {args.revision}")
        if revision["status"] != "pending":
            raise SystemExit(f"roadmap revision is not pending: {args.revision}")
        commitment = store.get("roadmap_commitments", revision["proposed_commitment_id"])
        if not commitment:
            raise SystemExit(f"roadmap commitment not found: {revision['proposed_commitment_id']}")
        rejected_at = now_iso()
        store.update(
            "roadmap_revisions",
            revision["id"],
            {"status": "rejected", "reason": args.reason, "decided_by": args.actor, "accepted_at": rejected_at},
        )
        store.update(
            "roadmap_commitments",
            commitment["id"],
            {"status": "rejected", "accepted_by": args.actor, "accepted_at": rejected_at},
        )
        print(f"rejected_revision: {revision['id']}")
        print(f"rejected_commitment: {commitment['id']}")
        print(f"rejected_by: {args.actor}")
    finally:
        store.close()


def cmd_roadmap_close(args: argparse.Namespace) -> None:
    from .. import cli as c

    store = Store(args.db)
    try:
        commitment = store.get("roadmap_commitments", args.commitment)
        if not commitment:
            raise SystemExit(f"roadmap commitment not found: {args.commitment}")
        if commitment["status"] != "accepted":
            raise SystemExit(f"roadmap commitment is not accepted: {args.commitment}")
        tasks, statuses = c.project_tasks_and_statuses(store, commitment["project_id"])
        assessment = c.roadmap_commitment_assessment(store, commitment, tasks, statuses)
        if not assessment["closure_ready"] and not args.force:
            reason = assessment["unresolved_reason"] or "commitment is not closure-ready"
            raise SystemExit(f"roadmap commitment is not closure-ready: {reason}")
        closed_at = now_iso()
        store.update(
            "roadmap_commitments",
            commitment["id"],
            {
                "status": "closed",
                "closed_by": args.actor,
                "closed_at": closed_at,
                "closure_reason": args.reason,
            },
        )
        print(f"closed_commitment: {commitment['id']}")
        print(f"closed_by: {args.actor}")
        print(f"closed_at: {closed_at}")
    finally:
        store.close()


def cmd_roadmap_status(args: argparse.Namespace) -> None:
    from .. import cli as c

    store = Store(args.db)
    try:
        project = store.get("projects", args.project)
        if not project:
            raise SystemExit(f"project not found: {args.project}")
        tasks, statuses = c.project_tasks_and_statuses(store, project["id"])
        commitments = c.accepted_roadmap_commitments(store, project["id"])
        closed_commitments = c.closed_roadmap_commitments(store, project["id"])
        pending_revisions = c.pending_roadmap_revisions(store, project["id"])
        print(f"project_id: {project['id']}")
        agent_state = c.roadmap_agent_state(store, project["id"], tasks, statuses)
        c.print_roadmap_agent_state(agent_state)
        c.print_roadmap_agent_next_actions(c.roadmap_agent_next_actions(store, project["id"], agent_state))
        print("accepted_commitments:")
        if commitments:
            for commitment in commitments:
                print(f"- {commitment['id']} {commitment['title']}")
                print(f"  intent: {commitment['intent'] or 'none'}")
        else:
            print("- none")
        print("closed_commitments:")
        if closed_commitments:
            for commitment in closed_commitments:
                print(f"- {commitment['id']} {commitment['title']}")
                print(f"  closed_at: {commitment['closed_at'] or 'none'}")
                print(f"  closure_reason: {commitment['closure_reason'] or 'none'}")
        else:
            print("- none")
        print("pending_revisions:")
        if pending_revisions:
            for revision in pending_revisions:
                commitment = store.get("roadmap_commitments", revision["proposed_commitment_id"])
                title = commitment["title"] if commitment else "missing commitment"
                print(f"- {revision['id']} -> {revision['proposed_commitment_id']} {title}")
                if revision.get("source_path"):
                    print(f"  source_path: {revision['source_path']}")
        else:
            print("- none")
    finally:
        store.close()


def cmd_roadmap_assess(args: argparse.Namespace) -> None:
    from .. import cli as c

    store = Store(args.db)
    try:
        project = store.get("projects", args.project)
        if not project:
            raise SystemExit(f"project not found: {args.project}")
        tasks, statuses = c.project_tasks_and_statuses(store, project["id"])
        body = render_roadmap_assess_markdown(project, c.roadmap_assessments(store, project["id"], tasks, statuses))
        if args.file:
            output = Path(args.file)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(body, encoding="utf-8")
            print(f"written: {output}")
        else:
            print(body, end="")
    finally:
        store.close()


def cmd_roadmap_summary(args: argparse.Namespace) -> None:
    from .. import cli as c

    store = Store(args.db)
    try:
        project = store.get("projects", args.project)
        if not project:
            raise SystemExit(f"project not found: {args.project}")
        tasks, statuses = c.project_tasks_and_statuses(store, project["id"])
        assessments = c.roadmap_assessments(store, project["id"], tasks, statuses)
        body = render_human_roadmap_summary_markdown(project, c.human_roadmap_summary(assessments))
        if args.file:
            output = Path(args.file)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(body, encoding="utf-8")
            print(f"written: {output}")
        else:
            print(body, end="")
    finally:
        store.close()


def cmd_roadmap_discuss(args: argparse.Namespace) -> None:
    from .. import cli as c

    store = Store(args.db)
    try:
        project = store.get("projects", args.project)
        if not project:
            raise SystemExit(f"project not found: {args.project}")
        tasks, statuses = c.project_tasks_and_statuses(store, project["id"])
        summary = c.project_summary_data(store, project, tasks, statuses)
        body = render_roadmap_discuss_markdown(summary)
        if args.file:
            output = Path(args.file)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(body, encoding="utf-8")
            print(f"written: {output}")
            proposal_path = Path(c.roadmap_proposal_path_for_commitment(store, project["id"]))
            if proposal_path.exists():
                source_path = comparable_roadmap_source_path(str(proposal_path))
                revisions = [
                    revision
                    for revision in store.list_where("roadmap_revisions", "project_id=?", (project["id"],))
                    if comparable_roadmap_source_path(revision.get("source_path") or "") == source_path
                ]
                pending = [revision for revision in revisions if revision["status"] == "pending"]
                if pending:
                    revision = pending[0]
                    commitment = store.get("roadmap_commitments", revision["proposed_commitment_id"])
                    title = commitment["title"] if commitment else "missing commitment"
                    print(
                        f"notice: {proposal_path} already exists and is linked to pending roadmap revision "
                        f"{revision['id']} for {revision['proposed_commitment_id']} {title}"
                    )
                else:
                    statuses = ", ".join(
                        f"{revision['id']}:{revision['status']}" for revision in revisions
                    ) or "none"
                    print(
                        f"warning: {proposal_path} already exists but is not linked to a pending roadmap revision "
                        f"(matching revisions: {statuses}); verify it is a fresh internal RoadmapProposal draft before import"
                    )
        else:
            print(body, end="")
    finally:
        store.close()


def cmd_roadmap_task_plan(args: argparse.Namespace) -> None:
    from .. import cli as c

    store = Store(args.db)
    try:
        commitment = store.get("roadmap_commitments", args.commitment)
        if not commitment:
            raise SystemExit(f"roadmap commitment not found: {args.commitment}")
        if commitment["status"] != "accepted":
            raise SystemExit(f"roadmap commitment is not accepted: {args.commitment}")
        body = render_roadmap_task_plan_markdown(commitment)
        if args.file:
            output = Path(args.file)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(body, encoding="utf-8")
            print(f"written: {output}")
        else:
            print(body, end="")
    finally:
        store.close()


def cmd_roadmap_export(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        project = store.get("projects", args.project)
        if not project:
            raise SystemExit(f"project not found: {args.project}")
        output = render_and_write_human_roadmap(store, project, args.file)
        print(f"written: {output}")
    finally:
        store.close()
