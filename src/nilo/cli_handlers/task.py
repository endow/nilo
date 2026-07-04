from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..ai_context import evidence_ai_context, review_ai_context, task_ai_context
from ..cli_support import make_id
from ..display_labels import ai_value_label, bool_label, category_label, field_label, severity_label, status_label
from ..failure import deterministic_id
from ..human_status import human_next_action_text
from ..project_boundary import ProjectBoundaryError, record_nilo_issue_for_task, require_write_fence, resolve_project_boundary
from ..snapshot import commit_aware_evidence_status, compact_snapshot, current_git_snapshot, evidence_status, review_result_status
from ..store import Store
from ..task_analytics import project_task_analytics, task_analytics
from ..task_logic import active_task_completion, completion_status, custom_split_task_specs, projected_task_status, require_ai_completion_evidence, split_task_specs
from ..timeutil import now_iso
from ..transitions import TransitionError, complete_task, invalidate_task_completion
from ..workflow_context import mark_release_commit_recorded


def cmd_task_create(args: argparse.Namespace) -> str:
    from .. import cli as c

    store = Store(args.db)
    try:
        project = store.get("projects", args.project)
        if not project:
            raise SystemExit(f"project not found: {args.project}")
        created_at = now_iso()
        row = {
            "id": args.id or deterministic_id("task", [args.project, args.title, created_at]),
            "project_id": args.project,
            "title": args.title,
            "description": "\n".join(args.description or []),
            "acceptance_criteria": args.acceptance or [],
            "parent_task_id": args.parent_task,
            "split_index": args.split_index,
            "task_type": args.task_type,
            "risk_level": args.risk,
            "requires_understanding_check": args.requires_understanding_check,
            "roadmap_commitment_id": args.commitment or "",
            "roadmap_item_id": args.roadmap_item or "",
            "status": "planned",
            "assigned_model_profile": args.model,
            "degradation_mode": args.degradation,
            "mode": args.mode,
            "base_commit": None,
            "created_at": created_at,
        }
        store.insert("tasks", row)
        if args.model:
            store.insert(
                "model_usage_logs",
                {
                    "id": make_id("model_usage"),
                    "task_id": row["id"],
                    "model_profile_id": args.model,
                    "purpose": "task_assignment",
                    "degradation_mode": args.degradation,
                    "created_at": created_at,
                },
            )
        print(row["id"])
        return row["id"]
    finally:
        store.close()


def cmd_task_start(args: argparse.Namespace) -> None:
    from .facade import cmd_facade_start

    cmd_facade_start(args)


def cmd_task_analytics(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        if args.project:
            data = project_task_analytics(store, args.project, since=args.since)
            if args.format == "json":
                print(json.dumps(data, ensure_ascii=False, indent=2))
                return
            print_project_task_analytics(data)
            return
        data = task_analytics(store, args.task)
        if args.format == "json":
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return
        print_single_task_analytics(data)
    finally:
        store.close()


def print_project_task_analytics(data: dict) -> None:
    summary = data["summary"]
    verification = data["verification"]
    review = data["review"]
    failure = data["failure"]
    task_design = data["task_design"]
    title = f"Task analytics: {data['project_id']}"
    if data.get("scope", {}).get("since"):
        title += f" (since {data['scope']['since']})"
    print(title)
    print()
    print("総評:")
    print(f"- 完了: {summary['completed_count']} / {summary['task_count']}")
    print(f"- 未完了: {summary['open_count']}")
    print(f"- 予約付き完了: {summary['completed_with_reservations_count']}")
    print(f"- 人間確認済み完了: {summary['human_confirmed_completion_count']}")
    print(f"- 検証付き完了: {summary['completed_with_verification_count']}")
    print(f"- レビュー付き完了: {summary['completed_with_review_count']}")
    print(f"- 未解決レビュー指摘あり: {summary['open_blocking_review_finding_task_count']}")
    print(f"- 未解決 failure log あり: {summary['open_failure_task_count']}")
    print(f"- overdrive mode: {summary['overdrive_task_count']}")
    print()
    print("検証:")
    print(f"- run: {verification['run_count']}")
    print(f"- 成功 / 失敗 / timeout: {verification['passed_count']} / {verification['failed_count']} / {verification['timed_out_count']}")
    print(f"- 検証なし完了: {len(verification['completed_without_verification_task_ids'])}")
    print(f"- dirty snapshot: {len(verification['dirty_snapshot_task_ids'])}")
    print_compact_command_list("timeout が多い command", verification["timeout_commands"], "timed_out_count")
    print_compact_command_list("exit_code != 0 が多い command", verification["failed_commands"], "failed_count")
    print_compact_duration_list("同一 command の所要時間", verification.get("duration_commands", []))
    print()
    print("レビュー:")
    print(f"- 依頼 / 結果: {review['request_count']} / {review['result_count']}")
    print(f"- verdict 分布: {format_counts(review['verdict_counts'])}")
    print(f"- severity 分布: {format_counts(review['finding_severity_counts'])}")
    print(f"- blocking / open / resolved findings: {review['blocking_finding_count']} / {review['open_finding_count']} / {review['resolved_finding_count']}")
    print(f"- レビュー後に修正が発生したタスク: {len(review['tasks_with_post_review_updates'])}")
    print()
    print("失敗:")
    print(f"- failure log: {failure['log_count']}")
    print(f"- open / resolved: {failure['open_count']} / {failure['resolved_count']}")
    print(f"- category 分布: {format_counts(failure['category_counts'])}")
    print(f"- severity 分布: {format_counts(failure['severity_counts'])}")
    print(f"- 同一 category が多いタスク: {len(failure['tasks_with_repeated_categories'])}")
    print(f"- 最近の category: {format_counts(failure['recent_category_counts'])}")
    print()
    print("作業設計:")
    print(f"- task_type 分布: {format_counts(task_design['task_type_counts'])}")
    print(f"- risk_level 分布: {format_counts(task_design['risk_level_counts'])}")
    print(f"- roadmap / 単発: {task_design['roadmap_task_count']} / {task_design['standalone_task_count']}")
    print(f"- overdrive mode: {task_design['overdrive_task_count']}")
    print(f"- requires_understanding_check: {task_design['requires_understanding_check_task_count']}")
    print(f"- risk 別 failure: {format_counts(task_design['risk_failure_counts'])}")
    print(f"- risk 別 review finding: {format_counts(task_design['risk_review_finding_counts'])}")


def print_single_task_analytics(data: dict) -> None:
    task = data["task"]
    summary = data["summary"]
    print(f"Task analytics: {task['id']} {task['title']}")
    print()
    print("総評:")
    print(f"- 状態: {task['status']}")
    print(f"- 種別 / リスク / mode: {task['task_type']} / {task['risk_level']} / {task['mode']}")
    print(f"- roadmap_commitment_id: {task['roadmap_commitment_id'] or 'なし'}")
    print(f"- 検証 run: {summary['verification_run_count']}")
    print(f"- レビュー依頼 / 結果 / 指摘: {summary['review_request_count']} / {summary['review_result_count']} / {summary['review_finding_count']}")
    print(f"- failure log: {summary['failure_log_count']} (open {summary['open_failure_log_count']})")
    print(f"- completion / transition: {summary['completion_count']} / {summary['transition_count']}")
    print()
    print("検証:")
    verification = data["verification"]
    print(f"- 成功 / 失敗 / timeout: {verification['passed_count']} / {verification['failed_count']} / {verification['timed_out_count']}")
    print(f"- dirty snapshot: {len(verification['dirty_snapshot_task_ids'])}")
    print_compact_duration_list("同一 command の所要時間", verification.get("duration_commands", []))
    print()
    print("レビュー:")
    review = data["review"]
    print(f"- verdict 分布: {format_counts(review['verdict_counts'])}")
    print(f"- severity 分布: {format_counts(review['finding_severity_counts'])}")
    print(f"- blocking / open / resolved findings: {review['blocking_finding_count']} / {review['open_finding_count']} / {review['resolved_finding_count']}")
    print()
    print("失敗:")
    failure = data["failure"]
    print(f"- open / resolved: {failure['open_count']} / {failure['resolved_count']}")
    print(f"- category 分布: {format_counts(failure['category_counts'])}")
    print(f"- severity 分布: {format_counts(failure['severity_counts'])}")
    print()
    print("完了/遷移:")
    for completion in data["completion"]["completions"] or []:
        reservations = "yes" if completion["completed_with_reservations"] else "no"
        human_confirmed = "yes" if completion["human_confirmed"] else "no"
        print(f"- completion {completion['id']}: actor={completion['actor']} reservations={reservations} human_confirmed={human_confirmed}")
    for transition in data["transitions"][:10]:
        print(f"- transition {transition['transition']}: {transition['previous_state']} -> {transition['new_state']}")


def print_compact_command_list(label: str, commands: list[dict], count_key: str) -> None:
    if not commands:
        print(f"- {label}: なし")
        return
    rendered = ", ".join(f"{item['command']} ({item[count_key]})" for item in commands[:3])
    print(f"- {label}: {rendered}")


def print_compact_duration_list(label: str, commands: list[dict]) -> None:
    if not commands:
        print(f"- {label}: なし")
        return
    rendered_items = []
    for item in commands[:3]:
        note = " timeout短すぎ候補" if item.get("timeout_may_be_short") else ""
        rendered_items.append(
            f"{item['command']} (latest={item['latest_seconds']}s max={item['max_seconds']}s{note})"
        )
    print(f"- {label}: {', '.join(rendered_items)}")


def format_counts(counts: dict) -> str:
    if not counts:
        return "なし"
    return ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def cmd_task_status(args: argparse.Namespace) -> None:
    from .. import cli as c

    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        if getattr(args, "ai", False):
            data = task_ai_context(store, args.task, snapshot_mode="fast")
            if getattr(args, "format", "text") == "json":
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                task_summary = data["task"]
                print(f"{field_label('task')}: {task_summary['id']} {task_summary['title']}")
                print(f"{field_label('status')}: {ai_value_label(task_summary['state'])}")
                print(f"{field_label('evidence')}: {ai_value_label(data['evidence']['status'])}")
                print(f"{field_label('unresolved_review_count')}: {data['review']['unresolved_count']}")
                print(f"{field_label('completion')}: {ai_value_label('completion_allowed' if data['completion']['allowed'] else 'completion_blocked')}")
                print(f"{field_label('blocking_reasons')}:")
                if data["completion"]["blocking_reasons"]:
                    for reason in data["completion"]["blocking_reasons"]:
                        print(f"- {reason}")
                else:
                    print("- なし")
                if data.get("failure_logs"):
                    print(f"{field_label('failure_logs')}:")
                    for failure in data["failure_logs"]:
                        print(f"- [{severity_label(failure['severity'])}] {category_label(failure['category'])}")
                        print(f"  {failure['message']}")
                    print(data["failure_logs_note"])
                print(f"{field_label('next_required_actions')}:")
                for action in data["next_required_actions"] or ["なし"]:
                    print(f"- {human_next_action_text(action)}")
            return
        verification_run = store.latest_for_task("verification_runs", args.task)
        current_snapshot = current_git_snapshot(Path.cwd())
        instruction = store.latest_for_task("instructions", args.task)
        report = store.latest_for_task("agent_reports", args.task)
        quality_review = store.latest_for_task("quality_reviews", args.task)
        review_request = store.latest_for_task("review_requests", args.task)
        review_result = store.latest_for_task("review_results", args.task)
        review_findings = store.list_where("review_findings", "task_id=?", (args.task,))
        recipe_provenance = store.latest_for_task("recipe_task_provenance", args.task)
        print(f"{field_label('id')}: {task['id']}")
        print(f"{field_label('status')}: {status_label(projected_task_status(store, task))}")
        print(f"{field_label('task_type')}: {task['task_type']}")
        print(f"{field_label('risk_level')}: {task['risk_level']}")
        print(f"{field_label('requires_understanding_check')}: {bool_label(bool(task['requires_understanding_check']))}")
        print(f"{field_label('mode')}: {task.get('mode', 'normal')}")
        if recipe_provenance:
            print(f"{field_label('recipe')}: {recipe_provenance['recipe_name']} ({recipe_provenance['source_layer']} layer)")
            print(f"{field_label('recipe_provenance')}: stored for audit")
        if task.get("description"):
            print(f"{field_label('description')}:")
            print(task["description"])
        if task.get("acceptance_criteria"):
            print(f"{field_label('acceptance_criteria')}:")
            for criterion in task["acceptance_criteria"]:
                print(f"- {criterion}")
        print(f"{field_label('base_commit')}: {task['base_commit'] or 'なし'}")
        if instruction:
            print(f"{field_label('latest_instruction')}: {instruction['id']}")
        if report:
            print(f"{field_label('latest_report')}: {report['id']} ({report['claimed_status']})")
        completion = active_task_completion(store, args.task)
        print(f"{field_label('evidence_status')}: {status_label(commit_aware_evidence_status(verification_run, current_snapshot, completion))}")
        if verification_run:
            result = "timed_out" if verification_run["timed_out"] else f"exit_code={verification_run['exit_code']}"
            print(f"{field_label('latest_verification_run')}: {verification_run['id']} ({result})")
            print(f"{field_label('verification_source')}: {verification_run.get('source', 'nilo_executed')}")
            print(f"verification_mode: {verification_run.get('metadata', {}).get('verification_mode', 'targeted')}")
            print(f"{field_label('verification_command')}: {verification_run['command']}")
            print(f"{field_label('verification_working_tree')}: {c.verification_working_tree_summary(verification_run)}")
            for line in c.verification_snapshot_policy_lines(verification_run):
                print(line)
            for file in c.verification_working_tree_state(verification_run)["files"]:
                print(f"- {file}")
            if verification_run["evidence_check_id"]:
                print(f"{field_label('verification_evidence_check')}: {verification_run['evidence_check_id']}")
        if quality_review:
            c.print_quality_review(quality_review)
        if review_request:
            print(f"{field_label('latest_review_request')}: {review_request['id']} ({review_request['status']}) {review_request['requester']} -> {review_request['reviewer']}")
        if review_result:
            print(f"{field_label('latest_review_result')}: {review_result['id']} ({review_result['verdict']}, {review_result_status(review_result, current_snapshot)})")
        if review_findings:
            print(f"{field_label('review_findings')}:")
            for finding in review_findings:
                marker = "ブロック" if finding["blocking"] else "非ブロック"
                location = f" {finding['file_path']}:{finding['line']}" if finding["file_path"] else ""
                print(f"- {finding['id']} [{status_label(finding['status'])}] {severity_label(finding['severity'])} {marker}{location}: {finding['title']}")
        completion_warnings = c.recipe_completion_warnings(store, args.task)
        if completion_warnings:
            print(f"{field_label('completion_warnings')}:")
            for warning in completion_warnings:
                print(f"- {severity_label(warning['severity'])}: {warning['message']}")
        understanding = store.latest_for_task("understanding_checks", args.task)
        if understanding:
            print(f"{field_label('latest_understanding_check')}: {understanding['id']} ({status_label(understanding['status'])})")
        completion = active_task_completion(store, args.task)
        if completion:
            print(f"{field_label('completed_reason')}: {completion['reason']}")
            print(f"{field_label('completed_with_reservations')}: {bool_label(bool(completion.get('completed_with_reservations')))}")
    finally:
        store.close()


def cmd_evidence_show(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        if not store.get("tasks", args.task):
            raise SystemExit(f"task not found: {args.task}")
        data = evidence_ai_context(store, args.task)
        if args.format == "json":
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return
        print(f"{field_label('task_id')}: {args.task}")
        print(f"{field_label('evidence')}: {status_label(data['status'])}")
        print(f"{field_label('verification_run')}: {data['verification_run_id'] or 'なし'}")
        print(f"exit_code: {data['verification_exit_code']}")
        print(f"timed_out: {bool_label(data['verification_timed_out'])}")
    finally:
        store.close()


def cmd_review_show(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        if not store.get("tasks", args.task):
            raise SystemExit(f"task not found: {args.task}")
        data = review_ai_context(store, args.task)
        if args.format == "json":
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return
        print(f"{field_label('task_id')}: {args.task}")
        print(f"{field_label('unresolved_review_count')}: {data['unresolved_count']}")
        print(f"{field_label('unresolved_blocking_count')}: {data['unresolved_blocking_count']}")
        print(f"{field_label('unresolved_findings')}:")
        if data["unresolved_findings"]:
            for finding in data["unresolved_findings"]:
                marker = "ブロック" if finding["blocking"] else "非ブロック"
                location = f" {finding['file_path']}:{finding['line']}" if finding["file_path"] else ""
                print(f"- {finding['id']} {severity_label(finding['severity'])} {marker}{location}: {finding['title']}")
        else:
            print("- なし")
    finally:
        store.close()


def cmd_task_update(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        values = {}
        changes = []
        if args.description is not None:
            values["description"] = "\n".join(args.description)
            changes.append("description")
        if args.acceptance is not None and args.append_acceptance is not None:
            raise SystemExit("use either --acceptance to replace criteria or --append-acceptance to append criteria, not both")
        if args.acceptance is not None:
            values["acceptance_criteria"] = args.acceptance
            changes.append("acceptance_criteria")
        if args.append_acceptance is not None:
            values["acceptance_criteria"] = [*task.get("acceptance_criteria", []), *args.append_acceptance]
            changes.append("acceptance_criteria")
        if not values:
            raise SystemExit("nothing to update; pass --description, --acceptance, or --append-acceptance")
        store.update("tasks", args.task, values)
        updated = store.get("tasks", args.task)
        print(f"task: {updated['id']}")
        print(f"updated: {', '.join(changes)}")
        if "description" in values:
            print("description:")
            print(updated["description"])
        if "acceptance_criteria" in values:
            print("acceptance_criteria:")
            for criterion in updated["acceptance_criteria"]:
                print(f"- {criterion}")
    finally:
        store.close()


def cmd_task_list(args: argparse.Namespace) -> None:
    from ..project_logic import fast_project_tasks_and_recorded_statuses

    store = Store(args.db)
    try:
        project = store.get("projects", args.project)
        if not project:
            raise SystemExit(f"project not found: {args.project}")
        tasks, statuses = fast_project_tasks_and_recorded_statuses(store, args.project)
        for task in reversed(tasks):
            print(
                "\t".join(
                    [
                        task["id"],
                        statuses[task["id"]],
                        task["task_type"],
                        task["risk_level"],
                        task["title"],
                        task["created_at"],
                    ]
                )
            )
    finally:
        store.close()


def cmd_task_complete(args: argparse.Namespace) -> None:
    from .. import cli as c

    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        boundary = resolve_project_boundary(db_path=args.db)
        try:
            require_write_fence(boundary)
        except ProjectBoundaryError as exc:
            record_nilo_issue_for_task(store, task["project_id"], task["id"], "task complete", exc, boundary)
            raise SystemExit(str(exc)) from exc
        try:
            verification_before_completion = store.latest_for_task("verification_runs", args.task)
            snapshot_before_completion = current_git_snapshot(Path.cwd())
            human_acceptance = (getattr(args, "human_acceptance", "") or "").strip()
            human_confirm = bool(getattr(args, "human_confirm", False) or human_acceptance)
            decision_note = (getattr(args, "decision_note", "") or "").strip() or human_acceptance
            if args.actor == "human" and not decision_note:
                raise TransitionError("decision_note_required", "human completion requires --decision-note with the human acceptance note")
            result = complete_task(
                store,
                args.task,
                actor=args.actor,
                reason=args.reason,
                human_confirm=human_confirm,
                decision_source="human_interactive" if args.actor == "human" else "",
                decision_note=decision_note,
                cwd=Path.cwd(),
            )
        except TransitionError as exc:
            raise SystemExit(f"{exc.message}{(': ' + exc.remediation) if exc.remediation else ''}") from exc
        print(f"status: {completion_status(args.actor)}")
        print(f"completed_by: {args.actor}")
        print(f"task_completion: {result.created_ids['task_completion']}")
        closed_commitments = c.auto_close_ready_roadmap_commitments(
            store,
            task["project_id"],
            args.actor,
            f"All linked tasks completed after {args.task}: {args.reason}",
            task.get("roadmap_commitment_id") or None,
        )
        if closed_commitments:
            print("closed_roadmap_commitments:")
            for commitment in closed_commitments:
                print(f"- {commitment['id']} {commitment['title']}")
        completion_warnings = c.recipe_completion_warnings(store, args.task)
        if completion_warnings:
            print("completion_warnings:")
            for warning in completion_warnings:
                print(f"- {warning['severity']}: {warning['message']}")
        changed_files = c.git_changed_files(Path.cwd())
        if changed_files and args.commit:
            message = args.commit_message or f"Complete {task['title']}"
            code, out, err = c.commit_changed_files(Path.cwd(), changed_files, message)
            if code != 0:
                raise SystemExit(err or "git commit failed")
            commit_sha = _git_single_value(["rev-parse", "HEAD"])
            committed_tree_hash = _git_single_value(["rev-parse", "HEAD^{tree}"])
            post_commit_snapshot = current_git_snapshot(Path.cwd())
            completion_id = result.created_ids["task_completion"]
            completion = store.get("task_completions", completion_id)
            completed_snapshot = completion.get("completed_snapshot") or {}
            completed_snapshot["commit_transition"] = {
                "verified_snapshot": compact_snapshot(verification_before_completion or {}),
                "pre_commit_snapshot": compact_snapshot(snapshot_before_completion),
                "post_commit_snapshot": compact_snapshot(post_commit_snapshot),
                "commit_sha": commit_sha,
                "commit_message": message,
                "committed_from_verified_dirty_tree": bool(
                    verification_before_completion
                    and compact_snapshot(verification_before_completion) == compact_snapshot(snapshot_before_completion)
                    and not post_commit_snapshot.get("working_tree_dirty")
                ),
                "verified_diff_hash": (verification_before_completion or {}).get("git_diff_hash", ""),
                "committed_tree_hash": committed_tree_hash,
            }
            store.update("task_completions", completion_id, {"completed_snapshot": completed_snapshot})
            mark_release_commit_recorded(
                store,
                task_id=args.task,
                commit_sha=commit_sha,
                commit_message=message,
                post_commit_snapshot=compact_snapshot(post_commit_snapshot),
            )
            print("commit: created")
            if out:
                print(out)
        elif changed_files:
            print("next_actions:")
            print("- commit accepted changes")
            print(f"- suggested command: git add {' '.join(changed_files)} && git commit -m \"Complete {task['title']}\"")
    finally:
        store.close()


def _git_single_value(args: list[str]) -> str:
    from ..gitmeta import git_output

    code, out, _ = git_output(args, Path.cwd())
    return out.strip() if code == 0 else ""


def cmd_task_completion_invalidate(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        completion = store.get("task_completions", args.completion)
        if not completion:
            raise SystemExit(f"task completion not found: {args.completion}")
        if completion.get("invalidated_at"):
            raise SystemExit(f"task completion already invalidated: {args.completion}")
        task = store.get("tasks", completion["task_id"])
        if not task:
            raise SystemExit(f"task not found for completion: {completion['task_id']}")
        boundary = resolve_project_boundary(db_path=args.db)
        try:
            require_write_fence(boundary)
        except ProjectBoundaryError as exc:
            record_nilo_issue_for_task(store, task["project_id"], task["id"], "task completion invalidate", exc, boundary)
            raise SystemExit(str(exc)) from exc
        try:
            invalidate_task_completion(store, args.completion, actor=args.actor, reason=args.reason)
        except TransitionError as exc:
            raise SystemExit(f"{exc.message}{(': ' + exc.remediation) if exc.remediation else ''}") from exc
        print(f"status: invalidated")
        print(f"task_completion: {args.completion}")
    finally:
        store.close()


def cmd_task_split(args: argparse.Namespace) -> None:
    from .. import cli as c

    store = Store(args.db)
    try:
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        specs = custom_split_task_specs(task, args.child) if args.child else split_task_specs(task)
        if not specs:
            raise SystemExit("at least one non-empty --child value is required")
        print("Generated subtasks:")
        for index, (task_type, title) in enumerate(specs, start=1):
            row = {
                "id": make_id("task"),
                "project_id": task["project_id"],
                "title": title,
                "description": task["description"] if args.child else "",
                "acceptance_criteria": task["acceptance_criteria"] if args.child else [],
                "parent_task_id": task["id"],
                "split_index": index,
                "task_type": task_type,
                "risk_level": task["risk_level"],
                "requires_understanding_check": task_type == "implementation" and task["risk_level"] == "high",
                "roadmap_commitment_id": task.get("roadmap_commitment_id", ""),
                "roadmap_item_id": task.get("roadmap_item_id", ""),
                "status": "planned",
                "assigned_model_profile": task["assigned_model_profile"],
                "degradation_mode": task["degradation_mode"],
                "mode": task.get("mode", "normal"),
                "base_commit": None,
                "created_at": now_iso(),
            }
            store.insert("tasks", row)
            code_policy = "Code changes: forbidden" if task_type in ("research", "design", "review", "verification") else "Code changes: allowed"
            print(f"{index}. {task_type}")
            print(f"   id: {row['id']}")
            print(f"   {title}")
            print(f"   {code_policy}")
    finally:
        store.close()
