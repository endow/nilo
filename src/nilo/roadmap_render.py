from __future__ import annotations

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
        "# ロードマップ状態",
        "",
        project["name"],
        "",
        f"作業タスク: {summary.get('work_tasks', '確認が必要です')}",
        f"証跡注意: {summary.get('evidence_attention', 'なし')}",
        "",
        summary["conclusion"],
        "",
    ]
    lines.append("次に判断すること:")
    lines.append(f"- {summary['next_judgement']}")
    lines.append("")
    if not summary["items"]:
        return "\n".join(lines).rstrip() + "\n"

    for item in summary["items"]:
        lines.append(f"## {item['title']}")
        lines.append("")
        lines.append(f"- 作業タスク: {item.get('work_task_label', item['implementation_task_label'])}")
        lines.append(f"- ロードマップ: {item['roadmap_state_label']}")
        lines.append(f"- 確認状況: {item['state_label']}")
        if item["failed_verification_count"]:
            lines.append("- 検証: テストまたは検証に失敗した記録があります。")
        elif item["passed_verification_count"]:
            lines.append("- 検証: テストは通っています。")
        else:
            lines.append("- 検証: 成功したテスト記録はまだありません。")
        lines.append(f"- 証跡注意: {item.get('evidence_attention_label', 'なし')}")
        lines.append("")
        if item.get("evidence_attention_items"):
            lines.append("注意:")
            for attention in item["evidence_attention_items"]:
                lines.append(f"- {attention}")
            lines.append("")
        if item["reason"]:
            lines.append("止まっている理由:")
            lines.append(item["reason"])
            if item["needs_diff_human_review"]:
                lines.append("変更ファイルに対して、どのテストで確認済みかをNiloが自動判定できませんでした。")
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
                lines.append("- changed_files:")
                for path in item["changed_files"]:
                    lines.append(f"  - {path}")
            if item["missing_tests"]:
                lines.append("- missing_tests:")
                for path in item["missing_tests"]:
                    lines.append(f"  - {path}")
            if item["unknown_files"]:
                lines.append("- unknown_files:")
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




def task_plan_candidates(commitment: dict, primary_language: str = "ja") -> list[dict]:
    implementation_acceptance = list(commitment["success_criteria"])
    if not implementation_acceptance:
        implementation_acceptance = [f"{commitment['title']} の実装方針が満たされている"]
    if primary_language == "ja":
        implementation_title = f"{commitment['title']} を実装する"
        implementation_description = commitment["intent"] or "承認された作業計画を実装する。"
        verification_title = f"{commitment['title']} を検証する"
        verification_description = "承認された作業計画の証跡ポリシーを満たすことを確認する。"
    else:
        implementation_title = f"Implement {commitment['title']}"
        implementation_description = commitment["intent"] or "Implement the accepted work plan."
        verification_title = f"Verify {commitment['title']}"
        verification_description = "Verify that the accepted work plan's evidence policy is satisfied."
    candidates = [
        {
            "title": implementation_title,
            "task_type": "implementation",
            "risk": "medium",
            "commitment_id": commitment["id"],
            "description": implementation_description,
            "acceptance": implementation_acceptance,
        }
    ]
    evidence_acceptance = list(commitment["evidence_policy"]) or FOCUSED_DEFAULT_EVIDENCE_POLICY
    candidates.append(
        {
            "title": verification_title,
            "task_type": "verification",
            "risk": "medium",
            "commitment_id": commitment["id"],
            "description": verification_description,
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


def render_roadmap_task_plan_markdown(commitment: dict, primary_language: str = "ja") -> str:
    candidates = task_plan_candidates(commitment, primary_language)
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
