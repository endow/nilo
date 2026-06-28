from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from ..cli import prompt_quality_review
from ..cli_support import make_id, read_text_or_exit
from ..instruction import build_autoscore_prompt, build_review_prompt
from ..quality import parse_quality_review
from ..quality_logic import (
    normalize_required_scores,
    parse_scores,
    required_scores_for_task,
    validate_known_scores,
    validate_required_scores,
)
from ..project_boundary import ProjectBoundaryError, record_nilo_issue_for_task, require_write_fence, resolve_project_boundary
from ..review import VALID_FINDING_STATUSES, build_review_context, build_review_result_template, parse_review_result
from ..review_dispatcher import dispatch_review, doctor_reviewer_config, init_reviewer_config, quick_review
from ..review_lifecycle import insert_review_request, update_review_request
from ..reviewer_registry import ReviewerResolutionError, resolve_reviewer, resolve_review_request_target, reviewer_is_registered_available
from ..reviewer_registry import reviewer_prepare_status
from ..secret import mask_secrets
from ..snapshot import compact_snapshot, current_git_snapshot, review_result_status
from ..store import Store
from ..task_logic import is_task_completed_status, projected_task_status
from ..timeutil import now_iso
from ..transitions import TransitionError, import_review_result, update_review_finding


def parse_git_status_porcelain_z(stdout: str) -> list[str]:
    entries = [entry for entry in stdout.split("\0") if entry]
    files: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        status = entry[:2]
        path = entry[3:].strip()
        if status[0] in {"R", "C"} and index + 1 < len(entries):
            index += 1
            path = entries[index].strip()
        if path:
            files.append(path.replace("\\", "/"))
        index += 1
    return sorted(set(files))


def dirty_tree_files(cwd: Path) -> tuple[list[str], str]:
    completed = subprocess.run(
        ["git", "-c", "core.quotepath=false", "status", "--porcelain", "-z"],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        return [], "dirty tree could not be inspected; not a git repository or git is unavailable"
    return parse_git_status_porcelain_z(completed.stdout), ""


def cmd_quality_quick(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        if args.interactive:
            summary, issues, score_values = prompt_quality_review(args)
        else:
            if not args.summary:
                raise SystemExit("--summary is required unless --interactive is used")
            summary = args.summary
            issues = args.issue or []
            score_values = args.score or []
        if not summary.strip():
            raise SystemExit("quality summary is required")
        required_scores = required_scores_for_task(store, task, args.required_score or [])
        scores = parse_scores(score_values)
        validate_required_scores(scores, required_scores, args.strict_scores)
        row = {
            "id": make_id("quality"),
            "task_id": args.task,
            "reviewer": args.reviewer or "ai_review",
            "scores": scores,
            "summary": summary,
            "issues": issues,
            "created_at": now_iso(),
        }
        store.insert("quality_reviews", row)
        print(f"quality_review: {row['id']}")
        print(f"quality_summary: {row['summary']}")
        if row["issues"]:
            print("quality_issues:")
            for issue in row["issues"]:
                print(f"- {issue}")
        if row["scores"]:
            print("quality_scores:")
            for key, score in row["scores"].items():
                print(f"- {key}: {score}")
    finally:
        store.close()


def cmd_quality_autoscore_prepare(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        required_scores = required_scores_for_task(store, task, args.required_score or [])
        report = store.latest_for_task("agent_reports", args.task)
        verification_run = store.latest_for_task("verification_runs", args.task)
        print(build_autoscore_prompt(task, report, None, verification_run, required_scores))
    finally:
        store.close()


def cmd_quality_autoscore_import(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        if args.file:
            body = read_text_or_exit(Path(args.file))
        else:
            body = sys.stdin.read()
        if not body.strip():
            raise SystemExit("autoscore body is empty")
        required_scores = required_scores_for_task(store, task, args.required_score or [])
        try:
            summary, issues, scores = parse_quality_review(body)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
        validate_required_scores(scores, required_scores, args.strict_scores)
        validate_known_scores(scores, required_scores, args.allow_unknown_scores)
        row = {
            "id": make_id("quality"),
            "task_id": args.task,
            "reviewer": args.reviewer or "ai_review",
            "scores": scores,
            "summary": summary,
            "issues": issues,
            "created_at": now_iso(),
        }
        store.insert("quality_reviews", row)
        print(f"quality_review: {row['id']}")
        if row["scores"]:
            print("quality_scores:")
            for key, score in row["scores"].items():
                print(f"- {key}: {score}")
    finally:
        store.close()


def cmd_quality_schema_set(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        project = store.get("projects", args.project)
        if not project:
            raise SystemExit(f"project not found: {args.project}")
        required_scores = normalize_required_scores(args.required_score or [])
        if not required_scores:
            raise SystemExit("at least one required score is required")
        existing = store.get("quality_score_schemas", args.project)
        updated_at = now_iso()
        if existing:
            store.update(
                "quality_score_schemas",
                args.project,
                {
                    "required_scores": required_scores,
                    "updated_at": updated_at,
                },
            )
            schema_id = existing["id"]
        else:
            schema_id = args.project
            store.insert(
                "quality_score_schemas",
                {
                    "id": schema_id,
                    "project_id": args.project,
                    "required_scores": required_scores,
                    "created_at": updated_at,
                    "updated_at": updated_at,
                },
            )
        print(f"quality_score_schema: {schema_id}")
        print("required_scores:")
        for score in required_scores:
            print(f"- {score}")
    finally:
        store.close()


def cmd_quality_schema_list(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        project = store.get("projects", args.project)
        if not project:
            raise SystemExit(f"project not found: {args.project}")
        schema = store.get("quality_score_schemas", args.project)
        print(f"project_id: {args.project}")
        print("required_scores:")
        for score in schema["required_scores"] if schema else []:
            print(f"- {score}")
    finally:
        store.close()


def cmd_review_prepare(args: argparse.Namespace) -> None:
    if args.project or args.reviewer:
        if not args.project or not args.reviewer:
            raise SystemExit("review prepare readiness check requires --project and --reviewer")
        result = call_prepare_reviewer(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if not args.task:
        raise SystemExit("review prepare requires --task, or --project and --reviewer for readiness check")
    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        if args.review:
            request = store.get("review_requests", args.review)
            if not request or request["task_id"] != args.task:
                raise SystemExit(f"review request not found for task: {args.review}")
            report = store.latest_for_task("agent_reports", args.task)
            verification_run = store.latest_for_task("verification_runs", args.task)
            body = build_review_context(task, request, report, None, verification_run, Path.cwd())
            output = review_prompt_output_path(args, request)
            if output:
                write_review_file(output, body)
            else:
                print(body)
            return
        print(build_review_prompt(task))
    finally:
        store.close()


def call_prepare_reviewer(args: argparse.Namespace) -> dict:
    store = Store(args.db)
    try:
        project = store.get("projects", args.project)
        if not project:
            raise SystemExit(f"project not found: {args.project}")
        return {
            "project_id": args.project,
            **reviewer_prepare_status(store, args.reviewer),
        }
    finally:
        store.close()


def cmd_review_template(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        request = store.get("review_requests", args.review)
        if not request:
            raise SystemExit(f"review request not found: {args.review}")
        output = review_template_output_path(args, request)
        write_review_file(output, build_review_result_template(request), label="review_template")
        print(f"next_action: nilo review import --task {request['task_id']} --review {request['id']} --file {output}")
    finally:
        store.close()


def review_prompt_output_path(args: argparse.Namespace, request: dict) -> Path | None:
    if getattr(args, "write_default", False):
        return Path(".nilo") / "reviews" / f"{request['id']}_prompt.md"
    if args.file:
        return Path(args.file)
    return None


def review_template_output_path(args: argparse.Namespace, request: dict) -> Path:
    if getattr(args, "write_default", False):
        return Path(".nilo") / "reviews" / f"{request['id']}.md"
    return Path(args.file)


def write_review_file(output: Path, body: str, label: str = "review_context") -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(body, encoding="utf-8")
    print(f"{label}: {output}")


def cmd_review_import(args: argparse.Namespace) -> None:
    if args.review:
        cmd_review_result_import(args)
        return

    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        if args.file:
            body = read_text_or_exit(Path(args.file))
        else:
            body = sys.stdin.read()
        if not body.strip():
            raise SystemExit("review body is empty")
        required_scores = required_scores_for_task(store, task, args.required_score or [])
        try:
            summary, issues, scores = parse_quality_review(body)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
        validate_required_scores(scores, required_scores, args.strict_scores)
        validate_known_scores(scores, required_scores, args.allow_unknown_scores)
        row = {
            "id": make_id("quality"),
            "task_id": args.task,
            "reviewer": args.reviewer or "ai_review",
            "scores": scores,
            "summary": summary,
            "issues": issues,
            "created_at": now_iso(),
        }
        store.insert("quality_reviews", row)
        print(f"quality_review: {row['id']}")
    finally:
        store.close()


def cmd_review_request(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        try:
            resolved = resolve_review_request_target(store, args.reviewer)
        except ReviewerResolutionError as exc:
            raise SystemExit(f"{exc}\nnext_action: {exc.next_action}") from None
        created_at = now_iso()
        latest_event = store.latest_task_status_event(args.task)
        snapshot = compact_snapshot(current_git_snapshot(Path.cwd()))
        row = {
            "id": make_id("review"),
            "task_id": args.task,
            "requester": args.requester,
            "reviewer": resolved.reviewer,
            "status": "requested" if reviewer_has_fresh_heartbeat(store, resolved.reviewer) else "reviewer_unavailable",
            "reason": args.reason,
            "based_on_event_id": latest_event["event_id"] if latest_event else "",
            "based_on_snapshot": snapshot,
            "created_at": created_at,
            "updated_at": created_at,
        }
        insert_review_request(store, row)
        print(f"review_request: {row['id']}")
        print(f"status: {row['status']}")
        if row["status"] == "reviewer_unavailable":
            print(
                "next_action: "
                f"start a real MCP reviewer worker for {row['reviewer']}; "
                "nilo mcp reviewer-start only records heartbeat"
            )
        else:
            print(f"next_action: nilo review prepare --task {args.task} --review {row['id']}")
        print(f"handoff_prompt: nilo review prepare --task {args.task} --review {row['id']} --write-default")
        print(f"review_template: nilo review template --review {row['id']} --write-default")
    finally:
        store.close()
    maybe_wait_for_review(args, row["id"])


def cmd_review_dispatch(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task_id = args.task
        project_id = args.project
        if not task_id:
            if not project_id:
                raise SystemExit("review dispatch requires --task or --project")
            task, _dirty_tree_review = active_review_task(store, project_id, None, allow_dirty_tree_task=True)
            task_id = task["id"]
        result = dispatch_review(
            store,
            actor=args.actor,
            reviewer=args.reviewer,
            task_id=task_id,
            project_id=project_id,
            reason=args.reason,
            auto_start=args.auto_start,
            auto_configure=args.auto_configure,
            config_path=Path(args.config) if args.config else None,
            repo_root=Path.cwd(),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["status"] == "review_failed":
            raise SystemExit(1)
    finally:
        store.close()


def cmd_review_quick(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task_id = args.task
        project_id = args.project
        if not task_id:
            if not project_id:
                raise SystemExit("review quick requires --task or --project")
            task, _dirty_tree_review = active_review_task(store, project_id, None, allow_dirty_tree_task=True)
            task_id = task["id"]
        result = quick_review(
            store,
            actor=args.actor,
            reviewer=args.reviewer,
            task_id=task_id,
            project_id=project_id,
            reason=args.reason,
            should_import=args.should_import,
            timeout_seconds=args.timeout,
            auto_configure=args.auto_configure,
            config_path=Path(args.config) if args.config else None,
            repo_root=Path.cwd(),
        )
        if result.get("stdout"):
            print(result["stdout"], end="" if result["stdout"].endswith("\n") else "\n")
        if result.get("stderr"):
            print("quick_stderr:")
            print(result["stderr"], end="" if result["stderr"].endswith("\n") else "\n")
        print("quick_usage: local CLI fallback / diagnostics only; prefer Nilo MCP dispatch_review for normal AI review handoff")
        print(f"quick_status: {result['status']}")
        print(f"quick_imported: {str(bool(result.get('imported'))).lower()}")
        if result.get("review_request_id"):
            print(f"review_request: {result['review_request_id']}")
        if result.get("review_result_id"):
            print(f"review_result: {result['review_result_id']}")
        if result.get("verdict"):
            print(f"verdict: {result['verdict']}")
        if result.get("reason"):
            print(f"reason: {result['reason']}")
        if result["status"] in {"review_failed", "needs_reviewer_config"}:
            raise SystemExit(1)
    finally:
        store.close()


def cmd_review_init(args: argparse.Namespace) -> None:
    reviewers = args.reviewer or ["claude-code", "codex"]
    path = Path(args.config) if args.config else Path.cwd() / ".nilo" / "reviewers.toml"
    result = init_reviewer_config(path, reviewers, overwrite=args.overwrite)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_review_doctor(args: argparse.Namespace) -> None:
    reviewers = args.reviewer or None
    path = Path(args.config) if args.config else Path.cwd() / ".nilo" / "reviewers.toml"
    result = doctor_reviewer_config(path, reviewers)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def create_dirty_tree_review_task(store: Store, project_id: str, files: list[str]) -> dict:
    created_at = now_iso()
    file_lines = "\n".join(f"- {path}" for path in files)
    row = {
        "id": make_id("task"),
        "project_id": project_id,
        "title": "Review current dirty tree",
        "description": f"Review the current uncommitted working tree changes.\n\nDirty files:\n{file_lines}",
        "acceptance_criteria": [
            "Dirty tree review task.",
            "Review the current dirty tree only.",
            "Do not modify code during review.",
            "Report findings through Nilo review import.",
            *[f"Dirty file: {path}" for path in files],
        ],
        "parent_task_id": None,
        "split_index": None,
        "task_type": "review",
        "risk_level": "medium",
        "requires_understanding_check": False,
        "roadmap_commitment_id": "",
        "roadmap_item_id": "",
        "status": "planned",
        "assigned_model_profile": "",
        "degradation_mode": "normal",
        "base_commit": None,
        "created_at": created_at,
    }
    store.insert("tasks", row)
    return row


def is_dirty_tree_review_task(task: dict) -> bool:
    return task.get("task_type") == "review" and "Dirty tree review task." in task.get("acceptance_criteria", [])


def active_review_task(store: Store, project_id: str, task_id: str | None, allow_dirty_tree_task: bool = False) -> tuple[dict, bool]:
    if task_id:
        task = store.get("tasks", task_id)
        if not task:
            raise SystemExit(f"task not found: {task_id}")
        if task["project_id"] != project_id:
            raise SystemExit(f"task does not belong to project {project_id}: {task_id}")
        return task, False
    tasks = store.list_where("tasks", "project_id=?", (project_id,))
    active = [task for task in tasks if not is_task_completed_status(projected_task_status(store, task))]
    if allow_dirty_tree_task:
        files, inspection_error = dirty_tree_files(Path.cwd())
        if files and (not active or all(is_dirty_tree_review_task(task) for task in active)):
            return create_dirty_tree_review_task(store, project_id, files), True
    if not active:
        if allow_dirty_tree_task:
            if inspection_error:
                raise SystemExit(f"active task not found and {inspection_error}; pass --task explicitly")
        raise SystemExit("active task not found and no dirty working tree files were detected; pass --task explicitly")
    review_ready_statuses = {"verification_passed", "evidence_submitted", "review_approved", "review_commented"}
    review_ready = [task for task in active if projected_task_status(store, task) in review_ready_statuses]
    candidates = review_ready or active
    if len(candidates) > 1:
        details = ", ".join(f"{task['id']} [{projected_task_status(store, task)}] {task['title']}" for task in candidates)
        raise SystemExit(f"multiple review candidate tasks found; pass --task explicitly: {details}")
    return candidates[0], False


def cmd_review_delegate(args: argparse.Namespace) -> None:
    row, task, dirty_tree_review = create_review_delegation(args)
    print_review_delegation(args.project, row, task, dirty_tree_review)
    maybe_wait_for_review(args, row["id"])


def create_review_delegation(args: argparse.Namespace) -> tuple[dict, dict, bool]:
    store = Store(args.db)
    try:
        project = store.get("projects", args.project)
        if not project:
            raise SystemExit(f"project not found: {args.project}")
        try:
            resolved = resolve_reviewer(store, args.reviewer)
        except ReviewerResolutionError as exc:
            raise SystemExit(f"{exc}\nnext_action: {exc.next_action}") from None
        task, dirty_tree_review = active_review_task(store, args.project, args.task, allow_dirty_tree_task=True)
        created_at = now_iso()
        latest_event = store.latest_task_status_event(task["id"])
        snapshot = compact_snapshot(current_git_snapshot(Path.cwd()))
        row = {
            "id": make_id("review"),
            "task_id": task["id"],
            "requester": args.requester,
            "reviewer": resolved.reviewer,
            "status": "requested" if reviewer_has_fresh_heartbeat(store, resolved.reviewer) else "reviewer_unavailable",
            "reason": args.reason,
            "based_on_event_id": latest_event["event_id"] if latest_event else "",
            "based_on_snapshot": snapshot,
            "created_at": created_at,
            "updated_at": created_at,
        }
        insert_review_request(store, row)
        if args.write_default:
            report = store.latest_for_task("agent_reports", task["id"])
            verification_run = store.latest_for_task("verification_runs", task["id"])
            prompt_path = Path(".nilo") / "reviews" / f"{row['id']}_prompt.md"
            template_path = Path(".nilo") / "reviews" / f"{row['id']}.md"
            write_review_file(prompt_path, build_review_context(task, row, report, None, verification_run, Path.cwd()))
            write_review_file(template_path, build_review_result_template(row), label="review_template")
        return row, task, dirty_tree_review
    finally:
        store.close()


def claude_instruction(project_id: str, task: dict, review_request: dict, dirty_tree_review: bool = False) -> str:
    lines = [
        f"Nilo MCP を使って task {task['id']} の review {review_request['id']} をレビューして。",
    ]
    if dirty_tree_review:
        lines.append("レビュー対象は現在の未コミット差分です。")
    lines.extend(
        [
            "まず register_reviewer で実 reviewer worker として availability を更新してから、claim_next_review で対象 review を claim して。",
            "claim 結果に含まれる prompt/template を使い、レビュー結果を import_review_result で Nilo に戻して。",
            f"状態確認が必要な場合は get_status(project_id=\"{project_id}\") または get_task_status を使って。",
            "MCP 経由で検証ログを書き戻す必要がある場合は record_verification を使って。",
            "コード変更はしないで。",
            "通常のAI間レビュー依頼では claude/codex CLI を直接起動せず、Nilo MCP review workflow を優先する。",
        ]
    )
    return "\n".join(lines) + "\n"


def print_review_delegation(project_id: str, review_request: dict, task: dict, dirty_tree_review: bool = False) -> None:
    print(f"review_request: {review_request['id']}")
    print(f"task_id: {task['id']}")
    if dirty_tree_review:
        print("review_target: current dirty tree")
    print(f"reviewer: {review_request['reviewer']}")
    print(f"status: {review_request['status']}")
    if review_request["status"] == "reviewer_unavailable":
        print(
            "next_action: "
            f"start a real MCP reviewer worker for {review_request['reviewer']}; "
            "nilo mcp reviewer-start only records heartbeat"
        )
    print("claude_instruction:")
    print(claude_instruction(project_id, task, review_request, dirty_tree_review), end="")


def claude_runner_command(args: argparse.Namespace) -> list[str]:
    return [
        "rtk",
        "proxy",
        "claude",
        "-p",
        "--mcp-config",
        args.mcp_config,
        "--permission-mode",
        args.permission_mode,
    ]


def print_human_runner_command(args: argparse.Namespace) -> None:
    if not getattr(args, "verbose", False):
        return
    print("human_runner_command:")
    print(" ".join(claude_runner_command(args)))


def cmd_review_human_launch_claude(args: argparse.Namespace) -> None:
    if args.dry_run:
        store = Store(args.db)
        try:
            project = store.get("projects", args.project)
            if not project:
                raise SystemExit(f"project not found: {args.project}")
            task, dirty_tree_review = active_review_task(store, args.project, args.task, allow_dirty_tree_task=False)
        finally:
            store.close()
        review_request = {
            "id": "<review_id>",
            "task_id": task["id"],
            "requester": args.requester,
            "reviewer": "claude-code",
            "status": "dry_run",
            "reason": args.reason,
        }
        print_review_delegation(args.project, review_request, task, dirty_tree_review)
        print("human_launch_note: human-requested Claude CLI helper; normal AI review handoff should use Nilo MCP dispatch_review")
        print_human_runner_command(args)
        print("claude_status: skipped (dry-run)")
        return

    delegate_args = argparse.Namespace(
        db=args.db,
        project=args.project,
        task=args.task,
        requester=args.requester,
        reviewer="claude-code",
        reason=args.reason,
        write_default=args.write_default,
    )
    review_request, task, dirty_tree_review = create_review_delegation(delegate_args)
    instruction = claude_instruction(args.project, task, review_request, dirty_tree_review)
    print_review_delegation(args.project, review_request, task, dirty_tree_review)
    print("human_launch_note: human-requested Claude CLI helper; normal AI review handoff should use Nilo MCP dispatch_review")

    command = claude_runner_command(args)
    print_human_runner_command(args)

    result = subprocess.run(
        command,
        input=instruction,
        text=True,
        cwd=Path.cwd(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=args.timeout,
    )
    print(f"claude_exit_code: {result.returncode}")
    if result.stdout:
        print("claude_stdout:")
        print(result.stdout.rstrip())
    if result.stderr:
        print("claude_stderr:")
        print(result.stderr.rstrip())
    if result.returncode != 0:
        raise SystemExit(result.returncode)

    store = Store(args.db)
    try:
        refreshed = store.get("review_requests", review_request["id"])
        print(f"review_status: {refreshed['status'] if refreshed else 'unknown'}")
    finally:
        store.close()


def cmd_review_result_import(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        request = store.get("review_requests", args.review)
        if not request or request["task_id"] != args.task:
            raise SystemExit(f"review request not found for task: {args.review}")
        if args.file:
            body = read_text_or_exit(Path(args.file))
        else:
            body = sys.stdin.read()
        if not body.strip():
            raise SystemExit("review body is empty")
        boundary = resolve_project_boundary(db_path=args.db)
        try:
            require_write_fence(boundary)
        except ProjectBoundaryError as exc:
            record_nilo_issue_for_task(store, task["project_id"], task["id"], "review import", exc, boundary)
            raise SystemExit(str(exc)) from exc
        last_seen = _require_cli_fresh_task_context(
            store,
            args.task,
            getattr(args, "last_seen_event_id", ""),
            getattr(args, "context_token", ""),
        )
        try:
            result = import_review_result(
                store,
                args.task,
                args.review,
                body_md=body,
                reviewer=args.reviewer or request["reviewer"],
                last_seen_event_id=last_seen,
                cwd=Path.cwd(),
            )
        except TransitionError as exc:
            raise SystemExit(f"{exc.message}{(': ' + exc.remediation) if exc.remediation else ''}") from exc
        review_result = store.get("review_results", result.created_ids["review_result"])
        findings = store.list_where("review_findings", "review_result_id=?", (review_result["id"],))
        print(f"review_result: {review_result['id']}")
        print(f"verdict: {review_result['verdict']}")
        if findings:
            print("findings:")
            for finding in findings:
                marker = "blocking" if finding["blocking"] else "nonblocking"
                print(f"- {finding['status']} {finding['severity']} {marker}: {finding['title']}")
    finally:
        store.close()


def cmd_review_status(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        requests = store.list_where("review_requests", "task_id=?", (args.task,))
        findings = store.list_where("review_findings", "task_id=?", (args.task,))
        data = review_status_data(store, args.task, requests, findings)
        if args.format == "json":
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return
        print(f"task_id: {args.task}")
        print_review_finding_summary(data)
        print("review_requests:")
        if requests:
            for request in requests:
                print(f"- {request['id']} [{request['status']}] {request['requester']} -> {request['reviewer']}: {request['reason']}")
                if request["status"] == "withdrawn":
                    print(f"  withdrawn_by: {request['withdrawn_actor']}")
                    print(f"  withdrawn_at: {request['withdrawn_at']}")
                    print(f"  withdrawn_reason: {request['withdrawn_reason']}")
        else:
            print("- none")
        print("review_results:")
        if data["review_results"]:
            for result in data["review_results"]:
                print(f"- {result['id']} [{result['verdict']}, {review_result_status(result, current_git_snapshot(Path.cwd()))}] {result['reviewer']}: {result['summary']}")
        else:
            print("- none")
        print("review_findings:")
        if data["review_findings"]:
            for finding in data["review_findings"]:
                marker = "blocking" if finding["blocking"] else "nonblocking"
                location = f" {finding['file_path']}:{finding['line']}" if finding["file_path"] else ""
                print(f"- {finding['id']} [{finding['status']}] {finding['severity']} {marker}{location}: {finding['title']}")
                print_review_finding_history(finding["update_history"])
        else:
            print("- none")
    finally:
        store.close()


def cmd_review_withdraw(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        request = store.get("review_requests", args.review)
        if not request:
            raise SystemExit(f"review request not found: {args.review}")
        if request["status"] in {"completed", "withdrawn"}:
            raise SystemExit(f"review request is terminal and cannot be withdrawn: {args.review} [{request['status']}]")
        withdrawn_at = now_iso()
        update_review_request(
            store,
            args.review,
            {
                "status": "withdrawn",
                "withdrawn_reason": args.reason,
                "withdrawn_actor": args.actor,
                "withdrawn_at": withdrawn_at,
                "updated_at": withdrawn_at,
            },
        )
        print(f"review_request: {args.review}")
        print("status: withdrawn")
        print(f"withdrawn_by: {args.actor}")
        print(f"withdrawn_reason: {args.reason}")
    finally:
        store.close()


REVIEW_TERMINAL_STATUSES = {"completed", "withdrawn"}
REVIEW_WAITABLE_STATUSES = {"requested", "reviewer_unavailable", "claimed", "in_progress", "stale"}


def maybe_wait_for_review(args: argparse.Namespace, review_id: str) -> None:
    if not getattr(args, "wait", False):
        return
    wait_args = argparse.Namespace(
        db=args.db,
        review=review_id,
        timeout=args.wait_timeout,
        poll_interval=args.poll_interval,
        actor=args.requester,
        timeout_reason=f"review wait timed out after {args.wait_timeout:g} seconds",
    )
    cmd_review_wait(wait_args)


def withdraw_review_request(store: Store, review_id: str, reason: str, actor: str) -> dict:
    request = store.get("review_requests", review_id)
    if not request:
        raise SystemExit(f"review request not found: {review_id}")
    if request["status"] in REVIEW_TERMINAL_STATUSES:
        raise SystemExit(f"review request is terminal and cannot be withdrawn: {review_id} [{request['status']}]")
    withdrawn_at = now_iso()
    return update_review_request(
        store,
        review_id,
        {
            "status": "withdrawn",
            "withdrawn_reason": reason,
            "withdrawn_actor": actor,
            "withdrawn_at": withdrawn_at,
            "updated_at": withdrawn_at,
        },
    )


def current_review_wait_state(store: Store, review_id: str) -> tuple[dict, dict | None]:
    request = store.get("review_requests", review_id)
    if not request:
        raise SystemExit(f"review request not found: {review_id}")
    results = store.list_where("review_results", "review_request_id=?", (review_id,))
    if results:
        return request, results[0]
    return request, None


def reviewer_has_fresh_heartbeat(store: Store, reviewer: str) -> bool:
    return reviewer_is_registered_available(store, reviewer)


def cmd_review_wait(args: argparse.Namespace) -> None:
    deadline = time.monotonic() + max(args.timeout, 0.0)
    while True:
        store = Store(args.db)
        try:
            request, result = current_review_wait_state(store, args.review)
            if result:
                print(f"review_request: {args.review}")
                print("status: completed")
                print(f"review_result: {result['id']}")
                print(f"verdict: {result['verdict']}")
                return
            if request["status"] in REVIEW_TERMINAL_STATUSES:
                print(f"review_request: {args.review}")
                print(f"status: {request['status']}")
                return
            if request["status"] not in REVIEW_WAITABLE_STATUSES:
                raise SystemExit(f"review request is not waitable: {args.review} [{request['status']}]")
            if request["status"] in {"requested", "reviewer_unavailable"} and not reviewer_has_fresh_heartbeat(
                store, request["reviewer"]
            ):
                withdrawn = withdraw_review_request(
                    store,
                    args.review,
                    f"reviewer unavailable while waiting: {request['reviewer']}",
                    args.actor,
                )
                print(f"review_request: {args.review}")
                print("status: withdrawn")
                print("wait_result: reviewer_unavailable")
                print(f"withdrawn_by: {withdrawn['withdrawn_actor']}")
                print(f"withdrawn_reason: {withdrawn['withdrawn_reason']}")
                raise SystemExit(1)
            if time.monotonic() >= deadline:
                withdrawn = withdraw_review_request(store, args.review, args.timeout_reason, args.actor)
                print(f"review_request: {args.review}")
                print("status: withdrawn")
                print("wait_result: timed_out")
                print(f"withdrawn_by: {withdrawn['withdrawn_actor']}")
                print(f"withdrawn_reason: {withdrawn['withdrawn_reason']}")
                raise SystemExit(1)
        finally:
            store.close()
        time.sleep(max(args.poll_interval, 0.0))


def review_status_data(store: Store, task_id: str, requests: list[dict], findings: list[dict]) -> dict:
    counts = {status: 0 for status in sorted(VALID_FINDING_STATUSES)}
    unresolved_blocking = 0
    for finding in findings:
        counts[finding["status"]] = counts.get(finding["status"], 0) + 1
        if finding["status"] == "unresolved" and finding["blocking"]:
            unresolved_blocking += 1
    enriched_findings = []
    for finding in findings:
        item = dict(finding)
        item["update_history"] = list(reversed(store.list_where("review_finding_updates", "finding_id=?", (finding["id"],))))
        enriched_findings.append(item)
    return {
        "task_id": task_id,
        "review_requests": requests,
        "review_results": store.list_where("review_results", "task_id=?", (task_id,)),
        "review_findings": enriched_findings,
        "total_findings": len(findings),
        "unresolved_blocking": unresolved_blocking,
        "finding_status_counts": counts,
    }


def print_review_finding_summary(data: dict) -> None:
    print("review_summary:")
    print(f"- total_findings: {data['total_findings']}")
    print(f"- unresolved_blocking: {data['unresolved_blocking']}")
    print("finding_status_counts:")
    for status in sorted(data["finding_status_counts"]):
        print(f"- {status}: {data['finding_status_counts'][status]}")


def print_review_finding_history(updates: list[dict]) -> None:
    print("  update_history:")
    if not updates:
        print("  - none")
        return
    for update in updates:
        print(
            f"  - {update['created_at']} {update['actor']}: "
            f"{update['previous_status']} -> {update['new_status']}; {update['reason']}"
        )


def cmd_review_finding_update(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        finding = store.get("review_findings", args.finding)
        if not finding:
            raise SystemExit(f"review finding not found: {args.finding}")
        _require_cli_fresh_task_context(store, finding["task_id"], getattr(args, "last_seen_event_id", ""), getattr(args, "context_token", ""))
        try:
            update_review_finding(
                store,
                finding["id"],
                status=args.status,
                reason=args.decision_note or args.reason,
                actor=args.actor,
                human_confirm=getattr(args, "human_confirm", False),
                decision_source="human_interactive" if args.actor == "human" else "",
            )
        except TransitionError as exc:
            raise SystemExit(f"{exc.message}{(': ' + exc.remediation) if exc.remediation else ''}") from exc
        print(f"review_finding: {finding['id']}")
        print(f"previous_status: {finding['status']}")
        print(f"status: {args.status}")
    finally:
        store.close()


def _event_id_from_cli_context(context_token: str, task_id: str) -> str:
    if not context_token:
        return ""
    parts = context_token.split(":", 2)
    if len(parts) != 3 or parts[0] != "task" or parts[1] != task_id:
        raise SystemExit("invalid context_token")
    return parts[2]


def _require_cli_fresh_task_context(store: Store, task_id: str, last_seen_event_id: str, context_token: str) -> str:
    observed = last_seen_event_id or _event_id_from_cli_context(context_token, task_id)
    if not observed:
        raise SystemExit("missing required argument: --context-token or --last-seen-event-id")
    latest = store.latest_task_status_event(task_id)
    current = latest["event_id"] if latest else ""
    if observed != current:
        raise SystemExit(f"stale task state: last_seen_event_id={observed}, current_event_id={current}")
    return observed
