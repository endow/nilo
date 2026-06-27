from __future__ import annotations

from pathlib import Path
from typing import Any

from .display_labels import ai_value_label, category_label, field_label, severity_label
from .failure import compact_failure_message, list_failure_logs, summarize_failure_logs
from .human_status import human_next_action_text
from .snapshot import current_git_snapshot, evidence_status
from .store import Store
from .task_logic import is_task_completed_status, projected_task_status, unresolved_review_findings


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


def render_ai_context_text(data: dict[str, Any]) -> str:
    lines: list[str] = [f"{field_label('project')}: {data['project_id']} ({data['project_name']})"]
    current = data.get("current_task")
    if not current:
        lines.append(f"{field_label('status')}: {ai_value_label('no_active_task')}")
        lines.append("現在のタスク: なし")
    else:
        task = current["task"]
        git = current["git"]
        evidence = current["evidence"]
        review = current["review"]
        completion = current["completion"]
        lines.extend(
            [
                f"{field_label('task')}: {task['id']} {task['title']}",
                f"{field_label('status')}: {ai_value_label(task['state'])}",
                f"git: head={git['git_head'] or 'none'} diff_hash={git['git_diff_hash'] or 'none'} dirty={git['dirty']}",
                f"{field_label('evidence')}: {ai_value_label(evidence['status'])}",
                f"{field_label('unresolved_review_count')}: {review['unresolved_count']}",
                f"{field_label('completion')}: {ai_value_label('allowed' if completion['allowed'] else 'blocked')}",
            ]
        )
        if completion["blocking_reasons"]:
            lines.append(f"{field_label('blocking_reasons')}:")
            lines.extend(f"- {reason}" for reason in completion["blocking_reasons"])
        if current.get("failure_logs"):
            lines.append(f"{field_label('failure_logs')}:")
            for failure in current["failure_logs"]:
                lines.append(f"- [{severity_label(failure['severity'])}] {category_label(failure['category'])}")
                lines.append(f"  {failure['message']}")
            lines.append(current["failure_logs_note"])
    failure_summary = data.get("failure_summary", {})
    if failure_summary:
        lines.append(f"{field_label('failure_summary')}:")
        lines.append(f"- {field_label('open_failures')}: {failure_summary.get('open_failures', 0)}")
        lines.append(f"- {field_label('high_open_failures')}: {failure_summary.get('high_open_failures', 0)}")
        latest = failure_summary.get("latest_open_failure")
        if latest:
            lines.append(f"- {field_label('latest_open_failure')}: {latest['task_id']} {category_label(latest['category'])}")
        lines.append(f"詳細は `nilo failure list --project {data['project_id']}` を確認してください。")
    lines.append(f"{field_label('next_required_actions')}:")
    actions = data.get("next_required_actions") or []
    lines.extend(f"- {human_next_action_text(action)}" for action in actions) if actions else lines.append("- なし")
    lines.append("作業規模の判定:")
    lines.append("- 小さく明確な修正は通常 task として進める。")
    lines.append("- 複数ファイルだけでは roadmap 扱いにせず、ひとまとまりの明確なバグ修正は通常 task として進める。")
    if current:
        lines.append("- CLI等の複数機能・複数実装トラックは roadmap 推奨。自動作成せず承認後に作る。")
    else:
        lines.append("- DB schema、CLI、AI向け出力、docs/tests は、複数機能・複数実装トラック・不明確な範囲などの広さがある場合に roadmap を推奨する。")
        lines.append("- 大きい作業だと判断した場合でも自動では roadmap を作らず、人間に作業計画化を推奨して判断を待つ。")
        lines.append("- 人間が承認した場合だけ `nilo roadmap discuss` で作業計画を作る。")
    lines.append("語彙ルール:")
    if current:
        lines.append("- タスク化=Task 作成、Todo=受付だけ。")
    else:
        lines.append("- ユーザーが「これをタスク化して」「Taskにして」「作業タスクを作って」と言った場合は、Todo ではなく Task 作成を優先する。")
        lines.append("- create_task=新規具体作業、create_task_from_todo=既存 Todo 変換、create_todo=受付だけ。")
        lines.append("- Todo は後で見る、メモ、候補、未実行、曖昧な受付に使う。")
        lines.append("- type / risk / acceptance は意図が明確なら補完し、補完できないほど曖昧な場合だけ Todo に入れる。")
    lines.append("ロードマップ承認待ちの応答ルール:")
    if current:
        lines.append("- 大きい作業は内部用語だけで説明せず、作業計画の推奨・人間判断・承認後の Task 化を案内する。")
    else:
        lines.append("- pending Roadmap / RoadmapProposal / RoadmapRevision をユーザーに内部用語だけで説明しない。")
        lines.append("- まず「作業が大きいので、先に作業計画を作った」と説明する。")
        lines.append("- 次に、計画の中身を人間が判断できる形で要約または全文表示する。")
        lines.append("- 「これで進めてよければ承認してください」と明示する。")
        lines.append("- 承認後は「この計画をもとに Task 化します」と説明する。")
        lines.append("- 修正したい場合は「どこを変えるか指示してください」と案内する。")
    return "\n".join(lines)


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
