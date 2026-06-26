from __future__ import annotations

import re

from .cli_support import cli_quote

FOCUSED_DEFAULT_EVIDENCE_POLICY = [
    (
        "Record targeted verification for the changed module or focused test group first; "
        "use full verification only for release, broad-risk, or shared-core changes; "
        "if full verification is skipped, document the scope reason instead of treating the skip as a failure."
    )
]


def roadmap_revision_source_label(revision: dict) -> str:
    source_path = revision.get("source_path") or ""
    return f" source_path={source_path}" if source_path else ""


def render_roadmap_assess_markdown(project: dict, assessments: list[dict]) -> str:
    lines = [
        "# Roadmap Assessment",
        "",
        "## Project",
        "",
        f"- project_id: {project['id']}",
        f"- project_name: {project['name']}",
        "",
        "## Accepted Commitments",
        "",
    ]
    if not assessments:
        lines.append("- none")
        return "\n".join(lines) + "\n"

    for assessment in assessments:
        lines.append(f"### {assessment['commitment_id']} {assessment['title']}")
        lines.append("")
        lines.append(f"- status: {assessment['status']}")
        lines.append(f"- closure_ready: {str(assessment['closure_ready']).lower()}")
        lines.append(f"- unresolved_reason: {assessment['unresolved_reason'] or 'none'}")
        lines.append("")
        lines.append("#### Success Criteria")
        lines.append("")
        if assessment["success_criteria"]:
            for item in assessment["success_criteria"]:
                lines.append(f"- [{item['state']}] {item['criterion']}")
                lines.append(f"  - related_tasks: {', '.join(item['related_task_ids']) or 'none'}")
                lines.append(f"  - verification_evidence: {', '.join(item['verification_evidence']) or 'none'}")
                lines.append(f"  - unresolved_reason: {item['unresolved_reason'] or 'none'}")
        else:
            lines.append("- none")
        lines.append("")
        lines.append("#### Related Tasks")
        lines.append("")
        if assessment["related_tasks"]:
            for task in assessment["related_tasks"]:
                lines.append(f"- {task['task_id']} [{task['status']}] {task['task_type']} {task['title']}")
                if task.get("recipe_provenance"):
                    recipe = task["recipe_provenance"]
                    lines.append(f"  - recipe: {recipe['recipe_name']} ({recipe['source_layer']} layer)")
                lines.append(f"  - latest_report: {task['latest_report_id'] or 'none'}")
                lines.append(f"  - latest_evidence_status: {task['latest_evidence_status']}")
                lines.append(
                    f"  - latest_verification: {task['latest_verification_run_id'] or 'none'} "
                    f"({task['latest_verification_status']})"
                )
                if task["latest_verification_source"]:
                    lines.append(f"  - verification_source: {task['latest_verification_source']}")
                if task["latest_verification_command"]:
                    lines.append(f"  - verification_command: {task['latest_verification_command']}")
                diff = task["diff_verification"]
                lines.append(f"  - diff_verification: {diff['status']}")
                if diff["reason"]:
                    lines.append(f"  - diff_reason: {diff['reason']}")
                if diff["changed_files"]:
                    lines.append(f"  - changed_files: {', '.join(diff['changed_files'])}")
                if diff["matched_tests"]:
                    rendered = [
                        f"{source} -> {', '.join(tests)}"
                        for source, tests in diff["matched_tests"].items()
                    ]
                    lines.append(f"  - matched_tests: {'; '.join(rendered)}")
                if diff["missing_tests"]:
                    rendered = [
                        f"{source} -> {', '.join(tests)}"
                        for source, tests in diff["missing_tests"].items()
                    ]
                    lines.append(f"  - missing_tests: {'; '.join(rendered)}")
                if diff.get("unknown_files"):
                    lines.append(f"  - unknown_files: {', '.join(diff['unknown_files'])}")
        else:
            lines.append("- none")
        lines.append("")
        lines.append("#### Evidence Policy")
        lines.append("")
        if assessment["evidence_policy"]:
            for policy in assessment["evidence_policy"]:
                lines.append(f"- {policy}")
        else:
            lines.append("- none")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_human_roadmap_summary_markdown(project: dict, summary: dict) -> str:
    lines = [
        "# 現在の状態",
        "",
        summary["conclusion"],
        f"次に判断すること: {summary['next_judgement']}",
        "",
    ]
    if not summary["items"]:
        return "\n".join(lines).rstrip() + "\n"

    for item in summary["items"]:
        lines.append(f"## {item['title']}")
        lines.append("")
        lines.append(f"{item['title']} は{item['state_label']}")
        if item["active_task_count"]:
            lines.append(f"実装タスクが {item['active_task_count']} 件残っています。")
        elif item["has_related_tasks"]:
            lines.append("実装タスクは残っていません。")
        else:
            lines.append("対応する実装タスクがまだありません。")
        if item["failed_verification_count"]:
            lines.append("テストまたは検証に失敗した記録があります。")
        elif item["passed_verification_count"]:
            lines.append("テストは通っています。")
        else:
            lines.append("成功したテスト記録はまだありません。")
        lines.append("")
        lines.append("止まっている理由:")
        lines.append(item["reason"])
        if item["needs_diff_human_review"]:
            lines.append("変更ファイルとテストコマンドの対応を人間確認待ちです。")
        lines.append("")
        lines.append("人間が判断すること:")
        for decision in item["next_decisions"]:
            lines.append(f"- {decision}")
        lines.append("")
        lines.append("確認対象:")
        if item["related_task_ids"]:
            for task_id in item["related_task_ids"]:
                lines.append(f"- {task_id}")
        else:
            lines.append("- なし")
        if item["changed_files"] or item["missing_tests"] or item["unknown_files"]:
            lines.append("")
            lines.append("証跡紐づけの確認材料:")
            if item["changed_files"]:
                lines.append("- 変更ファイル:")
                for path in item["changed_files"]:
                    lines.append(f"  - {path}")
            if item["missing_tests"]:
                lines.append("- 自動判定できなかった関連テスト:")
                for path in item["missing_tests"]:
                    lines.append(f"  - {path}")
            if item["unknown_files"]:
                lines.append("- 対応するテスト候補を推定できなかったファイル:")
                for path in item["unknown_files"]:
                    lines.append(f"  - {path}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_roadmap_discuss_markdown(summary: dict) -> str:
    lines = [
        "# Roadmap Discussion Context",
        "",
        "## Project",
        "",
        f"- project_id: {summary['project_id']}",
        f"- project_name: {summary['project_name']}",
        f"- roadmap_position: {summary['roadmap_position']}",
        f"- work_state: {summary['work_state']}",
        f"- current_phase: {summary['current_phase']}",
        "",
        "## Accepted Commitments",
        "",
    ]
    if summary["roadmap_commitments"]:
        for commitment in summary["roadmap_commitments"]:
            lines.append(f"- {commitment['id']} {commitment['title']}")
            lines.append(f"  - intent: {commitment['intent'] or 'none'}")
            if commitment["success_criteria"]:
                lines.append("  - success_criteria:")
                for criterion in commitment["success_criteria"]:
                    lines.append(f"    - {criterion}")
    else:
        lines.append("- none")

    lines.extend(["", "## Pending Revisions", ""])
    if summary["pending_roadmap_revisions"]:
        for revision in summary["pending_roadmap_revisions"]:
            lines.extend(render_pending_roadmap_plan_lines(revision, "ja"))
            lines.append("")
            lines.append(
                f"- {revision['id']} [{revision['status']}] "
                f"proposed_commitment={revision['proposed_commitment_id']}{roadmap_revision_source_label(revision)}"
            )
    else:
        lines.append("- none")

    lines.extend(["", "## Active Tasks", ""])
    if summary["active_tasks"]:
        for task in summary["active_tasks"]:
            lines.append(f"- {task['id']} [{task['status']}] {task['task_type']} {task['risk_level']} {task['title']}")
            if task.get("recipe_provenance"):
                recipe = task["recipe_provenance"]
                lines.append(f"  - recipe: {recipe['recipe_name']} ({recipe['source_layer']} layer)")
            lines.append(f"  - latest_verification_run: {task['latest_verification_run']}")
            lines.append(f"  - verification_working_tree: {task['verification_working_tree']}")
    else:
        lines.append("- none")

    lines.extend(["", "## Unexecuted Verifications", ""])
    if summary["unexecuted_verifications"]:
        for item in summary["unexecuted_verifications"]:
            lines.append(f"- {item['task_id']}: {item['issue']}")
    else:
        lines.append("- none")

    lines.extend(["", "## Design Residue", ""])
    if summary["design_residue"]:
        for item in summary["design_residue"]:
            lines.append(f"- {item['source']} [{item['status']}] {item['suggested_task_type']}: {item['summary']}")
    else:
        lines.append("- none")

    lines.extend(
        [
            "",
            "## Requested Output",
            "",
            "Propose a RoadmapRevision. Include Summary, Proposed Changes, Rationale, Success Criteria, Non Goals, Autonomy Scope, Review Gates, Evidence Policy, and Suggested Tasks.",
        ]
    )
    return "\n".join(lines) + "\n"


def pending_roadmap_approval_message(language: str = "ja") -> str:
    if language == "ja":
        return (
            "この作業は少し大きいので、いきなり実装タスクにせず、先に作業計画を作りました。\n\n"
            "以下の内容で進めてよいか確認してください。\n\n"
            "承認すると、この計画をもとに具体的な Nilo Task を作成します。\n"
            "修正したい場合は、「ここを変えて」と指示してください。"
        )
    return (
        "This request is large enough that Nilo created a work plan before implementation.\n\n"
        "Please review the plan below and decide whether it is OK to proceed.\n\n"
        "After approval, Nilo will create concrete Tasks from this plan.\n"
        "If you want changes, describe which part should be changed."
    )


def _pending_revision_commitment(revision: dict) -> dict:
    return revision.get("proposed_commitment") or {}


def render_pending_roadmap_plan_lines(revision: dict, language: str = "ja") -> list[str]:
    commitment = _pending_revision_commitment(revision)
    title = commitment.get("title") or revision.get("title") or "作業計画"
    intent = commitment.get("intent") or "本文を確認してください。"
    success_criteria = commitment.get("success_criteria") or []
    non_goals = commitment.get("non_goals") or []
    review_gates = commitment.get("review_gates") or []
    evidence_policy = commitment.get("evidence_policy") or []
    body = (revision.get("body_md") or "").strip()
    if language == "ja":
        lines = [
            pending_roadmap_approval_message(language),
            "",
            f"### 作業計画: {title}",
            "",
            "#### 目的",
            "",
            intent,
            "",
            "#### やること",
            "",
        ]
        for item in commitment.get("autonomy_scope") or ["本文の Proposed Changes / Scope を確認してください。"]:
            lines.append(f"- {item}")
        lines.extend(["", "#### 完了条件", ""])
        for item in success_criteria or ["完了条件は本文を確認してください。"]:
            lines.append(f"- {item}")
        lines.extend(["", "#### やらないこと", ""])
        for item in non_goals or ["明示された範囲外の変更は行いません。"]:
            lines.append(f"- {item}")
        lines.extend(["", "#### 確認・レビュー観点", ""])
        for item in review_gates or evidence_policy or ["承認前に、目的、範囲、完了条件が意図に合っているか確認してください。"]:
            lines.append(f"- {item}")
        lines.extend(
            [
                "",
                "#### 承認後に作られる Task の見込み",
                "",
                "- 実装 Task",
                "- 検証 Task",
            ]
        )
        if body:
            lines.extend(["", "#### 作業計画本文", "", body])
        return lines

    lines = [
        pending_roadmap_approval_message(language),
        "",
        f"### Work plan: {title}",
        "",
        "#### Purpose",
        "",
        intent,
        "",
        "#### Work to do",
        "",
    ]
    for item in commitment.get("autonomy_scope") or ["Review the Proposed Changes / Scope in the plan body."]:
        lines.append(f"- {item}")
    lines.extend(["", "#### Completion criteria", ""])
    for item in success_criteria or ["Review the plan body for completion criteria."]:
        lines.append(f"- {item}")
    lines.extend(["", "#### Non-goals", ""])
    for item in non_goals or ["Do not change behavior outside the approved plan."]:
        lines.append(f"- {item}")
    lines.extend(["", "#### Review focus", ""])
    for item in review_gates or evidence_policy or ["Confirm that the purpose, scope, and completion criteria match the request."]:
        lines.append(f"- {item}")
    lines.extend(["", "#### Expected Tasks after approval", "", "- Implementation Task", "- Verification Task"])
    if body:
        lines.extend(["", "#### Plan body", "", body])
    return lines


def human_roadmap_status_label(status: str, language: str) -> str:
    labels = {
        "ja": {
            "accepted": "採用済み",
            "closed": "完了",
            "pending": "確認待ち",
            "planned": "準備中",
            "instruction_generated": "作業指示あり",
            "agent_reported": "作業報告済み",
            "evidence_submitted": "作業報告済み",
            "verification_passed": "検証済み",
            "review_requested": "レビュー待ち",
            "review_commented": "レビューコメントあり",
            "review_approved": "レビュー承認済み",
            "review_changes_requested": "修正待ち",
            "needs_human_review": "人間の確認待ち",
        },
        "en": {
            "accepted": "accepted",
            "closed": "complete",
            "pending": "waiting for review",
            "planned": "planned",
            "instruction_generated": "ready to work",
            "agent_reported": "work reported",
            "evidence_submitted": "work reported",
            "verification_passed": "verified",
            "review_requested": "review requested",
            "review_commented": "review commented",
            "review_approved": "review approved",
            "review_changes_requested": "changes requested",
            "needs_human_review": "needs human review",
        },
    }
    return labels.get(language, labels["en"]).get(status, status.replace("_", " "))


def human_roadmap_phase_label(phase: str, language: str) -> str:
    labels = {
        "ja": {
            "completed": "完了",
            "documentation": "ドキュメント更新",
            "implementation": "実装",
            "verification": "検証",
            "review": "レビュー",
            "design": "設計",
            "research": "調査",
            "active": "作業中",
        },
        "en": {
            "completed": "complete",
            "documentation": "documentation",
            "implementation": "implementation",
            "verification": "verification",
            "review": "review",
            "design": "design",
            "research": "research",
            "active": "active",
        },
    }
    return labels.get(language, labels["en"]).get(phase, phase.replace("_", " "))


def human_roadmap_task_type_label(task_type: str, language: str) -> str:
    labels = {
        "ja": {
            "documentation": "ドキュメント更新",
            "implementation": "実装",
            "verification": "検証",
            "review": "レビュー",
            "design": "設計",
            "research": "調査",
            "test_addition": "テスト追加",
            "refactor": "整理",
        },
        "en": {
            "documentation": "documentation",
            "implementation": "implementation",
            "verification": "verification",
            "review": "review",
            "design": "design",
            "research": "research",
            "test_addition": "test addition",
            "refactor": "refactor",
        },
    }
    return labels.get(language, labels["en"]).get(task_type, task_type.replace("_", " "))


INTERNAL_ID_PATTERN = re.compile(r"\b(?:task|commitment|roadmap_rev|review|verification|evidence|report|instruction)_[A-Za-z0-9_]+\b")


def mask_internal_ids(value: str) -> str:
    return INTERNAL_ID_PATTERN.sub("内部ID", value)


def human_roadmap_position_text(value: str, language: str) -> str:
    if language == "ja":
        prefixes = [
            ("採用済みのロードマップ項目: ", "採用済みのロードマップ項目: "),
            ("承認済み RoadmapCommitment: ", "採用済みのロードマップ項目: "),
            ("accepted commitment: ", "採用済みのロードマップ項目: "),
            ("進行中タスクの焦点: ", "進行中の作業: "),
            ("active task focus: ", "進行中の作業: "),
            ("未解決の設計残差: ", "未解決の設計残差: "),
            ("design residue open: ", "未解決の設計残差: "),
        ]
        for prefix, label in prefixes:
            if value.startswith(prefix):
                return f"{label}{mask_internal_ids(value.removeprefix(prefix))}"
        if value == "roadmap not configured; no open design residue detected":
            return "ロードマップ未設定。未解決の設計残差はありません。"
        return mask_internal_ids(value)

    prefixes = [
        ("採用済みのロードマップ項目: ", "accepted commitment: "),
        ("承認済み RoadmapCommitment: ", "accepted commitment: "),
        ("進行中タスクの焦点: ", "active work: "),
        ("未解決の設計残差: ", "open design residue: "),
    ]
    for prefix, label in prefixes:
        if value.startswith(prefix):
            return f"{label}{mask_internal_ids(value.removeprefix(prefix))}"
    if value in ("active task なし", "作業中のタスクはありません。"):
        return "no active work"
    return mask_internal_ids(value)


def human_roadmap_current_direction(summary: dict, language: str) -> str:
    position = summary["roadmap_position"]
    active_focus_prefixes = ("active task focus: ", "進行中タスクの焦点: ", "進行中の作業: ")
    if language == "ja" and position.startswith(active_focus_prefixes) and summary["active_tasks"]:
        task = summary["active_tasks"][0]
        task_type = human_roadmap_task_type_label(task["task_type"], language)
        return f"進行中の作業: {task_type}"
    return human_roadmap_position_text(position, language)


def human_roadmap_work_state_text(value: str, language: str) -> str:
    labels = {
        "ja": {
            "active task なし": "進行中の作業はありません",
            "作業中のタスクはありません。": "進行中の作業はありません",
            "implementation/report 待ち": "作業と報告の完了待ち",
            "作業報告待ちです。": "作業と報告の完了待ち",
            "acceptance review 待ち": "人間の確認待ち",
            "人間の確認待ちです。": "人間の確認待ち",
            "人間の完了判断待ちです。": "人間の完了判断待ち",
            "review 待ち": "レビュー待ち",
            "レビュー依頼中です。": "レビュー待ち",
            "reviewer unavailable": "レビュー担当の準備待ち",
            "レビュー担当の起動待ちです。": "レビュー担当の準備待ち",
            "レビュー中です。": "レビュー中",
            "レビューで修正が必要です。": "レビューで修正が必要",
            "レビュー結果の確認待ちです。": "レビュー結果の確認待ち",
            "レビューが停止しています。": "レビューが停止中",
            "検証に失敗しています。": "検証失敗",
            "検証がタイムアウトしています。": "検証タイムアウト",
            "検証待ちです。": "検証待ち",
            "作業指示の作成待ちです。": "作業指示の作成待ち",
        },
        "en": {
            "active task なし": "no active work",
            "作業中のタスクはありません。": "no active work",
            "implementation/report 待ち": "waiting for work and report",
            "作業報告待ちです。": "waiting for work and report",
            "acceptance review 待ち": "waiting for human review",
            "人間の確認待ちです。": "waiting for human review",
            "人間の完了判断待ちです。": "waiting for human completion decision",
            "review 待ち": "waiting for review",
            "レビュー依頼中です。": "waiting for review",
            "reviewer unavailable": "reviewer unavailable",
            "レビュー担当の起動待ちです。": "waiting for reviewer",
            "レビュー中です。": "review in progress",
            "レビューで修正が必要です。": "changes requested by review",
            "レビュー結果の確認待ちです。": "waiting for review comment triage",
            "レビューが停止しています。": "review stalled",
            "検証に失敗しています。": "verification failed",
            "検証がタイムアウトしています。": "verification timed out",
            "検証待ちです。": "waiting for verification",
            "作業指示の作成待ちです。": "waiting for instructions",
        },
    }
    return labels.get(language, labels["en"]).get(value, mask_internal_ids(value))


def human_roadmap_action_text(action: str, language: str) -> str:
    text = action
    if text.startswith("task_") and ": " in text:
        text = text.split(": ", 1)[1]
    if language == "ja":
        if text == "perform the instructed work and import a completion report":
            return "指示された作業を実施し、完了報告を取り込む"
        if text == "review dirty-tree verification metadata before accepting this task":
            return "未コミット差分を含む検証記録を確認してから作業を完了する"
        if text == "confirm the verification covered the intended uncommitted files":
            return "検証が今回の未コミット差分を対象にしているか確認する"
        if text == "review imported findings and decide whether to address them, accept risk, or complete the task":
            return "レビュー指摘を確認し、対応するか、リスクとして受け入れるか、完了するか判断する"
        if text.startswith("if accepted, run nilo task complete"):
            return "内容に問題がなければ、作業を完了扱いにする"
        if text.startswith("add --commit only when"):
            return "コミットも任せる場合だけ、明示的にコミットを指定する"
    else:
        if text == "active task なし":
            return "no active work"
    return mask_internal_ids(text)


def human_roadmap_active_task_line(task: dict, language: str) -> str:
    task_state = human_roadmap_status_label(task["status"], language)
    task_type = human_roadmap_task_type_label(task["task_type"], language)
    recipe = task.get("recipe_provenance")
    recipe_text = ""
    if recipe:
        recipe_text = f" / recipe: {recipe['recipe_name']} ({recipe['source_layer']} layer)"
    if language == "ja":
        if recipe:
            return f"{task_type}の作業 ({task_state} / recipe: {recipe['recipe_name']} ({recipe['source_layer']} layer))"
        return f"{task_type}の作業 ({task_state})"
    return f"{mask_internal_ids(task['title'])} ({task_state} / {task_type}{recipe_text})"


def render_human_roadmap_markdown(summary: dict, language: str = "en") -> str:
    labels = {
        "ja": {
            "title": "# ロードマップ",
            "project": "プロジェクト",
            "position": "今の方向",
            "work_state": "作業の状態",
            "current_phase": "作業の種類",
            "current_commitment": "## 現在のロードマップ項目",
            "intent": "目的",
            "success_criteria": "#### 成功条件",
            "pending_revisions": "## 確認待ちの案",
            "active_tasks": "## 進行中の作業",
            "next_actions": "## 次に確認すること",
            "none": "なし",
            "pending_revision": "確認待ちのロードマップ案があります。",
        },
        "en": {
            "title": "# Roadmap",
            "project": "Project",
            "position": "Current direction",
            "work_state": "Work state",
            "current_phase": "Work area",
            "current_commitment": "## Current Commitment",
            "intent": "Intent",
            "success_criteria": "#### Success Criteria",
            "pending_revisions": "## Proposals Waiting for Review",
            "active_tasks": "## Work in Progress",
            "next_actions": "## What to Check Next",
            "none": "none",
            "pending_revision": "A roadmap proposal is waiting for review.",
        },
    }
    text = labels.get(language, labels["en"])
    lines = [
        text["title"],
        "",
        f"- {text['project']}: {summary['project_name']}",
        f"- {text['position']}: {human_roadmap_current_direction(summary, language)}",
        f"- {text['work_state']}: {human_roadmap_work_state_text(summary['work_state'], language)}",
        f"- {text['current_phase']}: {human_roadmap_phase_label(summary['current_phase'], language)}",
        "",
        text["current_commitment"],
        "",
    ]
    if summary["roadmap_commitments"]:
        commitment = summary["roadmap_commitments"][0]
        lines.append(f"### {commitment['title']}")
        lines.append("")
        if commitment["intent"]:
            lines.append(f"- {text['intent']}: {commitment['intent']}")
        lines.append("")
        lines.append(text["success_criteria"])
        lines.append("")
        if commitment["success_criteria"]:
            for criterion in commitment["success_criteria"]:
                lines.append(f"- {criterion}")
        else:
            lines.append(f"- {text['none']}")
    else:
        lines.append(f"- {text['none']}")

    lines.extend(["", text["pending_revisions"], ""])
    if summary["pending_roadmap_revisions"]:
        for revision in summary["pending_roadmap_revisions"]:
            lines.extend(render_pending_roadmap_plan_lines(revision, language))
    else:
        lines.append(f"- {text['none']}")

    lines.extend(["", text["active_tasks"], ""])
    if summary["active_tasks"]:
        for task in summary["active_tasks"]:
            lines.append(f"- {human_roadmap_active_task_line(task, language)}")
    else:
        lines.append(f"- {text['none']}")

    lines.extend(["", text["next_actions"], ""])
    if summary["next_actions"]:
        for action in summary["next_actions"]:
            lines.append(f"- {human_roadmap_action_text(action, language)}")
    else:
        lines.append(f"- {text['none']}")

    return "\n".join(lines).rstrip() + "\n"


def task_plan_candidates(commitment: dict) -> list[dict]:
    implementation_acceptance = list(commitment["success_criteria"])
    if not implementation_acceptance:
        implementation_acceptance = [f"{commitment['title']} の実装方針が満たされている"]
    candidates = [
        {
            "title": f"Implement {commitment['title']}",
            "task_type": "implementation",
            "risk": "medium",
            "commitment_id": commitment["id"],
            "description": commitment["intent"] or "承認された作業計画を実装する。",
            "acceptance": implementation_acceptance,
        }
    ]
    evidence_acceptance = list(commitment["evidence_policy"]) or FOCUSED_DEFAULT_EVIDENCE_POLICY
    candidates.append(
        {
            "title": f"Verify {commitment['title']}",
            "task_type": "verification",
            "risk": "medium",
            "commitment_id": commitment["id"],
            "description": "承認された作業計画の証跡ポリシーを満たすことを確認する。",
            "acceptance": evidence_acceptance,
        }
    )
    return candidates


def task_create_command(project_id: str, candidate: dict) -> str:
    parts = [
        "nilo",
        "task",
        "create",
        "--project",
        cli_quote(project_id),
        "--title",
        cli_quote(candidate["title"]),
        "--type",
        candidate["task_type"],
        "--risk",
        candidate["risk"],
        "--commitment",
        candidate["commitment_id"],
        "--description",
        cli_quote(candidate["description"]),
    ]
    for item in candidate["acceptance"]:
        parts.extend(["--acceptance", cli_quote(item)])
    return " ".join(parts)


def render_roadmap_task_plan_markdown(commitment: dict) -> str:
    candidates = task_plan_candidates(commitment)
    lines = [
        "# Roadmap Task Plan",
        "",
        "## Commitment",
        "",
        f"- id: {commitment['id']}",
        f"- project_id: {commitment['project_id']}",
        f"- title: {commitment['title']}",
        f"- status: {commitment['status']}",
        f"- intent: {commitment['intent'] or 'none'}",
        "",
        "## Review Boundaries",
        "",
    ]
    if commitment["review_gates"]:
        for gate in commitment["review_gates"]:
            lines.append(f"- {gate}")
    else:
        lines.append("- none")
    lines.extend(["", "## Task Candidates", ""])
    for index, candidate in enumerate(candidates, start=1):
        lines.append(f"### {index}. {candidate['title']}")
        lines.append("")
        lines.append(f"- type: {candidate['task_type']}")
        lines.append(f"- risk: {candidate['risk']}")
        lines.append(f"- description: {candidate['description']}")
        lines.append("- acceptance:")
        for item in candidate["acceptance"]:
            lines.append(f"  - {item}")
        lines.append("- task_create_command:")
        lines.append("")
        lines.append("```bash")
        lines.append(task_create_command(commitment["project_id"], candidate))
        lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
