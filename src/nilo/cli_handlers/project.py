from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..cli_support import read_text_or_exit
from ..project_model import project_row_from_args
from ..store import Store
from ..timeutil import now_iso


def cmd_project_create(args: argparse.Namespace) -> None:
    from .. import cli as c

    store = Store(args.db)
    try:
        created_at = now_iso()
        row = project_row_from_args(args, created_at)
        store.insert("projects", row)
        print(row["id"])
    finally:
        store.close()


def cmd_project_status(args: argparse.Namespace) -> None:
    from .. import cli as c

    store = Store(args.db)
    try:
        project = store.get("projects", args.project)
        if not project:
            raise SystemExit(f"project not found: {args.project}")
        tasks, statuses = c.project_tasks_and_statuses(store, args.project)
        active_tasks = [task for task in tasks if not c.is_task_completed_status(statuses[task["id"]])]
        if not getattr(args, "verbose", False):
            c.print_human_project_status(store, project, active_tasks, statuses)
            return

        print(f"project_id: {project['id']}")
        print(f"project_name: {project['name']}")
        design_residue = c.project_design_residue()
        commitments = c.accepted_roadmap_commitments(store, project["id"])
        pending_revisions = c.pending_roadmap_revisions(store, project["id"])
        print(f"roadmap_position: {c.project_roadmap_position(tasks, statuses, design_residue, commitments)}")
        print(f"work_state: {c.project_work_state(tasks, statuses)}")
        print(f"current_phase: {c.project_current_phase(tasks, statuses)}")
        print("active_tasks:")
        if not active_tasks:
            print("- none")

        all_unexecuted: list[tuple[str, str]] = []
        for task in active_tasks:
            status = statuses[task["id"]]
            verification_run = store.latest_for_task("verification_runs", task["id"])
            unexecuted = c.unexecuted_verifications_for_task(status, verification_run)
            all_unexecuted.extend((task["id"], item) for item in unexecuted)
            print(f"- {task['id']} [{status}] {task['task_type']} {task['risk_level']} {task['title']}")
            recipe_label = c.human_recipe_provenance_label(c.recipe_provenance_summary(store, task["id"]))
            if recipe_label:
                print(f"  recipe: {recipe_label}")
            print(f"  latest_verification_run: {c.verification_summary(verification_run)}")
            print(f"  verification_working_tree: {c.verification_working_tree_summary(verification_run)}")
            for line in c.verification_snapshot_policy_lines(verification_run):
                print(f"  {line}")
            blocking = c.unresolved_blocking_review_findings(store, task["id"])
            if blocking:
                print("  unresolved_blocking_review_findings:")
                for finding in blocking:
                    print(f"  - {finding['id']}")
            print("  next_actions:")
            for action in c.task_next_actions(task, status, verification_run, unexecuted):
                print(f"  - {action}")
            print("  unexecuted_verifications:")
            if unexecuted:
                for item in unexecuted:
                    print(f"  - {item}")
            else:
                print("  - none")

        print("next_actions:")
        project_actions = c.project_level_next_actions(store, tasks, statuses, design_residue, commitments, pending_revisions, project["id"])
        if project_actions:
            for action in project_actions:
                print(f"- {action}")
        else:
            print("- none")

        print("unexecuted_verifications:")
        if all_unexecuted:
            for task_id, item in all_unexecuted:
                print(f"- {task_id}: {item}")
        else:
            print("- none")
    finally:
        store.close()


def cmd_project_summary(args: argparse.Namespace) -> None:
    from .. import cli as c

    store = Store(args.db)
    try:
        project = store.get("projects", args.project)
        if not project:
            raise SystemExit(f"project not found: {args.project}")
        tasks, statuses = c.project_tasks_and_statuses(store, args.project)
        summary = c.project_summary_data(store, project, tasks, statuses)
        if args.format == "json":
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            c.print_project_summary_text(summary)
    finally:
        store.close()


def cmd_project_export_handson(args: argparse.Namespace) -> None:
    from .. import cli as c

    store = Store(args.db)
    try:
        output = Path(args.file)
        c.write_handson_markdown(store, args.project, output)
        print(f"exported: {output}")
    finally:
        store.close()


def cmd_project_export_recipes(args: argparse.Namespace) -> None:
    from .. import cli as c

    store = Store(args.db)
    try:
        project = store.get("projects", args.project)
        if not project:
            raise SystemExit(f"project not found: {args.project}")
        data = c.recipe_handoff_export_data(store, project, Path.cwd())
        output = Path(args.file)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"written: {output}")
        for diagnostic in data["diagnostics"]:
            print(f"diagnostic: {diagnostic['severity']}: {diagnostic['code']}: {diagnostic['message']}")
    finally:
        store.close()


def cmd_project_import_recipes(args: argparse.Namespace) -> None:
    from .. import cli as c

    store = Store(args.db)
    try:
        project = store.get("projects", args.project)
        if not project:
            raise SystemExit(f"project not found: {args.project}")
        body = read_text_or_exit(Path(args.file))
        data = json.loads(body)
        result = c.recipe_handoff_import_data(store, project, data, Path.cwd())
        print(f"imported_tasks: {result['imported_tasks']}")
        print(f"imported_provenance: {result['imported_provenance']}")
        print(f"imported_recipe_files: {result['imported_recipe_files']}")
        for diagnostic in result["diagnostics"]:
            print(f"diagnostic: {diagnostic['severity']}: {diagnostic['code']}: {diagnostic['message']}")
    finally:
        store.close()
