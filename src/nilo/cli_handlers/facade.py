from __future__ import annotations

import argparse
import io
import json
import re
import shlex
import time
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from ..ai_context import AI_CONTEXT_TEXT_MAX_CHARS, project_ai_context, render_ai_context_text
from ..display_labels import bool_label, field_label, status_label
from ..failure import deterministic_id
from ..human_status import human_next_action_text
from ..open_state import open_state_detector
from ..project_logic import fast_project_tasks_and_recorded_statuses, fast_unfinished_verification_targets
from ..project_language import human_gate_texts
from ..project_boundary import (
    ProjectBoundaryError,
    assert_self_development_allowed,
    boundary_warning_lines,
    resolve_project_boundary,
)
from ..snapshot import commit_aware_evidence_status, current_git_snapshot_fast, current_git_snapshot_full
from ..store import Store
from ..task_logic import active_task_completion, completion_audit_issues, is_task_completed_status, projected_task_status
from ..timeutil import now_iso
from ..transitions import TransitionError, cancel_task
from ..workflow_context import workflow_context
from .task import cmd_task_complete, cmd_task_create
from .workflow import cmd_outcome_record, cmd_report_import, cmd_verification_run

QUEUE_TODO_STATUSES = {"open", "ready", "triaged", "blocked", "requires_roadmap", "deferred"}
NEXT_TODO_STATUS_PRIORITY = {"ready": 0, "requires_roadmap": 1}
WORK_RECIPE_KEYWORDS = {
    "docs-update": ("readme", "docs", "document", "ドキュメント", "説明", "文言"),
    "bugfix": ("bug", "fix", "failing", "error", "exception", "バグ", "不具合", "失敗", "エラー", "直して"),
    "perf": ("slow", "heavy", "performance", "perf", "timeout", "full check", "遅い", "重い", "高速化", "計測", "ボトルネック"),
    "basic-design": ("design", "architecture", "plan", "設計", "方針", "検討", "実装前"),
}
WORK_RELEASE_INTENT_PATTERNS = (
    r"\brelease\s+(prep|prepare|preparation|flow|process|run)\b",
    r"\bprepare\s+(a\s+)?release\b",
    r"\bstart\s+(the\s+)?release\b",
    r"\brun\s+(the\s+)?release\b",
    r"\bpublish\s+(the\s+)?release\b",
    r"\brelease\s+(v?\d+(?:\.\d+){1,2}|this|it|now)\b",
    r"\b(v?\d+(?:\.\d+){1,2})\s*(を|の)?\s*(リリース|公開)",
    r"リリースレシピ(を|の)?(準備|始め|実行)",
    r"リリース(準備|作業|フロー|を始め|実行)",
    r"リリースを(始め|実行)",
    r"公開(準備|作業|フロー)",
)
WORK_RELEASE_META_PATTERNS = (
    r"\brelease\s+(recipe|note|notes|validation|bug|fix|task)\b",
    r"\b(recipe|note|notes|validation|bug|fix|task)\s+.*\brelease\b",
    r"(release|リリース).*(\bbug\b|\bfix\b|バグ|不具合|修正|起票|タスクを作|タスク作成|validation)",
    r"(release\s+note|リリースノート).*(空|template|テンプレート)",
)
WORK_RECIPE_ALIASES = {
    "docs": "docs-update",
    "document": "docs-update",
    "design": "basic-design",
}
WORK_PATH_TOKEN_PATTERN = re.compile(r"(?<![A-Za-z0-9_.-])(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+")
WORK_DOCUMENT_PATH_UPDATE_SUFFIX = re.compile(r"^(?:を|の)?(?:更新|編集|追記|整理|書き換え|修正)")
WORK_DOCUMENT_PATH_UPDATE_PREFIX = re.compile(r"(?:\bupdate|\bedit|\brevise)\s+(?:the\s+)?$")
WORK_DOCUMENT_PATH_SUFFIXES = (".md", ".mdx", ".rst", ".adoc", ".txt")

def read_only_work_route(request: str) -> dict[str, str] | None:
    """Route a caller-declared inspection without inferring intent from domain words."""
    normalized = " ".join(request.strip().lower().split())
    if not normalized:
        return None
    return {"kind": "inspection", "command": ""}
WORK_RECIPE_ACCEPTANCE = {
    "docs-update": [
        "対象読者が明確である",
        "冗長な説明が減っている",
        "主要導線が先に読める",
        "リンク切れや古い導線がない",
    ],
    "bugfix": [
        "再現条件または失敗条件が記録されている",
        "原因と修正内容が説明されている",
        "再発防止テストまたは代替検証がある",
        "関連範囲への副作用確認がある",
    ],
    "perf": [
        "改善対象が明確である",
        "改善前の体感または測定値がある",
        "ボトルネック仮説が記録されている",
        "改善後の再測定結果がある",
        "正しさ検証が通っている",
    ],
    "basic-design": [
        "目的と制約が明確である",
        "採用方針と代替案が記録されている",
        "実装前に必要な未決事項が分離されている",
    ],
    "release": [
        "公開操作は明示承認まで実行しない",
        "リリース対象とバージョン判断が記録されている",
        "公開前に必要な検証が通っている",
    ],
}


def infer_work_recipe_candidate(request: str, *, explicit_recipe: str | None = None, no_recipe: bool = False) -> dict[str, Any]:
    if no_recipe:
        return {"recipe": "", "confidence": "disabled", "reason": "--no-recipe", "ambiguous_candidates": []}
    if explicit_recipe:
        explicit_recipe = WORK_RECIPE_ALIASES.get(explicit_recipe, explicit_recipe)
        return {"recipe": explicit_recipe, "confidence": "explicit", "reason": "--recipe", "ambiguous_candidates": []}
    lowered = request.lower()
    document_path_update = _work_document_path_is_update_target(lowered)
    keyword_source = WORK_PATH_TOKEN_PATTERN.sub(" ", lowered)
    matches = []
    for recipe, keywords in WORK_RECIPE_KEYWORDS.items():
        hits = [keyword for keyword in keywords if _work_keyword_matches(keyword_source, keyword)]
        if hits:
            matches.append({"recipe": recipe, "score": len(hits), "reason": ", ".join(hits[:3])})
    if document_path_update and not any(match["recipe"] == "docs-update" for match in matches):
        matches.append({"recipe": "docs-update", "score": 1, "reason": "document path update"})
    release_reason = _work_release_intent_reason(lowered)
    if release_reason:
        matches.append({"recipe": "release", "score": 2, "reason": release_reason})
    if not matches:
        return {"recipe": "", "confidence": "none", "reason": "no clear recipe keyword", "ambiguous_candidates": []}
    matches.sort(key=lambda item: (-item["score"], item["recipe"]))
    top_score = matches[0]["score"]
    tied = [item for item in matches if item["score"] == top_score]
    if len(tied) > 1:
        return {"recipe": "", "confidence": "ambiguous", "reason": "multiple recipe candidates", "ambiguous_candidates": tied}
    return {
        "recipe": matches[0]["recipe"],
        "confidence": "high",
        "reason": f"matched: {matches[0]['reason']}",
        "ambiguous_candidates": [],
    }


def work_acceptance_for_recipe(recipe: str) -> list[str]:
    return WORK_RECIPE_ACCEPTANCE.get(recipe, ["依頼内容が満たされている", "変更内容と検証結果が記録されている"])


def _work_keyword_matches(lowered_request: str, keyword: str) -> bool:
    lowered_keyword = keyword.lower()
    if lowered_keyword.isascii() and lowered_keyword.replace(" ", "").isalpha():
        return re.search(rf"(?<![a-z0-9_-]){re.escape(lowered_keyword)}(?![a-z0-9_-])", lowered_request) is not None
    return lowered_keyword in lowered_request


def _work_document_path_is_update_target(lowered_request: str) -> bool:
    for match in WORK_PATH_TOKEN_PATTERN.finditer(lowered_request):
        path_token = match.group(0)
        if not (path_token.startswith("docs/") or path_token.endswith(WORK_DOCUMENT_PATH_SUFFIXES)):
            continue
        if WORK_DOCUMENT_PATH_UPDATE_SUFFIX.search(lowered_request[match.end() :]):
            return True
        if WORK_DOCUMENT_PATH_UPDATE_PREFIX.search(lowered_request[: match.start()]):
            return True
    return False


def _work_release_intent_reason(lowered_request: str) -> str:
    if not any(keyword in lowered_request for keyword in ("release", "publish", "version", "リリース", "公開", "バージョン")):
        return ""
    if any(re.search(pattern, lowered_request) for pattern in WORK_RELEASE_META_PATTERNS):
        return ""
    for pattern in WORK_RELEASE_INTENT_PATTERNS:
        if re.search(pattern, lowered_request):
            return "explicit release intent"
    return ""


def default_project_id(args: argparse.Namespace) -> str:
    return args.project or Path.cwd().name


def active_tasks_for_project(store: Store, project_id: str) -> tuple[list[dict], dict[str, str]]:
    from .. import project_logic as p

    tasks, statuses = fast_project_tasks_and_recorded_statuses(store, project_id)
    active_tasks, _ = p.roadmap_prioritized_project_active_tasks(store, project_id, tasks, statuses)
    return active_tasks, statuses


def first_active_task_for_project(store: Store, project_id: str) -> dict | None:
    active_tasks, _ = _fast_active_tasks_and_statuses(store, project_id, limit=1)
    return active_tasks[0] if active_tasks else None


def unresolved_project_state(store: Store, project_id: str, *, verbose: bool = False) -> dict:
    return open_state_detector(store, project_id, verbose=verbose)


def first_next_todo_for_project(store: Store, project_id: str) -> dict | None:
    todos = [
        item
        for item in store.list_where("todos", "project_id=?", (project_id,))
        if item["status"] in NEXT_TODO_STATUS_PRIORITY
    ]
    if not todos:
        return None
    return sorted(todos, key=lambda item: (NEXT_TODO_STATUS_PRIORITY[item["status"]], item["created_at"], item["id"]))[0]


def roadmap_attention_summary_for_project(store: Store, project_id: str) -> dict:
    from .. import project_logic as p
    return p.roadmap_attention_summary(store, project_id)


def print_roadmap_attention_summary(summary: dict) -> None:
    items = [item for item in summary.get("items", []) if item.get("evidence_attention_items")]
    if not items:
        return
    print()
    print("完了済みロードマップに証跡注意があります。")
    for item in items:
        print(f"- {item['title']}")
        print(f"  - 作業タスク: {item.get('work_task_label', item['implementation_task_label'])}")
        print(f"  - 注意: {'、'.join(item['evidence_attention_items'])}")
    print()
    print("次に判断すること:")
    print("- 証跡を確認して閉じる")
    print("- 追加検証を実行する")


def work_queue_data(store: Store, project_id: str, *, audit: bool = False, verbose: bool = False) -> dict:
    from .. import cli as c

    project = store.get("projects", project_id)
    if not project:
        raise SystemExit(f"project not found: {project_id}")
    tasks = []
    completion_audit_tasks = []
    snapshot = current_git_snapshot_full(Path.cwd()) if audit else None
    task_rows, recorded_statuses = fast_project_tasks_and_recorded_statuses(store, project_id)
    for task in task_rows:
        status = c.projected_task_status(store, task, current_snapshot=snapshot) if audit else recorded_statuses[task["id"]]
        if c.is_task_closed_status(status):
            if audit:
                audit_issues = completion_audit_issues(store, task, current_snapshot=snapshot)
                if not audit_issues:
                    continue
                completion = active_task_completion(store, task["id"])
                completion_audit_tasks.append(
                    {
                        "id": task["id"],
                        "title": task["title"],
                        "status": "invalid_completion",
                        "projected_status": status,
                        "task_type": task["task_type"],
                        "risk_level": task["risk_level"],
                        "completion_id": completion["id"] if completion else "",
                        "audit_issues": audit_issues,
                    }
                )
            continue
        tasks.append(
            {
                "id": task["id"],
                "title": task["title"],
                "status": status,
                "task_type": task["task_type"],
                "risk_level": task["risk_level"],
            }
        )
    if audit:
        tasks.extend(completion_audit_tasks)
    todos = []
    for todo in store.list_where("todos", "project_id=?", (project_id,)):
        if todo["status"] not in QUEUE_TODO_STATUSES:
            continue
        todos.append(
            {
                "id": todo["id"],
                "title": todo["title"],
                "status": todo["status"],
                "priority": todo["priority"],
                "kind": todo["kind"],
            }
        )
    unresolved = unresolved_project_state(store, project_id, verbose=verbose) if (audit or verbose) else {}
    unresolved_total = sum(value for key, value in unresolved.items() if isinstance(value, int))
    return {
        "project_id": project_id,
        "project_name": project["name"],
        "counts": {
            "tasks": len(tasks),
            "todos": len(todos),
            "total": len(tasks) + len(todos),
            "completion_audit_tasks": len(completion_audit_tasks),
            "unresolved_project_state": unresolved_total,
        },
        "tasks": tasks,
        "todos": todos,
        "completion_audit_tasks": completion_audit_tasks,
        "unresolved_project_state": unresolved,
    }


def no_active_task_recovery_message(project_id: str) -> str:
    return (
        f"active task not found for project: {project_id}. "
        "Before implementation, create or select a Nilo task. "
        f'For a concrete implementation request, run `nilo work "<user request>" --project {project_id}`, '
        "then rerun `nilo check --task <task_id> \"...\"`."
    )


def multiple_active_tasks_recovery_message(project_id: str, active_tasks: list[dict]) -> str:
    sorted_tasks = sorted(active_tasks, key=lambda task: task["id"])
    ids = ", ".join(task["id"] for task in sorted_tasks[:5])
    suffix = "" if len(active_tasks) <= 5 else f", ... ({len(active_tasks)} total)"
    example = sorted_tasks[0]["id"] if sorted_tasks else "<task_id>"
    return (
        f"multiple active tasks for project: {project_id}. "
        "nilo check refuses to guess because verification evidence must be attached to exactly one task. "
        f"Pass `--task <task_id>` to record this verification on the intended task: {ids}{suffix}. "
        f"Example: `nilo check --task {example} \"...\"`. "
        "If this command is not evidence for any active task, do not attach it to an unrelated task; "
        "run it outside `nilo check` or create/select the correct task first."
    )


def resolve_task_id(args: argparse.Namespace, store: Store) -> str:
    if getattr(args, "task", None):
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        return args.task

    project_id = default_project_id(args)
    project = store.get("projects", project_id)
    if not project:
        raise SystemExit(f"project not found: {project_id}")
    active_tasks, _ = active_tasks_for_project(store, project_id)
    if not active_tasks:
        raise SystemExit(no_active_task_recovery_message(project_id))
    if len(active_tasks) > 1:
        raise SystemExit(multiple_active_tasks_recovery_message(project_id, active_tasks))
    return active_tasks[0]["id"]


def _compact_task_rows(tasks: list[dict]) -> list[dict[str, str]]:
    return [{"id": task["id"], "title": task["title"], "status": task.get("status", "")} for task in sorted(tasks, key=lambda row: row["id"])]


def _work_stop_payload(reason: str, *, project_id: str, next_commands: list[str] | None = None, tasks: list[dict] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "stopped", "reason": reason, "project_id": project_id, "next": next_commands or []}
    if tasks is not None:
        payload["tasks"] = _compact_task_rows(tasks)
    return payload


def print_work_payload(payload: dict[str, Any], *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if payload["status"] == "stopped":
        print(f"stopped: {payload['reason']}")
        if payload.get("tasks"):
            print("choose target task:")
            for task in payload["tasks"]:
                print(f"- {task['id']} [{status_label(task.get('status', ''))}] {task['title']}")
        if payload.get("next"):
            print("next:")
            for command in payload["next"]:
                print(f"- {command}")
        return
    print(f"work_session: {payload['task_id']}")
    print(f"project: {payload['project_id']}")
    print(f"status: {payload['status']}")
    print(f"task: {payload['task_title']}")
    recipe = payload.get("recipe") or "none"
    print(f"recipe: {recipe}")
    if payload.get("recipe_reason"):
        print(f"recipe_reason: {payload['recipe_reason']}")
    print("objective:")
    print(f"- {payload['request']}")
    print("acceptance:")
    for item in payload.get("acceptance", []):
        print(f"- {item}")
    if payload.get("recommended_check"):
        print("recommended_check:")
        print(f"- {payload['recommended_check']}")
    print("stop_if:")
    for item in payload.get("stop_if", []):
        print(f"- {item}")
    if payload.get("next"):
        print("next:")
        for command in payload["next"]:
            print(f"- {command}")


def cmd_facade_work(args: argparse.Namespace) -> None:
    project_id = default_project_id(args)
    request = (getattr(args, "request", "") or "").strip()
    if not request and not getattr(args, "check", None):
        raise SystemExit('request required: nilo work "<user request>"')
    boundary = resolve_project_boundary(db_path=args.db)
    store = Store(args.db)
    try:
        project = store.get("projects", project_id)
        if not project:
            payload = _work_stop_payload("project_not_found", project_id=project_id, next_commands=[f"nilo init --project {project_id}"])
            payload["project_boundary"] = boundary.to_dict()
            print_work_payload(payload, as_json=bool(getattr(args, "json", False)))
            return
        read_only_route = None
        explicit_work_option = bool(
            getattr(args, "task", None)
            or getattr(args, "check", None)
            or getattr(args, "recipe", None)
            or getattr(args, "no_recipe", False)
        )
        declared_intent = getattr(args, "intent", "") or ""
        if declared_intent == "inspect" and explicit_work_option:
            raise SystemExit("--intent inspect cannot be combined with --task, --recipe, --no-recipe, or --check")
        inspect_request = declared_intent == "inspect" or (not declared_intent and not explicit_work_option)
        if inspect_request:
            read_only_route = read_only_work_route(request)
        if read_only_route:
            payload = {
                "status": "read_only",
                "project_id": project_id,
                "request": request,
                "route": read_only_route["kind"],
                "next": [f"{read_only_route['command']} --project {project_id}"] if read_only_route["command"] else [],
                "project_boundary": boundary.to_dict(),
            }
            if boundary.should_print_text() and not getattr(args, "json", False):
                for line in boundary.text_lines():
                    print(line)
                print()
            if getattr(args, "json", False):
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print("status: read_only")
                print(f"route: {payload['route']}")
                print("task_created: false")
                print("request_preserved: true")
                if payload["next"]:
                    print("next:")
                    print(f"- {payload['next'][0]}")
            return
        workflow = workflow_context(store, project_id)
        if workflow.get("type") == "recipe_run":
            payload = _work_stop_payload(
                f"blocked_workflow:{workflow.get('status')}",
                project_id=project_id,
                next_commands=[f"nilo next --project {project_id} --verbose"],
            )
            payload["active_recipe"] = workflow.get("recipe_name", "")
            payload["project_boundary"] = boundary.to_dict()
            print_work_payload(payload, as_json=bool(getattr(args, "json", False)))
            return
        from ..work_projection import NextActionCode, project_work_projection

        projection = project_work_projection(
            store,
            project_id,
            current_snapshot=current_git_snapshot_full(Path.cwd()),
        )
        requested_task_id = getattr(args, "task", None)
        blocking_projection_actions = {
            NextActionCode.REASSESS_STATE,
            NextActionCode.RESOLVE_BLOCKER,
            NextActionCode.RESOLVE_REVIEW_FINDINGS,
            NextActionCode.WAIT_FOR_REVIEW,
            NextActionCode.RUN_VERIFICATION,
            NextActionCode.RERUN_VERIFICATION,
        }
        projection_blocks_new_work = bool(
            request
            and projection.active_task_id
            and requested_task_id != projection.active_task_id
            and projection.next_action.code in blocking_projection_actions
        )
        explicitly_skippable_roadmap_evidence = bool(
            request
            and not projection.active_task_id
            and projection.blocker
            and projection.blocker.code in {"roadmap_evidence_attention", "roadmap_evidence_incomplete"}
        )
        state_blocker_stops_work = bool(
            projection.next_action.code in {NextActionCode.REASSESS_STATE, NextActionCode.RESOLVE_BLOCKER}
            and not explicitly_skippable_roadmap_evidence
        )
        if projection_blocks_new_work or state_blocker_stops_work:
            payload = _work_stop_payload(
                f"work_projection:{projection.next_action.code.value}",
                project_id=project_id,
                next_commands=list(projection.next_action.command_hint),
            )
            payload["work_projection"] = projection.to_dict()
            payload["project_boundary"] = boundary.to_dict()
            print_work_payload(payload, as_json=bool(getattr(args, "json", False)))
            return
        recipe = infer_work_recipe_candidate(
            request,
            explicit_recipe=getattr(args, "recipe", None),
            no_recipe=bool(getattr(args, "no_recipe", False)),
        )
        if recipe["confidence"] == "ambiguous":
            quoted_request = shlex.quote(request)
            next_commands = [f"nilo work --recipe {item['recipe']} {quoted_request}" for item in recipe["ambiguous_candidates"]]
            next_commands.append(f"nilo work --no-recipe {quoted_request}")
            payload = _work_stop_payload("ambiguous_recipe", project_id=project_id, next_commands=next_commands)
            payload["recipe_candidates"] = recipe["ambiguous_candidates"]
            payload["project_boundary"] = boundary.to_dict()
            print_work_payload(payload, as_json=bool(getattr(args, "json", False)))
            return
        active_tasks, statuses = active_tasks_for_project(store, project_id)
        task_id = getattr(args, "task", None)
        created = False
        if task_id:
            task = store.get("tasks", task_id)
            if not task:
                raise SystemExit(f"task not found: {task_id}")
            if task["project_id"] != project_id:
                payload = _work_stop_payload(
                    "task_project_mismatch",
                    project_id=project_id,
                    next_commands=[f"nilo work --project {task['project_id']} --task {task_id} {shlex.quote(request or task['title'])}"],
                )
                payload["task_project_id"] = task["project_id"]
                payload["project_boundary"] = boundary.to_dict()
                print_work_payload(payload, as_json=bool(getattr(args, "json", False)))
                return
            task_status = statuses.get(task_id, task["status"])
            if is_task_completed_status(task_status):
                payload = _work_stop_payload(
                    "task_already_completed",
                    project_id=project_id,
                    next_commands=[f"nilo work {shlex.quote(request or '<user request>')} --project {project_id}"],
                )
                payload["task_id"] = task_id
                payload["task_status"] = task_status
                payload["project_boundary"] = boundary.to_dict()
                print_work_payload(payload, as_json=bool(getattr(args, "json", False)))
                return
        elif len(active_tasks) == 1 and not request:
            task = active_tasks[0]
            task_id = task["id"]
        elif len(active_tasks) > 1 and not request:
            for active_task in active_tasks:
                active_task["status"] = statuses.get(active_task["id"], active_task["status"])
            payload = _work_stop_payload(
                "multiple_active_tasks",
                project_id=project_id,
                next_commands=[f"nilo instruct --task {active_task['id']}" for active_task in active_tasks],
                tasks=active_tasks,
            )
            payload["project_boundary"] = boundary.to_dict()
            print_work_payload(payload, as_json=bool(getattr(args, "json", False)))
            return
        else:
            if not request:
                payload = _work_stop_payload(
                    "request_required_without_active_task",
                    project_id=project_id,
                    next_commands=[f'nilo work "<user request>" --project {project_id} --check {shlex.quote(args.check)}'],
                )
                payload["project_boundary"] = boundary.to_dict()
                print_work_payload(payload, as_json=bool(getattr(args, "json", False)))
                return
            task_id = deterministic_id("task", [project_id, request, now_iso()])
            acceptance = ["依頼内容が満たされている", "変更内容と検証結果が記録されている"]
            if recipe["recipe"]:
                acceptance = [f"recipe: {recipe['recipe']}", f"recipe_reason: {recipe['reason']}", *work_acceptance_for_recipe(recipe["recipe"])]
            task = {
                "id": task_id,
                "project_id": project_id,
                "title": request[:80],
                "description": request,
                "acceptance_criteria": acceptance,
                "status": "planned",
            }
            if not getattr(args, "dry_run", False):
                create_args = argparse.Namespace(
                    db=args.db,
                    project=project_id,
                    title=task["title"],
                    description=[request],
                    acceptance=acceptance,
                    id=task_id,
                    parent_task=None,
                    split_index=None,
                    commitment="",
                    roadmap_item="",
                    model="",
                    degradation="normal",
                    mode="normal",
                    task_type="implementation",
                    risk="medium",
                    requires_understanding_check=False,
                )
                with redirect_stdout(io.StringIO()):
                    cmd_task_create(create_args)
                created = True
        gate_texts = human_gate_texts(project, Path.cwd())
        payload = {
            "status": "dry_run" if getattr(args, "dry_run", False) else ("created" if created else "ready"),
            "project_id": project_id,
            "task_id": task_id,
            "task_title": task["title"],
            "request": request or task["title"],
            "recipe": recipe["recipe"],
            "recipe_confidence": recipe["confidence"],
            "recipe_reason": recipe["reason"],
            "acceptance": task.get("acceptance_criteria") or work_acceptance_for_recipe(recipe["recipe"]),
            "recommended_check": getattr(args, "check", "") or "",
            "stop_if": [
                gate_texts["public_operation_required"],
                gate_texts["destructive_change_required"],
                gate_texts["verification_fails"],
                gate_texts["human_acceptance_required"],
            ],
            "next": [
                f"nilo instruct --task {task_id}",
                f'nilo check --task {task_id} "<verification command>"',
                f"nilo done --task {task_id} --actor ai --reason \"work session complete\"",
            ],
            "project_boundary": boundary.to_dict(),
        }
    finally:
        store.close()

    if getattr(args, "dry_run", False) or not getattr(args, "check", None):
        if boundary.should_print_text() and not getattr(args, "json", False):
            for line in boundary.text_lines():
                print(line)
            for warning in boundary_warning_lines(boundary):
                print(warning)
            print()
        print_work_payload(payload, as_json=bool(getattr(args, "json", False)))
        return

    if boundary.should_print_text() and not getattr(args, "json", False):
        for line in boundary.text_lines():
            print(line)
        for warning in boundary_warning_lines(boundary):
            print(warning)
        print()
    if not getattr(args, "json", False):
        print_work_payload(payload, as_json=False)

    check_stdout = io.StringIO()
    check_args = argparse.Namespace(
        db=args.db,
        project=project_id,
        task=task_id,
        command=args.check,
        mode=args.mode,
        snapshot="full" if getattr(args, "audit", False) else args.snapshot,
        timeout=args.timeout,
    )
    if getattr(args, "json", False):
        with redirect_stdout(check_stdout):
            cmd_facade_check(check_args)
    else:
        cmd_facade_check(check_args)
    store = Store(args.db)
    try:
        verification = store.latest_for_task("verification_runs", task_id)
        verification_failed = not verification or int(verification["exit_code"]) != 0 or bool(verification["timed_out"])
        if verification:
            payload["verification"] = {
                "id": verification["id"],
                "exit_code": verification["exit_code"],
                "timed_out": bool(verification["timed_out"]),
                "mode": args.mode,
                "snapshot": verification.get("metadata", {}).get("snapshot_mode", check_args.snapshot),
            }
    finally:
        store.close()
    if getattr(args, "json", False):
        payload["verification_output"] = check_stdout.getvalue().splitlines()
    if verification_failed:
        payload["status"] = "stopped"
        payload["reason"] = "verification_failed"
        if getattr(args, "json", False):
            print_work_payload(payload, as_json=True)
        else:
            print("stopped: verification_failed")
        return
    if not getattr(args, "no_done", False):
        done_args = argparse.Namespace(
            db=args.db,
            project=project_id,
            task=task_id,
            reason="work session complete",
            actor="ai",
            human_confirm=False,
            decision_note="",
            human_acceptance="",
            commit=False,
            commit_message=None,
        )
        done_stdout = io.StringIO()
        if getattr(args, "json", False):
            with redirect_stdout(done_stdout):
                cmd_facade_done(done_args)
            payload["completion_output"] = done_stdout.getvalue().splitlines()
        else:
            cmd_facade_done(done_args)
        payload["completion_recorded"] = True
    else:
        payload["completion_recorded"] = False
    if getattr(args, "json", False):
        print_work_payload(payload, as_json=True)


def explicit_check_task_warning(store: Store, task: dict) -> str:
    status = fast_project_tasks_and_recorded_statuses(store, task["project_id"])[1].get(task["id"], task["status"])
    if is_task_completed_status(status):
        return f"warning: recording verification on completed task {task['id']} ({status_label(status)}) because --task was explicit"
    return ""


def resolve_check_task_id(args: argparse.Namespace, store: Store) -> tuple[str, str]:
    if getattr(args, "task", None):
        task = store.get("tasks", args.task)
        if not task:
            raise SystemExit(f"task not found: {args.task}")
        return args.task, explicit_check_task_warning(store, task)

    project_id = default_project_id(args)
    project = store.get("projects", project_id)
    if not project:
        raise SystemExit(f"project not found: {project_id}")
    candidates = fast_unfinished_verification_targets(store, project_id)
    if not candidates:
        raise SystemExit(no_active_task_recovery_message(project_id))
    if len(candidates) > 1:
        raise SystemExit(multiple_active_tasks_recovery_message(project_id, candidates))
    return candidates[0]["id"], ""


def summary_for_project(store: Store, project_id: str) -> dict:
    from .. import cli as c

    project = store.get("projects", project_id)
    if not project:
        raise SystemExit(f"project not found: {project_id}")
    tasks, statuses, snapshot = c.project_tasks_statuses_and_snapshot(store, project_id)
    return c.project_summary_data(store, project, tasks, statuses, current_snapshot=snapshot)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _short_command(command: str, limit: int = 80) -> str:
    command = " ".join((command or "").split())
    if len(command) <= limit:
        return command
    return command[: limit - 3] + "..."


def _fast_evidence_status(verification_run: dict | None, snapshot: dict[str, Any]) -> str:
    if not verification_run:
        return "missing"
    if verification_run.get("timed_out") or verification_run.get("exit_code") not in (0, "0"):
        return "failed"
    if not snapshot.get("git_available", True):
        return "not checked in fast status"
    return "recorded"


def _fast_todo_counts(store: Store, project_id: str) -> dict[str, int]:
    rows = store.conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM todos
        WHERE project_id=? AND status IN ('ready', 'open')
        GROUP BY status
        """,
        (project_id,),
    ).fetchall()
    counts = {"ready": 0, "open": 0}
    counts.update({row["status"]: row["count"] for row in rows})
    return counts


def _fast_active_tasks_and_statuses(store: Store, project_id: str, *, limit: int = 3) -> tuple[list[dict[str, Any]], dict[str, str]]:
    from .. import project_logic as p

    tasks, statuses = fast_project_tasks_and_recorded_statuses(store, project_id)
    active_tasks = p.roadmap_prioritized_active_tasks(
        tasks,
        statuses,
        p.accepted_roadmap_commitments(store, project_id),
    )
    return active_tasks[:limit], statuses


def fast_status_data(store: Store, project_id: str, *, cwd: Path | None = None) -> dict[str, Any]:
    """Build the lightweight human status payload.

    This deliberately avoids full snapshots, roadmap assessment, commit mapping,
    history, failure-log summaries, and projected status audits.
    """
    timings: dict[str, float] = {}
    total_started = time.perf_counter()

    started = time.perf_counter()
    project = store.get("projects", project_id)
    timings["project_lookup_ms"] = _elapsed_ms(started)
    if not project:
        raise SystemExit(f"project not found: {project_id}")

    started = time.perf_counter()
    active_tasks, statuses = _fast_active_tasks_and_statuses(store, project_id, limit=3)
    timings["fast_task_query_ms"] = _elapsed_ms(started)

    started = time.perf_counter()
    snapshot = current_git_snapshot_fast(cwd or Path.cwd())
    timings["git_fast_status_ms"] = _elapsed_ms(started)

    started = time.perf_counter()
    task_summaries = []
    for task in active_tasks:
        verification_run = store.latest_for_task("verification_runs", task["id"])
        task_summaries.append(
            {
                "id": task["id"],
                "title": task["title"],
                "status": statuses[task["id"]],
                "task_type": task["task_type"],
                "risk_level": task["risk_level"],
                "latest_verification_run": {
                    "exit_code": verification_run["exit_code"] if verification_run else None,
                    "timed_out": bool(verification_run["timed_out"]) if verification_run else False,
                    "command": _short_command(verification_run["command"]) if verification_run else "",
                },
                "evidence": _fast_evidence_status(verification_run, snapshot),
            }
        )
    timings["verification_lookup_ms"] = _elapsed_ms(started)

    started = time.perf_counter()
    task_ids = [task["id"] for task in active_tasks]
    blocking_counts: dict[str, int] = {}
    if task_ids:
        placeholders = ",".join("?" for _ in task_ids)
        rows = store.conn.execute(
            f"""
            SELECT task_id, COUNT(*) AS count
            FROM review_findings
            WHERE task_id IN ({placeholders}) AND status='unresolved' AND blocking=1
            GROUP BY task_id
            """,
            tuple(task_ids),
        ).fetchall()
        blocking_counts = {row["task_id"]: row["count"] for row in rows}
    for task in task_summaries:
        task["unresolved_blocking_review_findings"] = int(blocking_counts.get(task["id"], 0))
    timings["review_count_ms"] = _elapsed_ms(started)

    todo_counts = _fast_todo_counts(store, project_id)
    timings["total_ms"] = _elapsed_ms(total_started)

    return {
        "project": {"id": project["id"], "name": project["name"]},
        "active_tasks": task_summaries,
        "todo_counts": todo_counts,
        "git": {
            "head": snapshot.get("git_head"),
            "tracked_dirty": bool(snapshot.get("working_tree_dirty")),
            "git_available": bool(snapshot.get("git_available")),
        },
        "timings": timings,
    }


def print_fast_status(data: dict[str, Any], *, debug_timing: bool = False) -> None:
    project = data["project"]
    active_tasks = data["active_tasks"]
    print(f"{field_label('project')}: {project['id']} ({project['name']})")
    print(
        "git: "
        f"head={data['git']['head'] or 'none'} "
        f"tracked_dirty={bool_label(data['git']['tracked_dirty'])} "
        f"available={bool_label(data['git']['git_available'])}"
    )
    if not active_tasks:
        print(f"{field_label('status')}: {status_label('no_active_task')}")
        todos = data["todo_counts"]
        print(f"todo: ready={todos['ready']} open={todos['open']}")
        print(f"{field_label('next_action')}:")
        print("- 作業中のタスクはありません。次に扱う具体的な作業を人間が決めてください。")
    else:
        print(f"{field_label('status')}: {status_label('in_progress')}")
        print("作業中のタスク:")
        for task in active_tasks:
            verification = task["latest_verification_run"]
            verification_bits = []
            if verification["command"]:
                verification_bits.append(f"command={verification['command']}")
            if verification["exit_code"] is not None:
                verification_bits.append(f"exit_code={verification['exit_code']}")
            verification_bits.append(f"timed_out={str(verification['timed_out']).lower()}")
            print(f"- {task['id']} [{status_label(task['status'])}] {task['title']}")
            print(f"  {field_label('evidence')}: {status_label(task['evidence'])}")
            print(f"  {field_label('latest_verification_run')}: {', '.join(verification_bits)}")
            print(f"  {field_label('unresolved_blocking_count')}: {task['unresolved_blocking_review_findings']}")
        todos = data["todo_counts"]
        print(f"todo: ready={todos['ready']} open={todos['open']}")
        print(f"{field_label('next_action')}:")
        first = active_tasks[0]
        print(f"- 次は {first['id']} の状態と検証結果を確認してください。")
    if debug_timing:
        print("debug_timing:")
        for key in (
            "project_lookup_ms",
            "fast_task_query_ms",
            "git_fast_status_ms",
            "verification_lookup_ms",
            "review_count_ms",
            "total_ms",
        ):
            print(f"- {key}: {data['timings'][key]}")


def audit_status_data(store: Store, project_id: str, *, cwd: Path | None = None) -> dict[str, Any]:
    project = store.get("projects", project_id)
    if not project:
        raise SystemExit(f"project not found: {project_id}")
    snapshot = current_git_snapshot_full(cwd or Path.cwd())
    tasks = store.list_where("tasks", "project_id=?", (project_id,))
    audited_tasks = []
    for task in tasks:
        status = projected_task_status(store, task, current_snapshot=snapshot)
        verification_run = store.latest_for_task("verification_runs", task["id"])
        completion = active_task_completion(store, task["id"])
        evidence = commit_aware_evidence_status(verification_run, snapshot, completion)
        blocking_count = store.conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM review_findings
            WHERE task_id=? AND status='unresolved' AND blocking=1
            """,
            (task["id"],),
        ).fetchone()["count"]
        audited_tasks.append(
            {
                "id": task["id"],
                "title": task["title"],
                "status": status,
                "evidence": evidence,
                "verification_exit_code": verification_run["exit_code"] if verification_run else None,
                "verification_timed_out": bool(verification_run["timed_out"]) if verification_run else False,
                "unresolved_blocking_review_findings": int(blocking_count or 0),
            }
        )
    return {
        "project": {"id": project["id"], "name": project["name"]},
        "git": {
            "head": snapshot.get("git_head"),
            "dirty": bool(snapshot.get("working_tree_dirty")),
            "git_available": bool(snapshot.get("git_available")),
            "diff_hash_computed": bool(snapshot.get("git_diff_hash_computed")),
        },
        "tasks": audited_tasks,
    }


def print_audit_status(data: dict[str, Any]) -> None:
    project = data["project"]
    print(f"{field_label('project')}: {project['id']} ({project['name']})")
    print("モード: 厳密監査")
    print(
        "git: "
        f"head={data['git']['head'] or 'none'} "
        f"dirty={bool_label(data['git']['dirty'])} "
        f"available={bool_label(data['git']['git_available'])} "
        f"diff_hash_computed={bool_label(data['git']['diff_hash_computed'])}"
    )
    print("タスク監査:")
    if not data["tasks"]:
        print("- なし")
        return
    for task in data["tasks"]:
        bits = [f"evidence={task['evidence']}"]
        if task["verification_exit_code"] is not None:
            bits.append(f"exit_code={task['verification_exit_code']}")
        bits.append(f"timed_out={str(task['verification_timed_out']).lower()}")
        bits.append(f"blocking_reviews={task['unresolved_blocking_review_findings']}")
        print(f"- {task['id']} [{status_label(task['status'])}] {task['title']}")
        print(f"  {', '.join(bits)}")


def print_facade_next_for_task(store: Store, task_id: str) -> None:
    from .. import cli as c
    from ..work_projection import next_action_text, task_work_projection

    task = store.get("tasks", task_id)
    if not task:
        raise SystemExit(f"task not found: {task_id}")
    status = c.projected_task_status(store, task)
    projection = task_work_projection(store, task_id, current_snapshot=current_git_snapshot_full(Path.cwd()))
    print(f"{field_label('task')}: {task['id']}")
    print(f"{field_label('title')}: {task['title']}")
    print(f"{field_label('status')}: {status_label(status)}")
    print(f"{field_label('next_action')}:")
    print(f"- {next_action_text(projection)}")


def cmd_facade_status(args: argparse.Namespace) -> None:
    project_id = default_project_id(args)
    boundary = resolve_project_boundary(db_path=args.db)
    store = Store(args.db)
    try:
        if getattr(args, "ai", False) or getattr(args, "json", False):
            try:
                snapshot_mode = "full" if getattr(args, "verbose", False) else "fast"
                data = project_ai_context(store, project_id, snapshot_mode=snapshot_mode, verbose=bool(getattr(args, "verbose", False)))
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
            data["project_boundary"] = boundary.to_dict()
            if getattr(args, "json", False):
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                if boundary.should_print_text():
                    print("\n".join(boundary.text_lines()))
                    for warning in boundary_warning_lines(boundary):
                        print(warning)
                    print()
                max_chars = None if getattr(args, "verbose", False) else AI_CONTEXT_TEXT_MAX_CHARS
                print(render_ai_context_text({key: value for key, value in data.items() if key != "project_boundary"}, max_chars=max_chars))
            return
        if boundary.should_print_text():
            for line in boundary.text_lines():
                print(line)
            for warning in boundary_warning_lines(boundary):
                print(warning)
        if not getattr(args, "verbose", False) and not getattr(args, "audit", False):
            data = fast_status_data(store, project_id)
            print_fast_status(data, debug_timing=bool(getattr(args, "debug_timing", False)))
            if not data["active_tasks"]:
                print_roadmap_attention_summary(roadmap_attention_summary_for_project(store, project_id))
            return
        if getattr(args, "audit", False):
            print_audit_status(audit_status_data(store, project_id))
            return
        summary = summary_for_project(store, project_id)
        print(f"{field_label('project')}: {summary['project_id']} ({summary['project_name']})")
        print("モード: 詳細表示")
        print(f"ロードマップ: {summary['roadmap_position']}")
        print(f"作業状態: {summary['work_state']}")
        print("作業中:")
        if summary["active_tasks"]:
            for task in summary["active_tasks"]:
                print(f"- {task['id']} [{status_label(task['status'])}] {field_label('task_type')}: {task['task_type']} {task['title']}")
        else:
            print("- なし")
        print("TODO:")
        if summary["todo_status_counts"]:
            for status, count in summary["todo_status_counts"].items():
                print(f"- {status_label(status)}: {count}")
        else:
            print("- なし")
        print(f"{field_label('next_action')}:")
        actions = summary["next_actions"] or []
        if actions:
            for action in actions[:3]:
                print(f"- {human_next_action_text(action)}")
        else:
            print("- なし")
    finally:
        store.close()


def cmd_facade_next(args: argparse.Namespace) -> None:
    project_id = default_project_id(args)
    boundary = resolve_project_boundary(db_path=args.db)
    store = Store(args.db)
    try:
        if boundary.should_print_text():
            for line in boundary.text_lines():
                print(line)
            for warning in boundary_warning_lines(boundary):
                print(warning)
            print()
        if getattr(args, "task", None):
            if getattr(args, "do", False):
                print("stopped: next_do_no_safe_action")
                print(f"next: nilo next --task {args.task}")
                return
            print_facade_next_for_task(store, args.task)
            return
        project = store.get("projects", project_id)
        if not project:
            raise SystemExit(f"project not found: {project_id}")
        workflow = workflow_context(store, project_id)
        if workflow.get("type") == "recipe_run":
            if getattr(args, "do", False):
                print("stopped: active_recipe_requires_explicit_step")
                print(f"next: nilo next --project {project_id} --verbose")
                return
            if getattr(args, "ai", False):
                action: dict[str, Any] = {
                    "project_id": project_id,
                    "active_recipe": workflow.get("recipe_name", ""),
                    "recipe_status": workflow.get("status", ""),
                    "next_action": "run_release_prepare",
                    "command": workflow.get("release_prepare_command", ""),
                }
                if workflow.get("status") == "waiting_public_approval":
                    action.update(
                        {
                            "next_action": "await_public_approval",
                            "required_approval_text": workflow.get("required_approval_text", ""),
                            "command_after_approval": workflow.get("release_publish_command", ""),
                        }
                    )
                elif workflow.get("status") == "paused_for_fix":
                    action.update(
                        {
                            "next_action": "fix_in_current_release_task",
                            "blocked_recipe": workflow.get("recipe_name", ""),
                            "blocked_reason": workflow.get("blocked_reason", workflow.get("reason", "")),
                            "failed_verification_id": workflow.get("failed_verification_id", ""),
                            "failed_summary_path": workflow.get("failed_summary_path", ""),
                            "failed_shards": workflow.get("failed_shards", []),
                            "resume_command": workflow.get("resume_command", ""),
                        }
                    )
                print(json.dumps(action, ensure_ascii=False, indent=2))
                return
            print(f"{field_label('project')}: {project_id} ({project['name']})")
            if getattr(args, "verbose", False):
                print("workflow_context:")
                print(f"- recipe: {workflow['recipe_name']}")
                print(f"- status: {workflow['status']}")
                print(f"- current_step: {workflow['current_step']}")
                print(f"- next_step: {workflow['next_step']}")
                if workflow.get("pending_public_operations"):
                    print("pending_public_operations:")
                    for operation in workflow["pending_public_operations"]:
                        print(f"- {operation['operation']}: {operation['target']}")
                    if workflow.get("approval_prompt"):
                        print(workflow["approval_prompt"])
                    if workflow.get("public_execution_command"):
                        print(f"execute_after_approval: {workflow['public_execution_command']}")
            print(f"{field_label('next_action')}:")
            if workflow.get("status") == "waiting_public_approval":
                print("- next_action: await_public_approval")
                if workflow.get("required_approval_text"):
                    print(f"required_approval_text: {workflow['required_approval_text']}")
                if workflow.get("release_publish_command"):
                    print(f"command_after_approval: {workflow['release_publish_command']}")
                elif workflow.get("public_execution_command"):
                    if getattr(args, "verbose", False):
                        print(f"- After approval, execute: {workflow['public_execution_command']}")
                    else:
                        print(f"execute_after_approval: {workflow['public_execution_command']}")
            elif workflow.get("status") == "paused_for_fix":
                if workflow.get("failed_verification_id"):
                    print("- リリース検証が失敗しました。")
                    print("- リリースTaskは修正待ちとして継続しています。")
                    print("- 同じリリースTask内で原因を修正してください。")
                    print("- 修正後に release resume を実行してください。")
                    print("- next_action: fix_in_current_release_task")
                else:
                    print("- next_action: fix_in_current_release_task")
                if workflow.get("reason"):
                    print(f"reason: {workflow['reason']}")
                if workflow.get("blocked_reason"):
                    print(f"blocked_reason: {workflow['blocked_reason']}")
                if workflow.get("failed_verification_id"):
                    print(f"failed_verification_id: {workflow['failed_verification_id']}")
                if workflow.get("failed_summary_path"):
                    print(f"failed_summary_path: {workflow['failed_summary_path']}")
                if workflow.get("failed_shards"):
                    print("failed_shards: " + ", ".join(str(item) for item in workflow["failed_shards"]))
                if workflow.get("resume_command"):
                    print(f"resume_command: {workflow['resume_command']}")
            else:
                print("- next_action: run_release_prepare")
                if workflow.get("release_prepare_command"):
                    print(f"command: {workflow['release_prepare_command']}")
                else:
                    print(f"- Continue release recipe step: {workflow['next_step']}")
            if not getattr(args, "verbose", False):
                print(f"details: nilo status --ai --verbose --project {project_id}")
            return
        from ..work_projection import next_action_text, project_work_projection

        projection = project_work_projection(
            store, project_id, current_snapshot=current_git_snapshot_full(Path.cwd())
        )
        if projection.active_task_id:
            if getattr(args, "do", False):
                print("stopped: active_task_requires_agent_work")
                print(f"next: nilo work --task {projection.active_task_id} \"<current task>\"")
                return
            print_facade_next_for_task(store, projection.active_task_id)
            return
        if getattr(args, "do", False):
            todo = first_next_todo_for_project(store, project_id)
            if todo:
                print("stopped: todo_conversion_requires_explicit_task")
                print(f"next: nilo work \"{todo['title']}\" --project {project_id}")
            else:
                print("stopped: no_safe_next_action")
                print(f"next: nilo work \"<user request>\" --project {project_id}")
            return
        print(f"{field_label('project')}: {project_id} ({project['name']})")
        attention = roadmap_attention_summary_for_project(store, project_id)
        attention_items = [item for item in attention.get("items", []) if item.get("evidence_attention_items")]
        if attention_items:
            print("warning: 完了済みロードマップに証跡注意があります。")
            for item in attention_items:
                print(f"- {item['title']}")
        print(f"{field_label('next_action')}:")
        summary = summary_for_project(store, project_id)
        if projection.diagnostics.get("legacy_next_action"):
            action = next_action_text(projection)
        else:
            action = (summary.get("human_next_actions") or [next_action_text(projection)])[0]
        print(f"- {action}")
    finally:
        store.close()


def cmd_facade_queue(args: argparse.Namespace) -> None:
    project_id = default_project_id(args)
    store = Store(args.db)
    try:
        verbose = bool(getattr(args, "verbose", False))
        data = work_queue_data(store, project_id, audit=bool(getattr(args, "audit", False)), verbose=verbose)
        if getattr(args, "json", False):
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return
        print(f"{field_label('project')}: {data['project_id']} ({data['project_name']})")
        print(f"queue: total={data['counts']['total']} tasks={data['counts']['tasks']} todos={data['counts']['todos']}")
        unresolved = data["unresolved_project_state"]
        if unresolved:
            unresolved_summary = (
                f"failures={unresolved['failures']} review_findings={unresolved['review_findings']} "
                f"evidence_issues={unresolved['evidence_issues']} roadmap_commitments={unresolved['roadmap_commitments']} "
                f"pending_roadmap_revisions={unresolved['pending_roadmap_revisions']} "
                f"invalid_completions={unresolved['invalid_completions']} "
                f"review_dispatches={unresolved['review_dispatches']} overdrive_runs={unresolved['overdrive_runs']}"
            )
            if data["counts"]["total"] == 0 and data["counts"]["unresolved_project_state"]:
                print(f"task/todo queue is empty, but unresolved project state remains: {unresolved_summary}")
            else:
                print(f"unresolved_project_state: {unresolved_summary}")
        if data["counts"]["completion_audit_tasks"] and not getattr(args, "audit", False):
            print(f"completion_audit: {data['counts']['completion_audit_tasks']} completed task(s) need audit; rerun with --audit")
        print(f"{field_label('tasks')}:")
        if data["tasks"]:
            for task in data["tasks"]:
                issue_suffix = f" issues={','.join(task.get('audit_issues', []))}" if task.get("audit_issues") else ""
                print(
                    f"- {task['id']} [{status_label(task['status'])}] "
                    f"{task['task_type']} {task['risk_level']} {task['title']}{issue_suffix}"
                )
        else:
            print("- なし")
        print("TODO:")
        if data["todos"]:
            for todo in data["todos"]:
                print(f"- {todo['id']} [{status_label(todo['status'])}] {todo['priority']} {todo['title']}")
        else:
            print("- なし")
        if verbose and unresolved.get("details"):
            print("unresolved_details:")
            for kind, items in unresolved["details"].items():
                print(f"- {kind}: {len(items)}")
                for item in items[:20]:
                    fields = " ".join(f"{key}={value}" for key, value in item.items())
                    print(f"  - {fields}")
    finally:
        store.close()


def cmd_facade_start(args: argparse.Namespace) -> None:
    project_id = default_project_id(args)
    boundary = resolve_project_boundary(db_path=args.db)
    if getattr(args, "self_development", False):
        try:
            assert_self_development_allowed(boundary)
        except ProjectBoundaryError as exc:
            raise SystemExit(str(exc)) from exc
    store = Store(args.db)
    try:
        project = store.get("projects", project_id)
        if not project:
            raise SystemExit(f"project not found: {project_id}")
        commitment_id = args.commitment
        task_id = deterministic_id("task", [project_id, args.title, now_iso()])
    finally:
        store.close()

    create_args = argparse.Namespace(
        db=args.db,
        project=project_id,
        title=args.title,
        description=args.description,
        acceptance=args.acceptance,
        id=task_id,
        parent_task=None,
        split_index=None,
        commitment=commitment_id,
        roadmap_item="",
        model="",
        degradation="normal",
        mode=args.mode,
        task_type=args.task_type,
        risk=args.risk,
        requires_understanding_check=False,
    )
    with redirect_stdout(io.StringIO()):
        cmd_task_create(create_args)
    print(f"task: {task_id}")
    print(f"next: nilo next --task {task_id}")
    print(f"instruct: nilo instruct --task {task_id}")


def cmd_facade_check(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task_id, warning = resolve_check_task_id(args, store)
    finally:
        store.close()
    if warning:
        print(warning)
    cmd_verification_run(
        argparse.Namespace(
            db=args.db,
            task=task_id,
            command=args.command,
            mode=args.mode,
            snapshot=args.snapshot,
            timeout=args.timeout,
        )
    )


def cmd_facade_report(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task_id = resolve_task_id(args, store)
    finally:
        store.close()
    cmd_report_import(argparse.Namespace(db=args.db, task=task_id, file=args.file, agent=args.agent))


def cmd_facade_done(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task_id = resolve_task_id(args, store)
    finally:
        store.close()
    cmd_task_complete(
        argparse.Namespace(
            db=args.db,
            task=task_id,
            reason=args.reason,
            actor=args.actor,
            human_confirm=args.human_confirm,
            decision_note=args.decision_note,
            human_acceptance=args.human_acceptance,
            commit=args.commit,
            commit_message=args.commit_message,
        )
    )


def cmd_facade_reject(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task_id = resolve_task_id(args, store)
    finally:
        store.close()
    cmd_outcome_record(
        argparse.Namespace(
            db=args.db,
            task=task_id,
            reason=args.reason,
            concern=[],
            decision="rejected",
        )
    )
    print("closed: true")


def cmd_facade_cancel(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        task_id = resolve_task_id(args, store)
        try:
            human_acceptance = (args.human_acceptance or "").strip()
            cancel_task(store, task_id, actor=args.actor, reason=args.reason, human_confirm=bool(args.human_confirm or human_acceptance), decision_note=(args.decision_note or human_acceptance))
        except TransitionError as exc:
            raise SystemExit(f"{exc.message}{(': ' + exc.remediation) if exc.remediation else ''}") from exc
    finally:
        store.close()
    print("status: cancelled")
    print("closed: true")
