from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .display_labels import ai_value_label, category_label, field_label, severity_label
from .failure import compact_failure_message, list_failure_logs, summarize_failure_logs
from .human_status import human_next_action_text
from .project_language import project_primary_language
from .snapshot import commit_aware_evidence_status, current_git_snapshot, evidence_status
from .store import Store
from .task_logic import active_task_completion, is_task_completed_status, projected_task_status, unresolved_review_findings
from .workflow_context import workflow_context


def _ai_context_text_max_chars() -> int:
    """Return the compact AI text budget resolved at process start."""
    value = os.environ.get("NILO_AI_CONTEXT_MAX_CHARS", "1000")
    try:
        parsed = int(value)
    except ValueError:
        return 1000
    return max(parsed, 320)


AI_CONTEXT_TEXT_MAX_CHARS = _ai_context_text_max_chars()


def active_tasks(store: Store, project_id: str) -> tuple[list[dict], dict[str, str]]:
    from . import project_logic as p

    tasks, statuses = p.fast_project_tasks_and_recorded_statuses(store, project_id)
    return [task for task in tasks if not is_task_completed_status(statuses[task["id"]])], statuses


def task_ai_context(store: Store, task_id: str, *, cwd: Path | None = None, snapshot_mode: str = "full") -> dict[str, Any]:
    from . import project_logic as p

    cwd = cwd or Path.cwd()
    task = store.get("tasks", task_id)
    if not task:
        raise ValueError(f"task not found: {task_id}")
    snapshot = current_git_snapshot(cwd, mode=snapshot_mode)
    verification_run = store.latest_for_task("verification_runs", task_id)
    latest_report = store.latest_for_task("agent_reports", task_id)
    completion = active_task_completion(store, task_id)
    strict_evidence = snapshot_mode == "full"
    evidence = commit_aware_evidence_status(verification_run, snapshot, completion, strict=strict_evidence)
    if evidence == "missing" and latest_report:
        evidence = "present"
    unresolved = unresolved_review_findings(store, task_id)
    latest_event = store.latest_task_status_event(task_id)
    status = projected_task_status(store, task, current_snapshot=snapshot, latest_event=latest_event)
    latest_event_id = latest_event["event_id"] if latest_event else ""
    unexecuted = p.unexecuted_verifications_for_task(status, verification_run)
    next_actions = p.task_next_actions(task, status, verification_run, unexecuted)
    failures = list_failure_logs(store, task_id=task_id, status="open", limit=5)
    blocking_reasons: list[str] = []
    usable_evidence_statuses = {"current"} if strict_evidence else {"current", "present", "recorded"}
    if evidence not in usable_evidence_statuses:
        blocking_reasons.append(f"evidence_{evidence}")
    if unresolved:
        blocking_reasons.append(f"unresolved_review_findings:{len(unresolved)}")
    completion_allowed = not blocking_reasons
    return {
        "task": {
            "id": task["id"],
            "title": task["title"],
            "state": status,
            "task_type": task["task_type"],
            "risk_level": task["risk_level"],
        },
        "git": {
            "git_head": snapshot.get("git_head"),
            "git_diff_hash": snapshot.get("git_diff_hash") or "",
            "dirty": bool(snapshot.get("working_tree_dirty")),
            "diff_hash_computed": bool(snapshot.get("git_diff_hash_computed", True)),
        },
        "evidence": {
            "status": evidence,
            "verification_run_id": verification_run["id"] if verification_run else "",
            "report_id": latest_report["id"] if latest_report else "",
            "verification_exit_code": verification_run["exit_code"] if verification_run else None,
            "verification_timed_out": bool(verification_run["timed_out"]) if verification_run else False,
        },
        "review": {
            "unresolved_count": len(unresolved),
            "unresolved_blocking_count": len([item for item in unresolved if item["blocking"]]),
        },
        "completion": {
            "allowed": completion_allowed,
            "blocked": not completion_allowed,
            "blocking_reasons": blocking_reasons,
        },
        "write_context_token": f"task:{task_id}:{latest_event_id}" if latest_event_id else "",
        "latest_task_status_event_id": latest_event_id,
        "next_required_actions": next_actions[:3],
        "failure_logs": [
            {
                "id": failure["id"],
                "severity": failure["severity"],
                "category": failure["category"],
                "message": compact_failure_message(failure["message"]),
                "created_at": failure["created_at"],
            }
            for failure in failures
        ],
        "failure_logs_note": "失敗ログは観測履歴であり、必須ルールではありません。同じ失敗を避ける参考には使えますが、新しい要件を作らないでください。",
    }


def project_ai_context(
    store: Store,
    project_id: str,
    *,
    cwd: Path | None = None,
    snapshot_mode: str = "full",
    verbose: bool = False,
) -> dict[str, Any]:
    from . import project_logic as p

    cwd = cwd or Path.cwd()
    project = store.get("projects", project_id)
    if not project:
        raise ValueError(f"project not found: {project_id}")
    workflow = workflow_context(store, project_id)
    tasks: list[dict] = []
    statuses: dict[str, str] = {}
    if workflow.get("type") == "recipe_run" and workflow.get("task_id"):
        current = task_ai_context(store, workflow["task_id"], cwd=cwd, snapshot_mode=snapshot_mode)
    else:
        tasks, statuses = p.fast_project_tasks_and_recorded_statuses(store, project_id)
        active, _ = p.roadmap_prioritized_project_active_tasks(store, project_id, tasks, statuses)
        current = task_ai_context(store, active[0]["id"], cwd=cwd, snapshot_mode=snapshot_mode) if active else None
    if workflow.get("type") == "recipe_run":
        next_actions = workflow_next_actions(workflow)
    elif current:
        next_actions = current["next_required_actions"]
    else:
        design_residue = p.project_design_residue()
        commitments = p.accepted_roadmap_commitments(store, project_id)
        pending_revisions = p.pending_roadmap_revisions(store, project_id)
        next_actions = p.project_level_next_actions(store, tasks, statuses, design_residue, commitments, pending_revisions, project_id)[:3]
    failure_summary = summarize_failure_logs(store, project_id=project_id, limit=100000)
    verbose_context = {
        "project_id": project_id,
        "project_name": project["name"],
        "primary_language": project_primary_language(project, cwd),
        "workflow_context": workflow,
        "current_task": current,
        "next_required_actions": next_actions,
        "failure_summary": {
            "open_failures": failure_summary["open_failure_count"],
            "high_open_failures": failure_summary["high_open_failure_count"],
            "latest_open_failure": (
                {
                    "task_id": failure_summary["latest_open_failure"]["task_id"],
                    "category": failure_summary["latest_open_failure"]["category"],
                }
                if failure_summary["latest_open_failure"]
                else None
            ),
        },
    }
    if verbose:
        verbose_context["detail_commands"] = _detail_commands(project_id, current["task"]["id"] if current else None)
        return verbose_context
    return compact_project_ai_context(verbose_context)


def _detail_commands(project_id: str, task_id: str | None) -> list[str]:
    commands = [
        f"nilo status --ai --verbose --project {project_id}",
        f"nilo roadmap status --ai --project {project_id}",
        f"nilo failure list --project {project_id}",
    ]
    if task_id:
        commands.insert(1, f"nilo task status --task {task_id} --ai")
        commands.insert(2, f"nilo evidence show --task {task_id} --ai")
        commands.insert(3, f"nilo review status --task {task_id} --format json")
    return commands


def compact_project_ai_context(data: dict[str, Any]) -> dict[str, Any]:
    workflow = data.get("workflow_context") or {}
    if workflow.get("type") == "recipe_run" and workflow.get("recipe_name") == "release":
        return _compact_release_recipe_context(data, workflow)
    current = data.get("current_task")
    task_id = current["task"]["id"] if current else None
    active_task = None
    blockers: list[str] = []
    latest_verification = {"status": "none", "verification_run_id": "", "exit_code": None}
    latest_review = {"unresolved_count": 0, "unresolved_blocking_count": 0}
    write_context_token = ""
    latest_task_status_event_id = ""
    if current:
        task = current["task"]
        completion = current["completion"]
        evidence = current["evidence"]
        review = current["review"]
        active_task = {"id": task["id"], "title": task["title"], "status": task["state"]}
        blockers = completion.get("blocking_reasons", [])
        latest_verification = {
            "status": evidence["status"],
            "verification_run_id": evidence["verification_run_id"],
            "exit_code": evidence["verification_exit_code"],
        }
        latest_review = {
            "unresolved_count": review["unresolved_count"],
            "unresolved_blocking_count": review["unresolved_blocking_count"],
        }
        write_context_token = current.get("write_context_token", "")
        latest_task_status_event_id = current.get("latest_task_status_event_id", "")

    next_actions = data.get("next_required_actions") or []
    next_action = next_actions[0] if next_actions else ""
    detail_commands = _detail_commands(data["project_id"], task_id)
    required_commands = []
    if next_action:
        required_commands.append("nilo next --project " + data["project_id"])
    if next_action.startswith("run nilo "):
        required_commands.append(next_action.removeprefix("run "))
    return {
        "compact": True,
        "project_id": data["project_id"],
        "project_name": data["project_name"],
        "primary_language": data.get("primary_language", ""),
        "active_task": active_task,
        "next_action": next_action,
        "blockers": {"count": len(blockers), "items": blockers[:3]},
        "latest_verification": latest_verification,
        "latest_review": latest_review,
        "write_context_token": write_context_token,
        "latest_task_status_event_id": latest_task_status_event_id,
        "failure_summary": data.get("failure_summary", {}),
        "required_commands": required_commands,
        "detail_commands": detail_commands,
    }


def _compact_release_recipe_context(data: dict[str, Any], workflow: dict[str, Any]) -> dict[str, Any]:
    status = workflow.get("status", "")
    target_version = str(workflow.get("target_version") or "").lstrip("v")
    if not target_version and workflow.get("required_approval_text"):
        target_version = str(workflow["required_approval_text"]).split(" ", 1)[0].lstrip("v")
    current = data.get("current_task") or {}
    task = current.get("task") or {}
    completion = current.get("completion") or {}
    evidence = current.get("evidence") or {}
    review = current.get("review") or {}
    blockers = completion.get("blocking_reasons") or []
    action: dict[str, Any] = {
        "compact": True,
        "project_id": data["project_id"],
        "project_name": data["project_name"],
        "active_recipe": "release",
        "recipe_status": status,
        "target_version": target_version,
        "active_task": {
            "id": workflow.get("task_id", ""),
            "title": task.get("title") or "release",
            "status": task.get("state") or status,
        },
        "blockers": {"count": len(blockers), "items": blockers[:3]},
        "latest_verification": {
            "status": evidence.get("status", "none"),
            "verification_run_id": evidence.get("verification_run_id", ""),
            "exit_code": evidence.get("verification_exit_code"),
        },
        "latest_review": {
            "unresolved_count": review.get("unresolved_count", 0),
            "unresolved_blocking_count": review.get("unresolved_blocking_count", 0),
        },
        "failure_summary": data.get("failure_summary", {}),
        "detail_commands": [f"nilo status --ai --verbose --project {data['project_id']}"],
        "required_commands": ["nilo next --project " + data["project_id"]],
    }
    if status == "waiting_public_approval":
        action.update(
            {
                "next_action": "await_public_approval",
                "required_approval_text": workflow.get("required_approval_text", ""),
                "command_after_approval": workflow.get("release_publish_command", ""),
            }
        )
    elif status == "paused_for_fix":
        failed = bool(workflow.get("failed_verification_id"))
        action.update(
            {
                "next_action": "create_separate_bugfix_task" if failed else "fix_and_resume",
                "blocked_recipe": workflow.get("recipe_name", ""),
                "blocked_reason": workflow.get("blocked_reason", workflow.get("reason", "")),
                "must_not_fix_inside_recipe": failed,
                "reason": workflow.get("reason", ""),
                "failed_verification_id": workflow.get("failed_verification_id", ""),
                "failed_summary_path": workflow.get("failed_summary_path", ""),
                "failed_shards": workflow.get("failed_shards", []),
                "resume_command": workflow.get("resume_command", ""),
            }
        )
    else:
        action.update({"next_action": "run_release_prepare", "command": workflow.get("release_prepare_command", "")})
    return action


def workflow_next_actions(workflow: dict[str, Any]) -> list[str]:
    if workflow.get("status") == "waiting_public_approval":
        operations = workflow.get("pending_public_operations") or []
        operation_text = ", ".join(f"{item['operation']}:{item['target']}" for item in operations)
        action = f"release recipe waiting for explicit public operation approval: {operation_text}"
        if workflow.get("public_execution_command"):
            action += f"; after approval run: {workflow['public_execution_command']}"
        return [action]
    if workflow.get("status") == "paused_for_fix":
        if workflow.get("failed_verification_id"):
            return [f"release recipe blocked by failed verification; create a separate bugfix task, then resume with: {workflow.get('resume_command', '')}"]
        return [f"release recipe paused for fix; resume with: {workflow.get('resume_command', '')}"]
    return [f"continue active {workflow.get('recipe_name')} recipe step: {workflow.get('next_step')}"]


def _shorten(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 3:
        return value[:limit]
    return value[: limit - 3] + "..."


def _compact_next_action_text(action: str) -> str:
    if action.startswith("possible large work; recommend roadmap planning"):
        return "大きい作業は作業計画の承認後に Task 化してください。"
    if action.startswith("run nilo instruct --task "):
        return action
    if action == "perform the instructed work and import a completion report":
        return "指示作業を実施し、完了報告を取り込んでください。"
    return _shorten(human_next_action_text(action), 90)


def _render_with_budget(required: list[str], optional_sections: list[list[str]], max_chars: int | None) -> str:
    if max_chars is None:
        lines = list(required)
        for section in optional_sections:
            lines.extend(section)
        return "\n".join(lines)

    selected: list[list[str]] = []
    for section in optional_sections:
        candidate_sections = [*selected, section]
        candidate_lines = list(required)
        for selected_section in candidate_sections:
            candidate_lines.extend(selected_section)
        if len("\n".join(candidate_lines)) <= max_chars:
            selected.append(section)

    lines = list(required)
    for section in selected:
        lines.extend(section)
    body = "\n".join(lines)
    if len(body) <= max_chars:
        return body

    compact_required = [_shorten(line, 120) for line in required]
    body = "\n".join(compact_required)
    if len(body) <= max_chars:
        return body

    overflow_note = "\n... compact output truncated; use detail_commands for details"
    budget = max(max_chars - len(overflow_note), 0)
    return _shorten(body, budget) + overflow_note


def render_ai_context_text(data: dict[str, Any], *, max_chars: int | None = None) -> str:
    if data.get("compact"):
        return render_compact_ai_context_text(data, max_chars=max_chars)

    required: list[str] = [f"{field_label('project')}: {data['project_id']} ({data['project_name']})"]
    optional_sections: list[list[str]] = []
    current = data.get("current_task")
    workflow = data.get("workflow_context") or {"type": "project", "status": "no_active_recipe"}
    if workflow.get("type") == "recipe_run":
        required.append(
            "workflow_context: "
            f"{workflow['recipe_name']} {workflow['status']} "
            f"current_step={workflow['current_step']} next_step={workflow['next_step']}"
        )
        if workflow.get("pending_public_operations"):
            required.append("pending_public_operations:")
            for item in workflow["pending_public_operations"]:
                required.append(f"- {item['operation']}: {item['target']}")
        if workflow.get("approval_prompt"):
            required.append(workflow["approval_prompt"])
            if workflow.get("public_execution_command"):
                required.append(f"execute_after_approval: {workflow['public_execution_command']}")
    elif workflow.get("latest_completed_release"):
        release = workflow["latest_completed_release"]
        required.append("Release recipe completed:")
        for key in ("commit", "tag", "github_release", "working_tree", "release_task"):
            value = release.get(key)
            if value:
                required.append(f"- {key}: {value}")
        if release.get("pushed"):
            required.append(f"- pushed: {', '.join(release['pushed'])}")
    if not current:
        required.append(f"{field_label('status')}: {ai_value_label('no_active_task')}")
        required.append("現在のタスク: なし")
    else:
        task = current["task"]
        git = current["git"]
        evidence = current["evidence"]
        review = current["review"]
        completion = current["completion"]
        git_head = git["git_head"] or "none"
        diff_hash = git["git_diff_hash"] or "none"
        exact_freshness_checked = bool(git.get("diff_hash_computed", True))
        if diff_hash == "__not_computed__":
            diff_hash = "not_computed"
        if max_chars is not None:
            git_head = git_head[:12] if git_head != "none" else git_head
            diff_hash = diff_hash[:12] if diff_hash != "none" else diff_hash
        required.extend(
            [
                f"{field_label('task')}: {task['id']} {_shorten(task['title'], 80 if max_chars is None else 48)}",
                f"{field_label('status')}: {ai_value_label(task['state'])}",
                f"git: head={git_head} diff_hash={diff_hash} dirty={git['dirty']}",
                f"{field_label('evidence')}: {ai_value_label(evidence['status'])}",
                f"{field_label('unresolved_review_count')}: {review['unresolved_count']}",
                f"{field_label('completion')}: {ai_value_label('completion_allowed' if completion['allowed'] else 'completion_blocked')}",
            ]
        )
        if not exact_freshness_checked and evidence["status"] in {"recorded", "present"}:
            required.append(f"{field_label('evidence')}: 証跡あり。厳密な差分一致は詳細確認で確認してください。")
        if completion["blocking_reasons"]:
            required.append(f"{field_label('blocking_reasons')}:")
            required.extend(f"- {reason}" for reason in completion["blocking_reasons"])
        if current.get("failure_logs"):
            failure_lines = [f"{field_label('failure_logs')}:"]
            for failure in current["failure_logs"]:
                failure_lines.append(f"- [{severity_label(failure['severity'])}] {category_label(failure['category'])}")
                failure_lines.append(f"  {_shorten(failure['message'], 90)}")
            if max_chars is None:
                failure_lines.append(current["failure_logs_note"])
            optional_sections.append(failure_lines)
    failure_summary = data.get("failure_summary", {})
    if failure_summary:
        failure_summary_lines = [f"{field_label('failure_summary')}:"]
        failure_summary_lines.append(f"- {field_label('open_failures')}: {failure_summary.get('open_failures', 0)}")
        failure_summary_lines.append(f"- {field_label('high_open_failures')}: {failure_summary.get('high_open_failures', 0)}")
        latest = failure_summary.get("latest_open_failure")
        if latest:
            failure_summary_lines.append(f"- {field_label('latest_open_failure')}: {latest['task_id']} {category_label(latest['category'])}")
        failure_summary_lines.append(f"詳細は `nilo failure list --project {data['project_id']}` を確認してください。")
        optional_sections.append(failure_summary_lines)
    next_lines = [f"{field_label('next_required_actions')}:"]
    actions = data.get("next_required_actions") or []
    if actions:
        visible_actions = actions if max_chars is None else actions[:1]
        next_lines.extend(f"- {_compact_next_action_text(action) if max_chars is not None else human_next_action_text(action)}" for action in visible_actions)
    else:
        next_lines.append("- なし")
    required.extend(next_lines)
    work_size_lines = [
        "作業規模の判定:",
        "- 小さく明確な修正は通常 task として進める。",
        "- 小〜中規模の作業は Light plan または通常 task として進める。",
        "- 複数ファイルだけでは roadmap 扱いにせず、ひとまとまりの明確なバグ修正は通常 task として進める。",
        "- 複数タスク・複数コミット・実装と検証の分離が必要な作業は Roadmap を推奨する。",
        "- DB schema、状態遷移、リリース基盤、複数サブシステムにまたがる大改修だけ Epic 扱いを提案する。",
        "- Epic 扱いが必要な場合は理由を示して停止し、明示承認まで roadmap revision / acceptance / task plan を進めない。",
    ]
    if current:
        work_size_lines.append("- CLI等の複数機能・複数実装トラックは roadmap 推奨。自動作成せず承認後に作る。")
    else:
        work_size_lines.append("- DB schema、CLI、AI向け出力、docs/tests は、複数機能・複数実装トラック・不明確な範囲などの広さがある場合に roadmap を推奨する。")
        work_size_lines.append("- 大きい作業だと判断した場合でも自動では roadmap を作らず、人間に作業計画化を推奨して判断を待つ。")
        work_size_lines.append(
            "- 人間が承認した場合だけ `nilo roadmap discuss` で相談用コンテキストを作り、"
            "RoadmapProposal を作成して `nilo roadmap import` または `nilo roadmap adopt` する。"
        )
    vocabulary_lines = ["語彙ルール:"]
    primary_language = data.get("primary_language")
    if primary_language:
        vocabulary_lines.append(
            f"- Niloへ保存する title / description / acceptance criteria / todo / roadmap の人間可読文面は primary_language={primary_language} で書く。"
        )
        vocabulary_lines.append("- command / path / enum / status / JSON field は英語や元の表記を維持する。")
    if current:
        vocabulary_lines.append("- タスク化=Task 作成、Todo=受付だけ。")
    else:
        vocabulary_lines.append("- ユーザーが「これをタスク化して」「Taskにして」「作業タスクを作って」と言った場合は、Todo ではなく Task 作成を優先する。")
        vocabulary_lines.append("- create_task=新規具体作業、create_task_from_todo=既存 Todo 変換、create_todo=受付だけ。")
        vocabulary_lines.append("- Todo は後で見る、メモ、候補、未実行、曖昧な受付に使う。")
        vocabulary_lines.append("- type / risk / acceptance は意図が明確なら補完し、補完できないほど曖昧な場合だけ Todo に入れる。")
    if current:
        optional_sections.append(work_size_lines)
    optional_sections.append(vocabulary_lines)
    if not current:
        optional_sections.append(work_size_lines)
    roadmap_lines = ["ロードマップ承認待ちの応答ルール:"]
    if current:
        roadmap_lines.append("- 大きい作業は内部用語だけで説明せず、作業計画の確認・承認・Task 化を案内する。")
    else:
        roadmap_lines.append("- pending Roadmap / RoadmapProposal / RoadmapRevision をユーザーに内部用語だけで説明しない。")
        roadmap_lines.append("- まず「作業が大きいので、先に作業計画を作った」と説明する。")
        roadmap_lines.append("- 次に、計画の中身を人間が判断できる形で要約または全文表示する。")
        roadmap_lines.append("- 「これで進めてよければ承認してください」と明示する。")
        roadmap_lines.append("- 承認後は「この計画をもとに Task 化します」と説明する。")
        roadmap_lines.append("- 修正したい場合は「どこを変えるか指示してください」と案内する。")
    roadmap_lines.append("- ロードマップ状態を聞かれたら、まず実装タスクが残っているかを答える。")
    roadmap_lines.append("- 次に roadmap commitment がクローズ済みか、クローズ可能か、人間確認待ちかを説明する。")
    roadmap_lines.append("- 内部状態名は原則出さず、人間向けの日本語ラベルで説明する。")
    roadmap_lines.append("- 最後に、次に人間が判断することを示す。")
    optional_sections.append(roadmap_lines)
    overdrive_lines = [
        "Overdrive 応答ルール:",
        "- 「全部オーバードライブ」は既定では現在の依頼対象に限定する。",
        "- `nilo next` で unrelated な別 task に進む前に止まり、`--scope queue` または明示承認が必要だと報告する。",
        "- 人間に報告するときは、実装ファイル、テスト、Nilo 帳票 md、docs md を分けて説明する。",
    ]
    optional_sections.append(overdrive_lines)
    return _render_with_budget(required, optional_sections, max_chars)


def render_compact_ai_context_text(data: dict[str, Any], *, max_chars: int | None = None) -> str:
    active = data.get("active_task")
    required: list[str] = [
        f"project_id: {data['project_id']}",
    ]
    if data.get("active_recipe"):
        required.append(f"active_recipe: {data['active_recipe']}")
        required.append(f"recipe_status: {data.get('recipe_status', '')}")
        if data.get("target_version"):
            required.append(f"target_version: {data['target_version']}")
    if active:
        required.append(f"active_task: {active['id']} [{active['status']}] {_shorten(active['title'], 64)}")
    else:
        required.append("active_task: none")

    action = data.get("next_action") or ""
    required.append("next_action:")
    required.append(f"- {_compact_next_action_text(action) if action else 'なし'}")
    required.append("roadmap_response_rules:")
    required.append("- まず実装タスクが残っているかを答える。")
    required.append("- 次に roadmap commitment のクローズ状態を説明する。")
    required.append("- 内部状態名は原則出さず、最後に次の人間判断を示す。")
    required.append("overdrive_rules: 既定は現在依頼対象。unrelated task 前で停止し、報告は実装/テスト/Nilo帳票md/docs mdを分ける。")
    for key in ("reason", "failed_verification_id", "resume_command", "required_approval_text", "command_after_approval", "command"):
        if data.get(key):
            required.append(f"{key}: {data[key]}")

    required.append("detail_commands:")
    detail_commands = data.get("detail_commands", [])
    if detail_commands:
        required.extend(f"- {command}" for command in detail_commands)
    else:
        required.append("- none")

    blockers = data.get("blockers") or {}
    blocker_items = blockers.get("items") or []
    required.append(f"blockers: count={blockers.get('count', 0)}")
    for blocker in blocker_items:
        required.append(f"- {blocker}")

    verification = data.get("latest_verification") or {}
    required.append(
        "latest_verification: "
        f"status={verification.get('status', 'none')} "
        f"id={verification.get('verification_run_id') or 'none'} "
        f"exit_code={verification.get('exit_code')}"
    )
    review = data.get("latest_review") or {}
    required.append(
        "latest_review: "
        f"unresolved={review.get('unresolved_count', 0)} "
        f"blocking={review.get('unresolved_blocking_count', 0)}"
    )

    failure_summary = data.get("failure_summary") or {}
    if failure_summary:
        latest = failure_summary.get("latest_open_failure")
        latest_text = "none" if not latest else f"{latest['task_id']} {latest['category']}"
        required.append(
            "failure_summary: "
            f"open={failure_summary.get('open_failures', 0)} "
            f"high={failure_summary.get('high_open_failures', 0)} "
            f"latest={latest_text}"
        )

    required.append("required_commands:")
    commands = data.get("required_commands") or []
    if commands:
        required.extend(f"- {command}" for command in commands)
    else:
        required.append("- none")

    return _render_with_budget(required, [], max_chars)


def evidence_ai_context(store: Store, task_id: str, *, cwd: Path | None = None) -> dict[str, Any]:
    return task_ai_context(store, task_id, cwd=cwd)["evidence"]


def review_ai_context(store: Store, task_id: str) -> dict[str, Any]:
    findings = unresolved_review_findings(store, task_id)
    return {
        "task_id": task_id,
        "unresolved_count": len(findings),
        "unresolved_blocking_count": len([item for item in findings if item["blocking"]]),
        "unresolved_findings": [
            {
                "id": item["id"],
                "severity": item["severity"],
                "blocking": bool(item["blocking"]),
                "title": item["title"],
                "file_path": item["file_path"],
                "line": item["line"],
            }
            for item in findings[:10]
        ],
    }
