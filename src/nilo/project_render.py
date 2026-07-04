from __future__ import annotations

from pathlib import Path

from .display_labels import field_label, status_label
from .human_status import human_next_action_text
from .snapshot import commit_aware_evidence_status, current_git_snapshot
from .store import Store
from .task_logic import active_task_completion


def print_roadmap_agent_state(state: dict | None) -> None:
    print("roadmap_agent_state:")
    if not state:
        print("- none")
        return
    print(f"  commitment_id: {state['commitment_id']}")
    print(f"  commitment_title: {state['commitment_title']}")
    print(f"  work_status: {state['work_status']}")
    print(f"  evidence_status: {state['evidence_status']}")
    print(f"  verification_status: {state['verification_status']}")
    print(f"  closure_status: {state['closure_status']}")
    print("  ai_allowed_actions:")
    for action in state["ai_allowed_actions"]:
        print(f"  - {action}")
    print("  ai_blocked_actions:")
    for action in state["ai_blocked_actions"]:
        print(f"  - {action}")
    print(f"  recommended_next_action: {state['recommended_next_action']}")


def print_roadmap_agent_next_actions(actions: list[dict]) -> None:
    print("roadmap_agent_next_actions:")
    if not actions:
        print("- none")
        return
    for action in actions:
        print(f"- action_id: {action['action_id']}")
        print(f"  actor: {action['actor']}")
        print(f"  status: {action['status']}")
        print(f"  command_hint: {action['command_hint']}")
        print(f"  reason: {action['reason']}")


def print_project_summary_text(summary: dict) -> None:
    from .project_logic import human_recipe_provenance_label, human_roadmap_assessment_summary

    print(f"project_id: {summary['project_id']}")
    print(f"project_name: {summary['project_name']}")
    print(f"roadmap_position: {summary['roadmap_position']}")
    print(f"work_state: {summary['work_state']}")
    print(f"current_phase: {summary['current_phase']}")

    print("task_status_counts:")
    if summary["task_status_counts"]:
        for status, count in summary["task_status_counts"].items():
            print(f"- {status}: {count}")
    else:
        print("- none")

    print("todo_status_counts:")
    if summary["todo_status_counts"]:
        for status, count in summary["todo_status_counts"].items():
            print(f"- {status}: {count}")
    else:
        print("- none")

    print("recent_history:")
    if summary["recent_history"]:
        for item in summary["recent_history"]:
            print(f"- {item['created_at']} {item['task_id']} {item['event']}: {item['summary']}")
    else:
        print("- none")

    print("active_tasks:")
    if summary["active_tasks"]:
        for task in summary["active_tasks"]:
            print(f"- {task['id']} [{task['status']}] {task['task_type']} {task['risk_level']} {task['title']}")
            recipe_label = human_recipe_provenance_label(task.get("recipe_provenance"))
            if recipe_label:
                print(f"  recipe: {recipe_label}")
            print(f"  latest_verification_run: {task['latest_verification_run']}")
            print(f"  verification_working_tree: {task['verification_working_tree']}")
            policy = task.get("verification_snapshot_policy", {})
            if policy.get("skipped_paths"):
                reasons = ", ".join(f"{reason}={count}" for reason, count in sorted(policy.get("skipped_reasons", {}).items())) or "none"
                print("  snapshot:")
                print(f"    observed paths: {policy['observed_paths']}")
                print(f"    hashed paths: {policy['hashed_paths']}")
                print(f"    skipped paths: {policy['skipped_paths']}")
                print(f"    skipped reasons: {reasons}")
            if task["pending_review_request"]:
                print(
                    f"  pending_review_request: {task['pending_review_request']} "
                    f"[{task['pending_review_status']}] -> {task['pending_review_reviewer']}"
                )
            if task["unresolved_blocking_review_findings"]:
                print("  unresolved_blocking_review_findings:")
                for finding_id in task["unresolved_blocking_review_findings"]:
                    print(f"  - {finding_id}")
    else:
        print("- none")

    print("unexecuted_verifications:")
    if summary["unexecuted_verifications"]:
        for item in summary["unexecuted_verifications"]:
            print(f"- {item['task_id']}: {item['issue']}")
    else:
        print("- none")

    print("roadmap_assessments:")
    if summary["roadmap_assessments"]:
        for assessment in summary["roadmap_assessments"]:
            item = human_roadmap_assessment_summary(assessment)
            print(f"- {assessment['commitment_id']} {assessment['title']}")
            print(f"  実装タスク: {item['implementation_task_label']}")
            print(f"  ロードマップ状態: {item['roadmap_state_label']}")
            print(f"  確認状況: {item['state_label']}")
            print(f"  止まっている理由: {item['reason']}")
    else:
        print("- none")

    print("closed_roadmap_commitments:")
    if summary["closed_roadmap_commitments"]:
        for commitment in summary["closed_roadmap_commitments"]:
            print(f"- {commitment['id']} {commitment['title']}")
            print(f"  closed_at: {commitment['closed_at'] or 'none'}")
            print(f"  closure_reason: {commitment['closure_reason'] or 'none'}")
    else:
        print("- none")

    print("next_actions:")
    if summary["next_actions"]:
        for action in summary["next_actions"]:
            print(f"- {action}")
    else:
        print("- none")

    print("human_next_actions:")
    if summary["human_next_actions"]:
        for action in summary["human_next_actions"]:
            print(f"- {action}")
    else:
        print("- none")

    print("commit_mapping:")
    for item in summary["commit_mapping"]:
        print(
            f"- {item['task_id']} [{item['status']}] "
            f"base_commit={item['base_commit'] or 'none'} "
            f"latest_verification_head={item['latest_verification_head'] or 'none'}: {item['summary']}"
        )
        for commit in item["commits"]:
            print(f"  - {commit['hash']} {commit['subject']}")
    print("design_residue:")
    for item in summary["design_residue"]:
        print(f"- {item['source']} [{item['status']}] {item['suggested_task_type']}: {item['summary']}")


def print_human_project_status(
    store: Store,
    project: dict,
    active_tasks: list[dict],
    statuses: dict[str, str],
    *,
    current_snapshot: dict | None = None,
) -> None:
    from .failure import summarize_failure_logs
    from .project_logic import (
        human_active_task_lines,
        human_recipe_provenance_label,
        recipe_provenance_summary,
        task_next_actions,
        unexecuted_verifications_for_task,
        unresolved_blocking_review_findings,
    )

    print(f"{field_label('project')}: {project['id']}")
    if not active_tasks:
        print(f"{field_label('status')}: 完了")
        print()
        print(f"{field_label('next_action')}:")
        print("- 作業中のタスクはありません。次に扱う具体的な作業を人間が決めてください。")
        return

    print(f"{field_label('status')}: 作業中")
    print()
    print("作業中のタスク:")

    next_lines: list[str] = []
    snapshot = current_snapshot or current_git_snapshot(Path.cwd(), mode="fast")
    for task in active_tasks[:3]:
        task = {**task, "status": statuses[task["id"]]}
        verification_run = store.latest_for_task("verification_runs", task["id"])
        blocking = unresolved_blocking_review_findings(store, task["id"])
        evidence = commit_aware_evidence_status(verification_run, snapshot, active_task_completion(store, task["id"]), strict=False)
        print(f"- {task['title']}")
        print(f"  {field_label('status')}: {status_label(task['status'])}")
        print(f"  {field_label('evidence')}: {status_label(evidence)}")
        if evidence in {"present", "recorded"}:
            print("  証跡あり。厳密な差分一致は詳細確認で確認してください。")
        for line in human_active_task_lines(task, verification_run, len(blocking)):
            print(f"  {line}")
        recipe_label = human_recipe_provenance_label(recipe_provenance_summary(store, task["id"]))
        if recipe_label:
            print(f"  Recipe: {recipe_label}")
        print()

        status = task["status"]
        unexecuted = unexecuted_verifications_for_task(status, verification_run)
        if blocking:
            next_lines.append("次はその指摘を確認して、修正するか、理由を記録して受け入れれば完了に進めます。")
        else:
            next_lines.append(human_next_action_text(task_next_actions(task, status, verification_run, unexecuted)[0]))

    print(f"{field_label('evidence')}:")
    print("- 上記の各タスク行を確認してください。")
    print()
    if next_lines:
        print(f"{field_label('next_action')}:")
        print(next_lines[0])
        print()
    failure_summary = summarize_failure_logs(store, project_id=project["id"], limit=100000)
    print(f"{field_label('failure_logs')}:")
    print(f"- {field_label('open_failures')}: {failure_summary['open_failure_count']}")
    print(f"- {field_label('high_open_failures')}: {failure_summary['high_open_failure_count']}")
    print()
    print("詳細が必要な場合:")
    print(f"nilo status --project {project['id']} --verbose")
