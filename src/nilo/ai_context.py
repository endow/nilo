from __future__ import annotations

from pathlib import Path
from typing import Any

from .display_labels import ai_value_label, category_label, field_label, severity_label
from .failure import compact_failure_message, list_failure_logs, summarize_failure_logs
from .human_status import human_next_action_text
from .snapshot import current_git_snapshot, evidence_status
from .store import Store
from .task_logic import is_task_completed_status, projected_task_status, unresolved_review_findings


AI_CONTEXT_TEXT_MAX_CHARS = 700


def active_tasks(store: Store, project_id: str) -> tuple[list[dict], dict[str, str]]:
    from . import project_logic as p

    tasks, statuses = p.project_tasks_and_statuses(store, project_id)
    return [task for task in tasks if not is_task_completed_status(statuses[task["id"]])], statuses


def task_ai_context(store: Store, task_id: str, *, cwd: Path | None = None) -> dict[str, Any]:
    from . import project_logic as p

    cwd = cwd or Path.cwd()
    task = store.get("tasks", task_id)
    if not task:
        raise ValueError(f"task not found: {task_id}")
    snapshot = current_git_snapshot(cwd)
    verification_run = store.latest_for_task("verification_runs", task_id)
    latest_report = store.latest_for_task("agent_reports", task_id)
    evidence = evidence_status(verification_run, snapshot)
    if evidence == "missing" and latest_report:
        evidence = "present"
    unresolved = unresolved_review_findings(store, task_id)
    status = projected_task_status(store, task)
    latest_event = store.latest_task_status_event(task_id)
    latest_event_id = latest_event["event_id"] if latest_event else ""
    unexecuted = p.unexecuted_verifications_for_task(status, verification_run)
    next_actions = p.task_next_actions(task, status, verification_run, unexecuted)
    failures = list_failure_logs(store, task_id=task_id, status="open", limit=5)
    blocking_reasons: list[str] = []
    if evidence != "current":
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


def project_ai_context(store: Store, project_id: str, *, cwd: Path | None = None) -> dict[str, Any]:
    from . import project_logic as p

    cwd = cwd or Path.cwd()
    project = store.get("projects", project_id)
    if not project:
        raise ValueError(f"project not found: {project_id}")
    tasks, statuses = p.project_tasks_and_statuses(store, project_id)
    active = [task for task in tasks if not is_task_completed_status(statuses[task["id"]])]
    current = task_ai_context(store, active[0]["id"], cwd=cwd) if active else None
    design_residue = p.project_design_residue()
    commitments = p.accepted_roadmap_commitments(store, project_id)
    pending_revisions = p.pending_roadmap_revisions(store, project_id)
    failure_summary = summarize_failure_logs(store, project_id=project_id, limit=100000)
    return {
        "project_id": project_id,
        "project_name": project["name"],
        "current_task": current,
        "next_required_actions": (
            current["next_required_actions"]
            if current
            else p.project_level_next_actions(store, tasks, statuses, design_residue, commitments, pending_revisions, project_id)[:3]
        ),
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

    overflow_note = "\n... compact output truncated; use --json or doctor ai-context for details"
    budget = max(max_chars - len(overflow_note), 0)
    return _shorten(body, budget) + overflow_note


def render_ai_context_text(data: dict[str, Any], *, max_chars: int | None = None) -> str:
    required: list[str] = [f"{field_label('project')}: {data['project_id']} ({data['project_name']})"]
    optional_sections: list[list[str]] = []
    current = data.get("current_task")
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
                f"{field_label('completion')}: {ai_value_label('allowed' if completion['allowed'] else 'blocked')}",
            ]
        )
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
        "- 複数ファイルだけでは roadmap 扱いにせず、ひとまとまりの明確なバグ修正は通常 task として進める。",
    ]
    if current:
        work_size_lines.append("- CLI等の複数機能・複数実装トラックは roadmap 推奨。自動作成せず承認後に作る。")
    else:
        work_size_lines.append("- DB schema、CLI、AI向け出力、docs/tests は、複数機能・複数実装トラック・不明確な範囲などの広さがある場合に roadmap を推奨する。")
        work_size_lines.append("- 大きい作業だと判断した場合でも自動では roadmap を作らず、人間に作業計画化を推奨して判断を待つ。")
        work_size_lines.append("- 人間が承認した場合だけ `nilo roadmap discuss` で作業計画を作る。")
    vocabulary_lines = ["語彙ルール:"]
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
    optional_sections.append(roadmap_lines)
    return _render_with_budget(required, optional_sections, max_chars)


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
