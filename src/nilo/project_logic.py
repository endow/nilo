from __future__ import annotations

import locale
import re
from pathlib import Path

from .design_residue import parse_design_residue
from .display_labels import field_label, status_label
from .human_status import human_next_action_text, human_project_work_state, human_task_status
from .review_lifecycle import update_review_request
from .reviewer_registry import latest_reviewer_row, reviewer_availability, reviewer_is_registered_available
from .snapshot import commit_aware_evidence_status, current_git_snapshot, evidence_status, review_result_status
from .store import Store
from .task_logic import active_task_completion, is_task_closed_status, is_task_completed_status, outcome_status, projected_task_status, unresolved_blocking_review_findings
from .timeutil import iso_age_seconds, now_iso


REVIEW_CLAIM_STALE_AFTER_SECONDS = 900
SQLITE_IN_CHUNK_SIZE = 500
ROADMAP_GUIDANCE_ACTION = (
    "possible large work; recommend roadmap planning to the human and wait for approval before creating it"
)
LARGE_WORK_STRONG_KEYWORDS = (
    "schema",
    "migration",
    "database",
    "cli",
    "--ai",
    "release",
    "backup",
    "upgrade",
    "roadmap",
    "recipe",
    "mcp",
    "スキーマ",
    "マイグレーション",
    "データベース",
    "コマンド",
    "状態表示",
    "次の作業",
    "AI向け",
    "リリース",
    "バックアップ",
    "アップグレード",
    "ロードマップ",
    "レシピ",
)
LARGE_WORK_WEAK_KEYWORDS = (
    "readme",
    "docs",
    "test",
    "tests",
    "command",
    "status",
    "next",
    "review",
    "failure",
    "ドキュメント",
    "テスト",
    "レビュー",
    "失敗ログ",
)
LARGE_WORK_GUIDANCE_STATUSES = {"planned", "instruction_generated"}


def contains_large_work_keyword(haystack: str, keywords: tuple[str, ...]) -> bool:
    for keyword in keywords:
        lowered = keyword.lower()
        if lowered.isascii() and lowered.replace("-", "").replace("_", "").isalnum():
            if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(lowered)}(?![A-Za-z0-9_-])", haystack):
                return True
        elif lowered in haystack:
            return True
    return False


def count_large_work_keywords(haystack: str, keywords: tuple[str, ...]) -> int:
    count = 0
    for keyword in keywords:
        lowered = keyword.lower()
        if lowered.isascii() and lowered.replace("-", "").replace("_", "").isalnum():
            if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(lowered)}(?![A-Za-z0-9_-])", haystack):
                count += 1
        elif lowered in haystack:
            count += 1
    return count


def git_commit_log(cwd: Path, base_commit: str, latest_head: str) -> list[dict]:
    from .cli import git_commit_log as cli_git_commit_log

    return cli_git_commit_log(cwd, base_commit, latest_head)


def project_current_phase(tasks: list[dict], statuses: dict[str, str]) -> str:
    active = [task for task in tasks if not is_task_closed_status(statuses[task["id"]])]
    if not active:
        return "completed"
    priority = [
        ("implementation", "implementation"),
        ("verification", "verification"),
        ("review", "review"),
        ("design", "design"),
        ("research", "research"),
        ("documentation", "documentation"),
    ]
    active_types = {task["task_type"] for task in active}
    for task_type, phase in priority:
        if task_type in active_types:
            return phase
    return "active"


def project_work_state(tasks: list[dict], statuses: dict[str, str]) -> str:
    active = [task for task in tasks if not is_task_closed_status(statuses[task["id"]])]
    if not active:
        return human_project_work_state(set())
    active_statuses = {statuses[task["id"]] for task in active}
    return human_project_work_state(active_statuses)


def accepted_roadmap_commitments(store: Store, project_id: str) -> list[dict]:
    return store.list_where("roadmap_commitments", "project_id=? AND status='accepted'", (project_id,))


def closed_roadmap_commitments(store: Store, project_id: str) -> list[dict]:
    return store.list_where("roadmap_commitments", "project_id=? AND status='closed'", (project_id,))


def pending_roadmap_revisions(store: Store, project_id: str) -> list[dict]:
    return store.list_where("roadmap_revisions", "project_id=? AND status='pending'", (project_id,))


def pending_roadmap_revision_summaries(store: Store, project_id: str) -> list[dict]:
    revisions = pending_roadmap_revisions(store, project_id)
    enriched = []
    for revision in revisions:
        commitment = store.get("roadmap_commitments", revision["proposed_commitment_id"])
        enriched.append({**revision, "proposed_commitment": commitment or {}})
    return enriched


def related_tasks_for_commitment(tasks: list[dict], commitment: dict) -> list[dict]:
    title = commitment["title"].lower()
    commitment_id = commitment["id"]
    related = []
    for task in tasks:
        if task.get("roadmap_commitment_id") == commitment_id:
            related.append(task)
            continue
        haystack = "\n".join([task["title"], task.get("description") or ""]).lower()
        if commitment_id.lower() in haystack or title in haystack:
            related.append(task)
    return related


def normalize_command_path(value: str) -> str:
    return value.replace("\\", "/").replace(".", "/").lower()


def expected_test_paths_for_file(path: str) -> list[str]:
    normalized = path.replace("\\", "/").strip()
    if not normalized:
        return []
    name = Path(normalized).name
    stem = Path(normalized).stem
    if normalized.startswith("tests/"):
        return [normalized]
    if normalized.startswith("src/nilo/") and name.endswith(".py") and name != "__init__.py":
        return [f"tests/test_{stem}.py"]
    if normalized.startswith("src/") and name.endswith(".py") and name != "__init__.py":
        return [f"tests/test_{stem}.py"]
    return []


def command_runs_broad_test_suite(command: str) -> bool:
    lowered = normalize_command_path(command)
    compact = " ".join(lowered.split())
    return (
        "unittest discover tests" in compact
        or "unittest discover -s tests" in compact
        or "unittest discover --start-directory tests" in compact
        or "pytest tests" in compact
        or "pytest -q tests" in compact
    )


def command_covers_expected_test(command: str, expected_path: str) -> bool:
    normalized_command = normalize_command_path(command)
    normalized_expected = expected_path.replace("\\", "/").lower()
    module_expected = normalized_expected.removesuffix(".py").replace("/", ".").lower()
    slash_module_expected = module_expected.replace(".", "/")
    return (
        normalized_expected in normalized_command
        or module_expected in command.lower()
        or slash_module_expected in normalized_command
    )


def diff_aware_verification_summary(report: dict | None, verification_run: dict | None) -> dict:
    changed_files = report["changed_files"] if report else []
    command = verification_run["command"] if verification_run else ""
    expected: dict[str, list[str]] = {
        path: expected_tests_for_path
        for path in changed_files
        if (expected_tests_for_path := expected_test_paths_for_file(path))
    }
    matched: dict[str, list[str]] = {}
    missing: dict[str, list[str]] = {}
    unknown = [path for path in changed_files if path.replace("\\", "/").startswith("src/") and path not in expected]

    if not changed_files:
        status = "no_changed_files"
        reason = "changed files not reported"
    elif not expected and unknown:
        status = "needs_human_review"
        reason = "no simple source/test mapping for changed source files"
    elif not expected:
        status = "not_applicable"
        reason = "no simple source/test mapping for changed files"
    elif not verification_run:
        status = "needs_human_review"
        reason = "verification run not recorded"
        missing = expected
    else:
        broad_suite = command_runs_broad_test_suite(command)
        for path, expected_tests in expected.items():
            covered = broad_suite or any(command_covers_expected_test(command, expected_test) for expected_test in expected_tests)
            if covered:
                matched[path] = expected_tests
            else:
                missing[path] = expected_tests
        if missing:
            status = "needs_human_review"
            reason = "changed files have no related test command"
        elif unknown:
            status = "needs_human_review"
            reason = "changed files include unknown test mapping"
        else:
            status = "related_test_detected"
            reason = ""

    return {
        "status": status,
        "reason": reason,
        "changed_files": changed_files,
        "expected_tests": expected,
        "matched_tests": matched,
        "missing_tests": missing,
        "unknown_files": unknown,
        "verification_command": command,
    }


def roadmap_task_evidence(store: Store, task: dict, status: str, *, current_snapshot: dict | None = None) -> dict:
    from .state_audit import task_completion_invalid

    report = store.latest_for_task("agent_reports", task["id"])
    verification_run = store.latest_for_task("verification_runs", task["id"])
    review_result = store.latest_for_task("review_results", task["id"])
    current_snapshot = current_snapshot or current_git_snapshot(Path.cwd())
    verification_status = "not_recorded"
    if verification_run:
        if verification_run["timed_out"]:
            verification_status = "timed_out"
        elif verification_run["exit_code"] == 0:
            verification_status = "passed"
        else:
            verification_status = "failed"
    return {
        "task_id": task["id"],
        "title": task["title"],
        "status": status,
        "task_type": task["task_type"],
        "latest_report_id": report["id"] if report else "",
        "latest_evidence_check_id": "",
        "latest_evidence_status": commit_aware_evidence_status(verification_run, current_snapshot, active_task_completion(store, task["id"])),
        "latest_verification_run_id": verification_run["id"] if verification_run else "",
        "latest_verification_status": verification_status,
        "latest_verification_source": verification_run.get("source", "nilo_executed") if verification_run else "",
        "latest_verification_command": verification_run["command"] if verification_run else "",
        "latest_review_result_id": review_result["id"] if review_result else "",
        "latest_review_status": review_result_status(review_result, current_snapshot) if review_result else "missing",
        "unresolved_review_findings": len(store.list_where("review_findings", "task_id=? AND status='unresolved'", (task["id"],))),
        "completion_valid": is_task_completed_status(status) and not task_completion_invalid(store, task["id"], current_snapshot=current_snapshot),
        "diff_verification": diff_aware_verification_summary(report, verification_run),
        "recipe_provenance": recipe_provenance_summary(store, task["id"]),
    }


def roadmap_commitment_assessment(store: Store, commitment: dict, tasks: list[dict], statuses: dict[str, str], *, current_snapshot: dict | None = None) -> dict:
    related_tasks = related_tasks_for_commitment(tasks, commitment)
    current_snapshot = current_snapshot or current_git_snapshot(Path.cwd())
    task_evidence = [roadmap_task_evidence(store, task, statuses[task["id"]], current_snapshot=current_snapshot) for task in related_tasks]
    has_task = bool(task_evidence)
    has_report = all(item["latest_report_id"] for item in task_evidence) if task_evidence else False
    usable_evidence_statuses = {"current", "recorded", "present"}
    has_passed_verification = all(
        item["latest_verification_status"] == "passed" and item["latest_evidence_status"] in usable_evidence_statuses
        for item in task_evidence
    ) if task_evidence else False
    has_failed_verification = any(item["latest_verification_status"] in ("failed", "timed_out") for item in task_evidence)
    has_diff_human_review = any(item["diff_verification"]["status"] == "needs_human_review" for item in task_evidence)
    has_current_review = all(item["latest_review_status"] in {"current", "missing"} for item in task_evidence) if task_evidence else False
    has_unresolved_findings = any(item["unresolved_review_findings"] for item in task_evidence)
    all_completions_valid = all(item["completion_valid"] for item in task_evidence) if task_evidence else False
    active = [item for item in task_evidence if not is_task_closed_status(item["status"])]

    if not has_task:
        overall_status = "task_plan_required"
        unresolved = "no related task"
    elif has_failed_verification:
        overall_status = "needs_reassessment"
        unresolved = "verification failed or timed out"
    elif active and not has_passed_verification:
        overall_status = "needs_verification"
        unresolved = "related task has no passing verification"
    elif not has_report:
        overall_status = "needs_report"
        unresolved = "one or more related tasks have no agent report"
    elif has_unresolved_findings:
        overall_status = "needs_review"
        unresolved = "related task has unresolved review findings"
    elif not has_current_review:
        overall_status = "needs_review"
        unresolved = "related task review is stale"
    elif not all_completions_valid:
        overall_status = "needs_completion_audit"
        unresolved = "related task completion is missing or invalid"
    elif has_diff_human_review:
        overall_status = "needs_human_review"
        unresolved = "diff-aware verification needs human review"
    elif has_passed_verification:
        overall_status = "evidence_present"
        unresolved = ""
    else:
        overall_status = "needs_verification"
        unresolved = "passing verification not recorded"

    criteria = []
    for criterion in commitment["success_criteria"]:
        if not has_task:
            state = "no_related_task"
            reason = "create tasks from roadmap task-plan"
        elif has_failed_verification:
            state = "needs_reassessment"
            reason = "latest related verification failed or timed out"
        elif not has_passed_verification:
            state = "needs_verification"
            reason = "passing verification not recorded"
        elif not has_report:
            state = "needs_report"
            reason = "one or more related tasks have no agent report"
        elif has_unresolved_findings:
            state = "needs_review"
            reason = "related task has unresolved review findings"
        elif not has_current_review:
            state = "needs_review"
            reason = "related task review is stale"
        elif not all_completions_valid:
            state = "needs_completion_audit"
            reason = "related task completion is missing or invalid"
        elif has_diff_human_review:
            state = "needs_human_review"
            reason = "diff-aware verification found changed files without related test command"
        else:
            state = "evidence_present"
            reason = ""
        criteria.append(
            {
                "criterion": criterion,
                "state": state,
                "related_task_ids": [item["task_id"] for item in task_evidence],
                "verification_evidence": [
                    item["latest_verification_run_id"] for item in task_evidence if item["latest_verification_run_id"]
                ],
                "unresolved_reason": reason,
            }
        )

    closure_ready = (
        overall_status == "evidence_present"
        and not unresolved
        and bool(criteria)
        and all(item["state"] == "evidence_present" and not item["unresolved_reason"] for item in criteria)
    )

    return {
        "commitment_id": commitment["id"],
        "title": commitment["title"],
        "status": overall_status,
        "closure_ready": closure_ready,
        "unresolved_reason": unresolved,
        "related_tasks": task_evidence,
        "success_criteria": criteria,
        "evidence_policy": commitment["evidence_policy"],
    }


def roadmap_assessments(
    store: Store,
    project_id: str,
    tasks: list[dict],
    statuses: dict[str, str],
    *,
    commitments: list[dict] | None = None,
    current_snapshot: dict | None = None,
) -> list[dict]:
    current_snapshot = current_snapshot or current_git_snapshot(Path.cwd())
    selected_commitments = commitments if commitments is not None else accepted_roadmap_commitments(store, project_id)
    return [
        roadmap_commitment_assessment(store, commitment, tasks, statuses, current_snapshot=current_snapshot)
        for commitment in selected_commitments
    ]


def auto_close_ready_roadmap_commitments(
    store: Store,
    project_id: str,
    actor: str,
    reason: str,
    commitment_id: str | None = None,
) -> list[dict]:
    from .transitions import TransitionError, close_roadmap_commitment

    tasks, statuses = project_tasks_and_statuses(store, project_id)
    closed = []
    for commitment in accepted_roadmap_commitments(store, project_id):
        if commitment_id and commitment["id"] != commitment_id:
            continue
        related = related_tasks_for_commitment(tasks, commitment)
        if not related:
            continue
        if any(not is_task_closed_status(statuses[task["id"]]) for task in related):
            continue
        assessment = roadmap_commitment_assessment(store, commitment, tasks, statuses)
        if not assessment["closure_ready"]:
            continue
        try:
            close_roadmap_commitment(
                store,
                commitment["id"],
                actor=actor,
                reason=reason,
                closure_ready=True,
                force=False,
            )
        except TransitionError:
            continue
        updated = store.get("roadmap_commitments", commitment["id"]) or commitment
        closed.append(updated)
    return closed


def human_roadmap_assessment_status(status: str) -> dict:
    mapping = {
        "task_plan_required": {
            "state": "タスク計画が必要です。",
            "reason": "ロードマップ項目に対応する実装タスクがまだありません。",
            "next_decisions": ["対応する実装タスクを作成する", "ロードマップ項目を見直す"],
        },
        "needs_verification": {
            "state": "検証記録の確認が必要です。",
            "reason": "関連タスクに成功した検証記録がまだありません。",
            "next_decisions": ["必要なテストを実行して記録する", "検証方針を見直す"],
        },
        "needs_report": {
            "state": "作業報告待ちです。",
            "reason": "関連タスクの完了報告がまだ取り込まれていません。",
            "next_decisions": ["完了報告を取り込む", "報告内容の不足を確認する"],
        },
        "needs_reassessment": {
            "state": "再確認が必要です。",
            "reason": "関連タスクの検証が失敗またはタイムアウトしています。",
            "next_decisions": ["失敗した検証を確認して修正する", "検証を再実行して記録する"],
        },
        "needs_review": {
            "state": "レビュー確認が必要です。",
            "reason": "関連タスクのレビュー結果または未解決指摘を確認する必要があります。",
            "next_decisions": ["レビュー指摘を確認する", "必要な対応を記録する"],
        },
        "needs_completion_audit": {
            "state": "完了記録の確認が必要です。",
            "reason": "関連タスクの完了記録が不足しているか、現在の証跡と一致していません。",
            "next_decisions": ["完了記録を確認する", "必要ならタスクの完了判断をやり直す"],
        },
        "needs_human_review": {
            "state": "人間の確認が必要です。",
            "reason": "変更ファイルに対して、どのテストで確認済みかをNiloが自動判定できませんでした。",
            "next_decisions": ["記録済みテストで十分としてロードマップ項目を閉じる", "追加で targeted test を記録してから閉じる"],
        },
        "evidence_present": {
            "state": "閉じられる状態です。",
            "reason": "実装タスクの報告と成功した検証記録がそろっています。",
            "next_decisions": ["記録済み証跡で十分としてロードマップ項目を閉じる", "追加確認が必要なら対象を指定する"],
        },
    }
    return mapping.get(
        status,
        {
            "state": "状態確認が必要です。",
            "reason": "Nilo がこの状態を人間向け説明へ変換できませんでした。",
            "next_decisions": ["詳細監査出力で状態コードを確認する"],
        },
    )


def human_roadmap_assessment_summary(assessment: dict) -> dict:
    status_text = human_roadmap_assessment_status(assessment["status"])
    related_tasks = assessment["related_tasks"]
    active_tasks = [task for task in related_tasks if not is_task_closed_status(task["status"])]
    passed_verifications = [task for task in related_tasks if task["latest_verification_status"] == "passed"]
    failed_verifications = [
        task for task in related_tasks if task["latest_verification_status"] in ("failed", "timed_out")
    ]
    diff_review_tasks = [
        task for task in related_tasks if task["diff_verification"]["status"] == "needs_human_review"
    ]
    changed_files = sorted(
        {
            path
            for task in diff_review_tasks
            for path in task["diff_verification"].get("changed_files", [])
        }
    )
    missing_tests = sorted(
        {
            test
            for task in diff_review_tasks
            for tests in task["diff_verification"].get("missing_tests", {}).values()
            for test in tests
        }
    )
    unknown_files = sorted(
        {
            path
            for task in diff_review_tasks
            for path in task["diff_verification"].get("unknown_files", [])
        }
    )
    stale_evidence_tasks = [
        task
        for task in related_tasks
        if task["latest_evidence_status"] == "stale"
    ]
    failed_evidence_tasks = [
        task
        for task in related_tasks
        if task["latest_evidence_status"] in {"failed", "timed_out"}
    ]
    commitment_status = assessment.get("commitment_status", "accepted")
    if commitment_status == "closed":
        roadmap_state_label = "クローズ済み"
    elif assessment["closure_ready"]:
        roadmap_state_label = "クローズ可能"
    elif diff_review_tasks:
        roadmap_state_label = "人間確認待ち"
    elif assessment["status"] in {"needs_human_review", "needs_review", "needs_completion_audit", "needs_report", "task_plan_required"}:
        roadmap_state_label = "人間確認待ち"
    else:
        roadmap_state_label = "追加検証待ち"
    if active_tasks:
        implementation_task_label = "残あり"
        work_task_label = "残作業あり"
    elif related_tasks:
        implementation_task_label = "すべて完了"
        work_task_label = "すべて完了"
    else:
        implementation_task_label = "未作成"
        work_task_label = "未作成"
    reason = status_text["reason"]
    if commitment_status == "closed":
        reason = "ロードマップ項目はすでにクローズされています。"
    elif not assessment["closure_ready"] and assessment["status"] == "evidence_present":
        reason = "自動クローズできる状態ではありません。"
    evidence_attention_items = []
    if stale_evidence_tasks:
        evidence_attention_items.append("古い証跡があります")
    if failed_evidence_tasks:
        evidence_attention_items.append("失敗した検証記録があります")
    if diff_review_tasks:
        evidence_attention_items.append("変更ファイルとテストの対応が人間確認待ちです")
    evidence_attention_label = "あり" if evidence_attention_items else "なし"
    return {
        "commitment_id": assessment["commitment_id"],
        "title": assessment["title"],
        "status": assessment["status"],
        "commitment_status": commitment_status,
        "closure_ready": assessment["closure_ready"],
        "state_label": status_text["state"],
        "implementation_task_label": implementation_task_label,
        "work_task_label": work_task_label,
        "roadmap_state_label": roadmap_state_label,
        "reason": reason,
        "next_decisions": status_text["next_decisions"],
        "has_related_tasks": bool(related_tasks),
        "active_task_count": len(active_tasks),
        "passed_verification_count": len(passed_verifications),
        "failed_verification_count": len(failed_verifications),
        "needs_diff_human_review": bool(diff_review_tasks),
        "evidence_attention_label": evidence_attention_label,
        "evidence_attention_items": evidence_attention_items,
        "related_task_ids": [task["task_id"] for task in related_tasks],
        "changed_files": changed_files,
        "missing_tests": missing_tests,
        "unknown_files": unknown_files,
    }


def human_roadmap_summary(assessments: list[dict]) -> dict:
    items = [human_roadmap_assessment_summary(assessment) for assessment in assessments]
    has_evidence_attention = any(item["evidence_attention_items"] for item in items)
    if not items:
        conclusion = "現在、受理済みロードマップ項目はありません。"
        next_judgement = "次に扱う方向性を人間が決めます。"
    elif items and all(item["implementation_task_label"] == "未作成" for item in items):
        conclusion = "実装タスクはまだ作成されていません。"
        next_judgement = "ロードマップ項目から実装タスクを作成するかを人間が判断します。"
    elif items and all(item["implementation_task_label"] != "残あり" for item in items):
        if any(item["status"] == "needs_reassessment" for item in items):
            conclusion = "実装タスクは残っていません。検証の再確認が必要なロードマップ項目があります。"
            next_judgement = "失敗またはタイムアウトした検証を確認します。"
        elif any(item["roadmap_state_label"] == "人間確認待ち" for item in items):
            conclusion = "実装タスクは残っていません。"
            next_judgement = "ロードマップ項目を閉じてよいか、不足している確認があるかを人間が判断します。"
        elif any(item["roadmap_state_label"] == "クローズ可能" for item in items):
            conclusion = "実装タスクは残っていません。"
            next_judgement = "ロードマップ項目を閉じてよいかを人間が判断します。"
        elif any(item["roadmap_state_label"] == "追加検証待ち" for item in items):
            conclusion = "実装タスクは残っていません。"
            next_judgement = "追加検証が必要か、現在の証跡で十分かを人間が判断します。"
        else:
            conclusion = "実装タスクは残っていません。"
            next_judgement = "次に扱う方向性を人間が決めます。"
    elif any(item["status"] == "needs_reassessment" for item in items):
        conclusion = "再確認が必要なロードマップ項目があります。"
        next_judgement = "失敗またはタイムアウトした検証を確認します。"
    elif any(item["status"] == "needs_human_review" for item in items):
        conclusion = "人間確認待ちのロードマップ項目があります。"
        next_judgement = "記録済み証跡で十分か、追加テストが必要かを人間が判断します。"
    elif items and all(item["closure_ready"] for item in items):
        conclusion = "すべての受理済みロードマップ項目は完了候補です。"
        next_judgement = "ロードマップ項目を閉じてよいかを人間が判断します。"
    else:
        conclusion = "まだ完了候補ではないロードマップ項目があります。"
        next_judgement = "不足しているタスク、報告、検証を確認します。"
    if items and all(item["implementation_task_label"] != "残あり" for item in items) and has_evidence_attention:
        evidence_attention = "あり"
    else:
        evidence_attention = "なし"
    if not items:
        work_tasks = "なし"
    elif all(item["implementation_task_label"] != "残あり" for item in items):
        work_tasks = "すべて完了"
    else:
        work_tasks = "残作業あり"
    return {
        "conclusion": conclusion,
        "next_judgement": next_judgement,
        "work_tasks": work_tasks,
        "evidence_attention": evidence_attention,
        "items": items,
    }


def roadmap_proposal_path_for_commitment(store: Store, project_id: str, commitment_id: str | None = None) -> str:
    if commitment_id:
        revisions = store.list_where(
            "roadmap_revisions",
            "project_id=? AND proposed_commitment_id=? AND status='accepted'",
            (project_id, commitment_id),
        )
        for revision in revisions:
            if revision.get("source_path"):
                return revision["source_path"]
    return f".nilo/roadmap/{project_id}/roadmap_proposal.md"


def selected_roadmap_commitment(store: Store, commitments: list[dict], tasks: list[dict], statuses: dict[str, str]) -> dict | None:
    if not commitments:
        return None

    ranked: list[tuple[int, int, dict]] = []
    for index, commitment in enumerate(commitments):
        assessment = roadmap_commitment_assessment(store, commitment, tasks, statuses)
        related = related_tasks_for_commitment(tasks, commitment)
        active = [task for task in related if not is_task_closed_status(statuses[task["id"]])]
        if active:
            rank = 0
        elif assessment["status"] != "evidence_present":
            rank = 1
        elif assessment["closure_ready"]:
            rank = 2
        else:
            rank = 3
        ranked.append((rank, index, commitment))
    return sorted(ranked, key=lambda item: (item[0], item[1]))[0][2]


def ordered_roadmap_commitments(store: Store, commitments: list[dict], tasks: list[dict], statuses: dict[str, str]) -> list[dict]:
    selected = selected_roadmap_commitment(store, commitments, tasks, statuses)
    if not selected:
        return []
    return [selected, *[commitment for commitment in commitments if commitment["id"] != selected["id"]]]


def active_roadmap_commitment(commitments: list[dict], tasks: list[dict], statuses: dict[str, str]) -> dict | None:
    for commitment in commitments:
        related = related_tasks_for_commitment(tasks, commitment)
        if any(not is_task_closed_status(statuses[task["id"]]) for task in related):
            return commitment
    return commitments[0] if commitments else None


def roadmap_prioritized_active_tasks(
    tasks: list[dict],
    statuses: dict[str, str],
    commitments: list[dict],
) -> list[dict]:
    active_tasks = [task for task in tasks if not is_task_closed_status(statuses[task["id"]])]
    commitment = active_roadmap_commitment(commitments, tasks, statuses)
    if not commitment:
        return active_tasks

    related_ids = {task["id"] for task in related_tasks_for_commitment(tasks, commitment)}
    if not related_ids:
        return active_tasks

    return sorted(active_tasks, key=lambda task: 0 if task["id"] in related_ids else 1)


def roadmap_prioritized_project_active_tasks(
    store: Store,
    project_id: str,
    tasks: list[dict],
    statuses: dict[str, str],
) -> tuple[list[dict], list[dict]]:
    commitments = ordered_roadmap_commitments(
        store,
        accepted_roadmap_commitments(store, project_id),
        tasks,
        statuses,
    )
    return roadmap_prioritized_active_tasks(tasks, statuses, commitments), commitments


def human_roadmap_path_for_project(project_id: str) -> str:
    return "ROADMAP.md"


def roadmap_agent_state(store: Store, project_id: str, tasks: list[dict], statuses: dict[str, str]) -> dict | None:
    commitments = accepted_roadmap_commitments(store, project_id)
    commitment = selected_roadmap_commitment(store, commitments, tasks, statuses)
    if not commitment:
        return None

    assessment = roadmap_commitment_assessment(store, commitment, tasks, statuses)
    active_tasks = [
        task
        for task in related_tasks_for_commitment(tasks, commitment)
        if not is_task_closed_status(statuses[task["id"]])
    ]

    if active_tasks:
        work_status = "active"
    elif assessment["status"] == "task_plan_required":
        work_status = "task_plan_required"
    elif assessment["status"] == "evidence_present":
        work_status = "complete"
    else:
        work_status = "incomplete"

    evidence_status = "complete" if assessment["status"] == "evidence_present" else "incomplete"
    verification_status = "complete" if assessment["status"] == "evidence_present" else "incomplete"
    if assessment["status"] == "task_plan_required":
        evidence_status = "missing"
        verification_status = "missing"
    elif assessment["status"] == "needs_reassessment":
        verification_status = "failed"

    closure_status = "awaiting_closure" if assessment["closure_ready"] and not active_tasks else "not_ready"

    if closure_status == "awaiting_closure":
        allowed_actions = ["summarize_current_commitment", "wait_for_user_direction"]
        recommended_next_action = "wait_for_user_direction"
    elif work_status == "task_plan_required":
        allowed_actions = ["summarize_current_commitment", "wait_for_user_direction"]
        recommended_next_action = "wait_for_user_direction"
    elif work_status == "active":
        allowed_actions = ["continue_active_task", "summarize_current_commitment"]
        recommended_next_action = "continue_active_task"
    else:
        allowed_actions = ["summarize_current_commitment", "wait_for_user_direction"]
        recommended_next_action = "wait_for_user_direction"

    return {
        "commitment_id": commitment["id"],
        "commitment_title": commitment["title"],
        "work_status": work_status,
        "evidence_status": evidence_status,
        "verification_status": verification_status,
        "closure_status": closure_status,
        "ai_allowed_actions": allowed_actions,
        "ai_blocked_actions": [],
        "recommended_next_action": recommended_next_action,
    }


def roadmap_agent_next_actions(store: Store, project_id: str, state: dict | None) -> list[dict]:
    if not state:
        return [
            {
                "action_id": "wait_for_user_direction",
                "actor": "ai",
                "status": "allowed",
                "command_hint": "ask the user what to do next; create a task only after a concrete request",
                "reason": "no active task is queued",
            }
        ]

    command_hints = {
        "summarize_current_commitment": "summarize the current commitment and evidence for human review",
        "continue_active_task": "continue the active task shown in active_tasks",
        "wait_for_user_direction": "ask the user what to do next; hide roadmap lifecycle commands unless explicitly requested",
    }
    reasons = {
        "summarize_current_commitment": "summarization is allowed for AI support",
        "continue_active_task": "an active task still needs agent work or evidence review",
        "wait_for_user_direction": "roadmap lifecycle bookkeeping is internal; no user-facing roadmap operation is required",
    }
    actions = []
    for action_id in state["ai_allowed_actions"]:
        actions.append(
            {
                "action_id": action_id,
                "actor": "ai",
                "status": "allowed",
                "command_hint": command_hints[action_id],
                "reason": reasons[action_id],
            }
        )
    for action_id in state["ai_blocked_actions"]:
        actions.append(
            {
                "action_id": action_id,
                "actor": "human",
                "status": "blocked_for_ai",
                "command_hint": "none",
                "reason": f"{action_id} is human-only",
            }
        )
    return actions


def project_roadmap_position(
    tasks: list[dict],
    statuses: dict[str, str],
    design_residue: list[dict],
    commitments: list[dict] | None = None,
) -> str:
    if commitments:
        return f"accepted commitment: {commitments[0]['title']}"
    open_residue = [item for item in design_residue if item["status"] != "resolved"]
    if open_residue:
        return f"design residue open: {open_residue[0]['summary']}"
    active = [task for task in tasks if not is_task_closed_status(statuses[task["id"]])]
    if active:
        priority = {
            "implementation": 0,
            "verification": 1,
            "review": 2,
            "design": 3,
            "research": 4,
            "documentation": 5,
        }
        focus = sorted(active, key=lambda task: priority.get(task["task_type"], 99))[0]
        return f"active task focus: {focus['title']}"
    return "roadmap not configured; no open design residue detected"


def task_looks_like_large_work(task: dict) -> bool:
    if task.get("roadmap_commitment_id"):
        return False

    acceptance = task.get("acceptance_criteria") or []
    description = task.get("description") or ""
    haystack = "\n".join([task.get("title") or "", description, "\n".join(acceptance)]).lower()
    strong_keyword_count = count_large_work_keywords(haystack, LARGE_WORK_STRONG_KEYWORDS)
    weak_keyword = contains_large_work_keyword(haystack, LARGE_WORK_WEAK_KEYWORDS)
    high_caution = task.get("risk_level") == "high" or task.get("requires_understanding_check")
    many_acceptance_items = len(acceptance) >= 3
    very_many_acceptance_items = len(acceptance) >= 5
    long_description = len(description) >= 400
    very_long_description = len(description) >= 800

    if very_many_acceptance_items or very_long_description:
        return True

    breadth_signals = 0
    if strong_keyword_count >= 1:
        breadth_signals += 1
    if strong_keyword_count >= 2:
        breadth_signals += 1
    if weak_keyword:
        breadth_signals += 1
    if high_caution:
        breadth_signals += 1
    if many_acceptance_items:
        breadth_signals += 1
    if long_description:
        breadth_signals += 1

    return breadth_signals >= 3


def large_work_next_actions(task: dict, status: str) -> list[str]:
    if status not in LARGE_WORK_GUIDANCE_STATUSES:
        return []
    return [ROADMAP_GUIDANCE_ACTION] if task_looks_like_large_work(task) else []


def task_next_actions(
    task: dict,
    status: str,
    verification_run: dict | None,
    unexecuted: list[str],
) -> list[str]:
    return [
        *large_work_next_actions(task, status),
        *next_actions_for_task(status, verification_run, unexecuted, task["id"], task["task_type"]),
    ]


def project_level_next_actions(
    store: Store,
    tasks: list[dict],
    statuses: dict[str, str],
    design_residue: list[dict],
    commitments: list[dict],
    pending_revisions: list[dict],
    project_id: str,
) -> list[str]:
    active_tasks = roadmap_prioritized_active_tasks(tasks, statuses, commitments)
    if active_tasks:
        actions = []
        for task in active_tasks[:3]:
            pending_review = latest_pending_review_request(store, task["id"])
            if pending_review:
                actions.append(f"{task['id']}: {next_action_for_review_request(store, pending_review)}")
                continue
            blocking = unresolved_blocking_review_findings(store, task["id"])
            if blocking:
                count_text = f"{len(blocking)}件" if len(blocking) != 1 else "1件"
                actions.append(
                    f"{task['title']}: レビュー指摘が{count_text}残っています。"
                    "指摘を確認して、修正するか、理由を記録して受け入れてください。"
                )
                continue
            status = statuses[task["id"]]
            verification_run = store.latest_for_task("verification_runs", task["id"])
            unexecuted = unexecuted_verifications_for_task(status, verification_run)
            actions.append(
                f"{task['id']}: "
                f"{task_next_actions(task, status, verification_run, unexecuted)[0]}"
            )
        return actions
    if pending_revisions:
        revision = pending_revisions[0]
        commitment = store.get("roadmap_commitments", revision["proposed_commitment_id"])
        title = commitment["title"] if commitment else "missing commitment"
        source = revision.get("source_path") or "none"
        return [
            f"roadmap update pending ({revision['id']} -> {revision['proposed_commitment_id']} {title}; "
            f"source_path: {source}); ask the user whether to adopt or reject the direction"
        ]
    if commitments:
        commitment = selected_roadmap_commitment(store, commitments, tasks, statuses) or commitments[0]
        assessment = roadmap_commitment_assessment(store, commitment, tasks, statuses)
        if assessment["status"] == "task_plan_required":
            return [
                no_active_task_action(
                    f"ask the user for the next concrete task within roadmap commitment {commitment['id']}",
                    project_id,
                )
            ]
        if assessment["status"] != "evidence_present":
            return [
                no_active_task_action(
                    f"roadmap evidence needs internal review ({assessment['unresolved_reason']})",
                    project_id,
                )
            ]
        if assessment["closure_ready"]:
            return [no_active_task_action("current roadmap scope is satisfied, ask the user for the next direction", project_id)]
        return [no_active_task_action("ask the user for the next concrete task", project_id)]
    open_residue = [item for item in design_residue if item["status"] != "resolved"]
    if open_residue:
        return [f"create a task for open design residue: {open_residue[0]['summary']}"]
    return [no_active_task_action("ask the user for the next concrete task or design direction", project_id)]


def no_active_task_action(next_step: str, project_id: str) -> str:
    return (
        "no active task; create or select a Nilo task before implementation; "
        f'if the user already gave a concrete implementation request, run `nilo work "<user request>" --project {project_id}` before code edits; '
        f"{next_step}"
    )


def todo_status_counts(store: Store, project_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for todo in store.list_where("todos", "project_id=?", (project_id,)):
        counts[todo["status"]] = counts.get(todo["status"], 0) + 1
    return dict(sorted(counts.items()))


def todo_next_actions(store: Store, project_id: str) -> list[str]:
    ready = list(reversed(store.list_where("todos", "project_id=? AND status='ready'", (project_id,))))
    if ready:
        return [f"ready todo から task を作成する: nilo todo start --item {ready[0]['id']}"]
    requires_roadmap = list(reversed(store.list_where("todos", "project_id=? AND status='requires_roadmap'", (project_id,))))
    if requires_roadmap:
        return [
            "requires_roadmap todo は作業計画を推奨して人間の承認を待つ: "
            f"承認後に nilo todo promote --item {requires_roadmap[0]['id']} --to roadmap-proposal --reason \"...\""
        ]
    open_todos = list(reversed(store.list_where("todos", "project_id=? AND status='open'", (project_id,))))
    if open_todos:
        return [f"open todo を triage する: nilo todo triage --item {open_todos[0]['id']} --status ready --reason \"...\""]
    deferred = list(reversed(store.list_where("todos", "project_id=? AND status='deferred'", (project_id,))))
    if deferred:
        return [f"deferred todo を再開するか判断する: nilo todo show --item {deferred[0]['id']}"]
    return []


def no_active_task_next_actions(
    store: Store,
    project_id: str,
    base_next_actions: list[str],
) -> list[str]:
    todo_actions = todo_next_actions(store, project_id)
    if not todo_actions:
        return base_next_actions
    if base_next_actions and base_next_actions[0].startswith("roadmap update pending"):
        return base_next_actions + todo_actions

    # Ready and requires_roadmap todos are already triaged and should interrupt
    # roadmap-level planning. Open/deferred todos are lower-priority intake work.
    first = todo_actions[0]
    if first.startswith("ready todo") or first.startswith("requires_roadmap todo"):
        return todo_actions + base_next_actions
    return base_next_actions + todo_actions


def verification_summary(verification_run: dict | None) -> str:
    from .verification_summary import verification_summary as render_verification_summary

    return render_verification_summary(verification_run)


def recipe_provenance_summary(store: Store, task_id: str) -> dict | None:
    provenance = store.latest_for_task("recipe_task_provenance", task_id)
    if not provenance:
        return None
    return {
        "recipe_name": provenance["recipe_name"],
        "source_layer": provenance["source_layer"],
        "source_label": f"{provenance['source_layer']} recipe",
        "source_id": provenance["source_id"],
        "content_hash": provenance["content_hash"],
        "created_at": provenance["created_at"],
        "rendered_fields": provenance["rendered_fields"],
        "recipe_snapshot": provenance["recipe_snapshot"],
    }


def human_recipe_provenance_label(provenance: dict | None) -> str:
    if not provenance:
        return ""
    return f"{provenance['recipe_name']} ({provenance['source_layer']} layer)"


def recipe_completion_warnings(store: Store, task_id: str) -> list[dict]:
    provenance = recipe_provenance_summary(store, task_id)
    if not provenance:
        return []
    contract = provenance.get("recipe_snapshot", {}).get("data", {}).get("completion_contract", {})
    evidence_items = contract.get("evidence", []) if isinstance(contract, dict) else []
    if not isinstance(evidence_items, list):
        return []
    report = store.latest_for_task("agent_reports", task_id)
    report_body = (report or {}).get("body_md", "").casefold()
    warnings = []
    for item in evidence_items:
        if not isinstance(item, str) or not item.strip():
            continue
        if item.casefold() in report_body:
            continue
        warnings.append(
            {
                "severity": "warning",
                "code": "missing_recipe_completion_evidence",
                "recipe_name": provenance["recipe_name"],
                "source_layer": provenance["source_layer"],
                "message": f"Recipe warning: missing completion_contract evidence: {item}",
            }
        )
    return warnings


def recipe_handoff_export_data(store: Store, project: dict, cwd: Path) -> dict:
    from .recipe import discover_recipes

    diagnostics: list[dict] = []
    discovered = discover_recipes(cwd)
    recipe_sources = []
    for source in discovered["all_sources"]:
        entry = source.to_dict()
        entry["body"] = ""
        if source.layer == "project":
            path = Path(source.source_id)
            if path.exists():
                entry["body"] = path.read_text(encoding="utf-8")
            else:
                diagnostics.append(
                    {
                        "severity": "warning",
                        "code": "missing_recipe_source_file",
                        "message": f"recipe source file is missing: {source.source_id}",
                    }
                )
        recipe_sources.append(entry)

    tasks = store.list_where("tasks", "project_id=?", (project["id"],))
    task_by_id = {task["id"]: task for task in tasks}
    provenance_rows = []
    exported_tasks = []
    for task in reversed(tasks):
        rows = list(reversed(store.list_where("recipe_task_provenance", "task_id=?", (task["id"],))))
        if not rows:
            continue
        exported_tasks.append(task)
        for row in rows:
            if row["source_layer"] != "builtin" and row["source_id"] and not Path(row["source_id"]).exists():
                diagnostics.append(
                    {
                        "severity": "warning",
                        "code": "missing_recipe_source_file",
                        "message": f"recipe provenance source file is missing for {task['id']}: {row['source_id']}",
                    }
                )
            provenance_rows.append(row)

    return {
        "schema_version": 1,
        "format": "nilo.recipe_handoff",
        "project_id": project["id"],
        "project_name": project["name"],
        "exported_at": now_iso(),
        "recipe_sources": recipe_sources,
        "tasks": [task_by_id[task["id"]] for task in exported_tasks],
        "recipe_task_provenance": provenance_rows,
        "diagnostics": diagnostics,
    }


def recipe_handoff_import_data(store: Store, project: dict, data: dict, cwd: Path) -> dict:
    if data.get("schema_version") != 1 or data.get("format") != "nilo.recipe_handoff":
        raise SystemExit("unsupported recipe handoff format")
    diagnostics: list[dict] = []
    imported_recipe_files = 0
    imported_tasks = 0
    imported_provenance = 0

    recipe_dir = cwd / ".nilo" / "recipes"
    for source in data.get("recipe_sources", []):
        if source.get("layer") != "project":
            continue
        body = source.get("body") or ""
        if not body:
            diagnostics.append(
                {
                    "severity": "warning",
                    "code": "missing_recipe_definition_body",
                    "message": f"recipe definition body is missing for {source.get('name', '<unknown>')}",
                }
            )
            continue
        recipe_dir.mkdir(parents=True, exist_ok=True)
        filename = Path(source.get("source_id") or f"{source.get('name', 'recipe')}.recipe.yml").name
        (recipe_dir / filename).write_text(body, encoding="utf-8", newline="\n")
        imported_recipe_files += 1

    for task in data.get("tasks", []):
        task_id = task.get("id")
        if not task_id or store.get("tasks", task_id):
            continue
        row = dict(task)
        row["project_id"] = project["id"]
        row.setdefault("description", "")
        row.setdefault("acceptance_criteria", [])
        row.setdefault("parent_task_id", None)
        row.setdefault("split_index", None)
        row.setdefault("task_type", "implementation")
        row.setdefault("risk_level", "medium")
        row.setdefault("requires_understanding_check", False)
        row.setdefault("roadmap_commitment_id", "")
        row.setdefault("roadmap_item_id", "")
        row.setdefault("status", "planned")
        row.setdefault("assigned_model_profile", "")
        row.setdefault("degradation_mode", "normal")
        row.setdefault("mode", "normal")
        row.setdefault("base_commit", None)
        row.setdefault("created_at", now_iso())
        store.insert("tasks", row)
        imported_tasks += 1

    for provenance in data.get("recipe_task_provenance", []):
        provenance_id = provenance.get("id")
        task_id = provenance.get("task_id")
        if not provenance_id or not task_id:
            continue
        if store.get("recipe_task_provenance", provenance_id):
            continue
        if not store.get("tasks", task_id):
            rendered = provenance.get("rendered_fields") or {}
            store.insert(
                "tasks",
                {
                    "id": task_id,
                    "project_id": project["id"],
                    "title": rendered.get("title", f"Imported recipe task {task_id}"),
                    "description": rendered.get("description", ""),
                    "acceptance_criteria": rendered.get("acceptance", []),
                    "parent_task_id": None,
                    "split_index": None,
                    "task_type": "implementation",
                    "risk_level": "medium",
                    "requires_understanding_check": False,
                    "roadmap_commitment_id": "",
                    "roadmap_item_id": "",
                    "status": "planned",
                    "assigned_model_profile": "",
                    "degradation_mode": "normal",
                    "mode": "normal",
                    "base_commit": None,
                    "created_at": now_iso(),
                },
            )
            imported_tasks += 1
        if provenance.get("source_layer") != "builtin" and provenance.get("source_id") and not Path(provenance["source_id"]).exists():
            diagnostics.append(
                {
                    "severity": "warning",
                    "code": "missing_recipe_source_file",
                    "message": f"recipe provenance source file is missing for {task_id}: {provenance['source_id']}",
                }
            )
        store.insert("recipe_task_provenance", dict(provenance))
        imported_provenance += 1

    return {
        "imported_tasks": imported_tasks,
        "imported_provenance": imported_provenance,
        "imported_recipe_files": imported_recipe_files,
        "diagnostics": diagnostics,
    }


def human_verification_summary(verification_run: dict | None) -> str:
    from .verification_summary import human_verification_summary as render_human_verification_summary

    return render_human_verification_summary(verification_run)


def human_active_task_lines(task: dict, verification_run: dict | None, blocking_count: int) -> list[str]:
    title = task["title"]
    status = task["status"]
    if blocking_count:
        if verification_run and not verification_run["timed_out"] and verification_run["exit_code"] == 0:
            intro = f"「{title}」は、実装とテストは済んでいます。"
        else:
            intro = f"「{title}」は、レビュー対応が必要です。"
        return [
            intro,
            f"今はレビュー指摘が{blocking_count}件残っています。",
            human_verification_summary(verification_run),
        ]
    if status == "planned":
        return [f"「{title}」は、作業指示の生成待ちです。"]
    if status == "instruction_generated":
        return [f"「{title}」は、作業中または完了報告待ちです。", human_verification_summary(verification_run)]
    if status in ("agent_reported", "evidence_submitted"):
        return [f"「{title}」は、作業報告済みです。", human_verification_summary(verification_run)]
    if status == "needs_human_review":
        return [f"「{title}」は、人間の確認待ちです。", human_verification_summary(verification_run)]
    if status == "verification_passed":
        return [f"「{title}」は、実装とテストは済んでいます。", human_verification_summary(verification_run)]
    return [f"「{title}」は、対応が必要です。", human_verification_summary(verification_run)]


def verification_working_tree_state(verification_run: dict | None) -> dict:
    from .verification_summary import verification_working_tree_state as render_verification_working_tree_state

    return render_verification_working_tree_state(verification_run)


def verification_working_tree_summary(verification_run: dict | None) -> str:
    from .verification_summary import verification_working_tree_summary as render_verification_working_tree_summary

    return render_verification_working_tree_summary(verification_run)


def verification_snapshot_policy_summary(verification_run: dict | None) -> dict:
    from .verification_summary import verification_snapshot_policy_summary as render_verification_snapshot_policy_summary

    return render_verification_snapshot_policy_summary(verification_run)


def verification_snapshot_policy_lines(verification_run: dict | None) -> list[str]:
    from .verification_summary import verification_snapshot_policy_lines as render_verification_snapshot_policy_lines

    return render_verification_snapshot_policy_lines(verification_run)


def unexecuted_verifications_for_task(status: str, verification_run: dict | None) -> list[str]:
    if verification_run:
        if verification_run["timed_out"]:
            return ["latest verification timed out"]
        if verification_run["exit_code"] != 0:
            return ["latest verification failed"]
        return []
    if status in ("planned", "instruction_generated", "agent_reported", "evidence_submitted", "needs_human_review"):
        return ["verification run not recorded"]
    return []


def clean_verification_task_ready(status: str, verification_run: dict | None, unexecuted: list[str], task_type: str) -> bool:
    return (
        task_type == "verification"
        and status == "evidence_submitted"
        and verification_run is not None
        and not verification_run["timed_out"]
        and verification_run["exit_code"] == 0
        and not verification_working_tree_state(verification_run)["dirty"]
        and not unexecuted
    )


def next_actions_for_task(
    status: str,
    verification_run: dict | None,
    unexecuted: list[str],
    task_id: str = "<task_id>",
    task_type: str = "",
) -> list[str]:
    if status == "review_requested":
        return [f"wait for a real MCP reviewer worker to claim review for task {task_id} with reviewer-claim or claim_next_review, then import_review_result"]
    if status == "review_reviewer_unavailable":
        return ["start a real MCP reviewer worker; reviewer-start only records a heartbeat and does not perform review work"]
    if status == "review_claimed" or status == "review_in_progress":
        return ["wait for the MCP reviewer to import_review_result, or mark the review stale if its claim has expired"]
    if status == "review_stale":
        return ["retry by letting an available MCP reviewer claim the stale review, or reassign before falling back to human review"]
    if status == "review_changes_requested":
        return [
            "レビュー指摘が残っています。指摘を確認し、必要なら修正してから再検証してください。"
            "問題ない指摘なら理由を記録して受け入れてください。"
        ]
    if status == "review_commented":
        return ["review imported findings and decide whether to address them, accept risk, or complete the task"]
    if status == "review_approved":
        return ["run required verification or complete the task if evidence is already sufficient"]
    if status == "planned":
        return [f"run nilo instruct --task {task_id}"]
    if status == "instruction_generated":
        return ["perform the instructed work and import a completion report"]
    if status in ("agent_reported", "evidence_submitted") and unexecuted:
        return [f"run nilo verification run --task {task_id} --command \"...\""]
    if status == "needs_human_review":
        return [
            "review evidence issues, changed files, and verification logs",
            f"ask the human to accept with nilo task complete --task {task_id} --reason \"...\" --actor human --human-acceptance \"...\" or request rework",
        ]
    if verification_run and not verification_run["timed_out"] and verification_run["exit_code"] == 0:
        if clean_verification_task_ready(status, verification_run, unexecuted, task_type):
            return [f"verification evidence is ready; ask the human to accept with nilo task complete --task {task_id} --reason \"...\" --actor human --human-acceptance \"...\""]
        if verification_working_tree_state(verification_run)["dirty"]:
            return [
                "review dirty-tree verification metadata before accepting this task",
                "confirm the verification covered the intended uncommitted files",
                f"if the human accepts, use nilo task complete --task {task_id} --reason \"...\" --actor human --human-acceptance \"...\"",
                "add --commit only when the human explicitly wants Nilo to commit the accepted changes",
            ]
        return [
            "review the diff, reported changed files, verification output, and unresolved caveats",
            f"if the human accepts, use nilo task complete --task {task_id} --reason \"...\" --actor human --human-acceptance \"...\"",
            "add --commit only when the human explicitly wants Nilo to commit the accepted changes",
        ]
    if verification_run and (verification_run["timed_out"] or verification_run["exit_code"] != 0):
        return ["inspect verification output and fix or create a follow-up task"]
    return ["review current task state"]


def latest_pending_review_request(store: Store, task_id: str) -> dict | None:
    latest_result = store.latest_for_task("review_results", task_id)
    rows = store.list_where(
        "review_requests",
        "task_id=? AND status IN ('requested', 'reviewer_unavailable', 'claimed', 'in_progress', 'stale')",
        (task_id,),
    )
    if not latest_result:
        return rows[0] if rows else None
    for row in rows:
        if row["updated_at"] > latest_result["created_at"]:
            return row
    return None


def refresh_review_dispatch_state(
    store: Store,
    project_id: str,
    stale_after_seconds: int = REVIEW_CLAIM_STALE_AFTER_SECONDS,
) -> list[dict]:
    tasks = store.list_where("tasks", "project_id=?", (project_id,))
    task_ids = {task["id"] for task in tasks}
    if not task_ids:
        return []

    now = now_iso()
    changed = []
    pending = store.list_where("review_requests", "status IN ('requested', 'claimed', 'in_progress')")
    for request in pending:
        if request["task_id"] not in task_ids:
            continue
        next_status = ""
        reason = ""
        if request["status"] == "requested" and not reviewer_is_registered_available(store, request["reviewer"]):
            next_status = "reviewer_unavailable"
            reason = "reviewer heartbeat is missing or stale"
        elif request["status"] in {"claimed", "in_progress"} and iso_age_seconds(request["updated_at"]) >= stale_after_seconds:
            next_status = "stale"
            reason = f"review claim exceeded {stale_after_seconds} seconds"
        if not next_status:
            continue
        updated = update_review_request(store, request["id"], {"status": next_status, "updated_at": now})
        changed.append({"review_request": updated, "previous_status": request["status"], "reason": reason})
    return changed


def next_action_for_review_request(store: Store, request: dict) -> str:
    status = request["status"]
    reviewer = request["reviewer"]
    task = store.get("tasks", request["task_id"]) or {}
    project_id = task.get("project_id") or "<project>"
    registration = latest_reviewer_row(store, reviewer)
    availability = reviewer_availability(registration)
    if status == "requested":
        if not reviewer_is_registered_available(store, reviewer):
            return review_worker_recovery_action(reviewer, request["id"], availability)
        return f"MCP reviewer worker {reviewer} should claim review {request['id']} with nilo mcp reviewer-claim or claim_next_review, then import_review_result"
    if status == "reviewer_unavailable":
        return review_worker_recovery_action(reviewer, request["id"], availability)
    if status in ("claimed", "in_progress"):
        return f"wait for MCP reviewer {reviewer} to import result for review {request['id']}, or mark it stale after timeout"
    if status == "stale":
        if availability != "available":
            return review_worker_recovery_action(reviewer, request["id"], availability)
        return f"retry stale review {request['id']} by letting a real MCP reviewer worker use nilo mcp reviewer-claim or claim_next_review, then import_review_result; otherwise reassign before human fallback"
    return f"inspect review request {request['id']} ({status})"


def review_worker_recovery_action(reviewer: str, review_id: str, availability: str) -> str:
    if availability == "heartbeat_only":
        return (
            f"{reviewer} reviewer is heartbeat_only; reviewer-start only records heartbeat and cannot complete review {review_id}; "
            "start a real MCP reviewer worker and use nilo mcp reviewer-claim or claim_next_review, then import_review_result"
        )
    if availability == "stale":
        return (
            f"{reviewer} reviewer heartbeat is stale; start or refresh a real MCP reviewer worker for review {review_id}, "
            "then use nilo mcp reviewer-claim or claim_next_review and import_review_result"
        )
    return (
        f"{reviewer} reviewer is missing; start a real MCP reviewer worker for review {review_id}, "
        "then use nilo mcp reviewer-claim or claim_next_review and import_review_result"
    )


def project_tasks_and_statuses(store: Store, project_id: str) -> tuple[list[dict], dict[str, str]]:
    refresh_review_dispatch_state(store, project_id)
    tasks = project_tasks_in_work_order(store, project_id)
    current_snapshot = current_git_snapshot(Path.cwd())
    latest_events = latest_task_status_events_for_project(store, project_id)
    statuses = {
        task["id"]: projected_task_status(
            store,
            task,
            current_snapshot=current_snapshot,
            latest_event=latest_events.get(task["id"]),
        )
        for task in tasks
    }
    return tasks, statuses


def latest_task_status_events_for_project(store: Store, project_id: str) -> dict[str, dict]:
    rows = store.conn.execute(
        """
        WITH events AS (
          SELECT id AS task_id, id AS event_id, 'task' AS source, status AS status, created_at, rowid AS event_rowid, 10 AS priority FROM tasks WHERE project_id=?
          UNION ALL
          SELECT u.task_id, u.id AS event_id, 'understanding' AS source, u.status AS status, u.created_at, u.rowid AS event_rowid, 20 AS priority FROM understanding_checks u JOIN tasks t ON t.id=u.task_id WHERE t.project_id=?
          UNION ALL
          SELECT i.task_id, i.id AS event_id, 'instruction' AS source, 'instruction_generated' AS status, i.created_at, i.rowid AS event_rowid, 30 AS priority FROM instructions i JOIN tasks t ON t.id=i.task_id WHERE t.project_id=?
          UNION ALL
          SELECT r.task_id, r.id AS event_id, 'agent_report' AS source, 'agent_reported' AS status, r.created_at, r.rowid AS event_rowid, 40 AS priority FROM agent_reports r JOIN tasks t ON t.id=r.task_id WHERE t.project_id=?
          UNION ALL
          SELECT rr.task_id, rr.id AS event_id, 'review_request' AS source, CASE
            WHEN rr.status='requested' THEN 'review_requested'
            WHEN rr.status='reviewer_unavailable' THEN 'review_reviewer_unavailable'
            WHEN rr.status='claimed' THEN 'review_claimed'
            WHEN rr.status='in_progress' THEN 'review_in_progress'
            WHEN rr.status='stale' THEN 'review_stale'
            ELSE 'review_requested'
          END AS status, rr.updated_at AS created_at, rr.rowid AS event_rowid, 45 AS priority FROM review_requests rr JOIN tasks t ON t.id=rr.task_id WHERE t.project_id=? AND rr.status IN ('requested', 'reviewer_unavailable', 'claimed', 'in_progress', 'stale')
          UNION ALL
          SELECT v.task_id, v.id AS event_id, 'verification_run' AS source, CASE WHEN v.timed_out=1 THEN 'verification_timed_out' WHEN v.exit_code=0 THEN 'verification_passed' ELSE 'verification_failed' END AS status, v.created_at, v.rowid AS event_rowid, 55 AS priority FROM verification_runs v JOIN tasks t ON t.id=v.task_id WHERE t.project_id=?
          UNION ALL
          SELECT rv.task_id, rv.id AS event_id, 'review_result' AS source, CASE WHEN rv.verdict='approved' THEN 'review_approved' WHEN rv.verdict='changes_requested' THEN 'review_changes_requested' ELSE 'review_commented' END AS status, rv.created_at, rv.rowid AS event_rowid, 65 AS priority FROM review_results rv JOIN tasks t ON t.id=rv.task_id WHERE t.project_id=?
          UNION ALL
          SELECT rfu.task_id, rfu.id AS event_id, 'review_finding_update' AS source, 'review_changes_requested' AS status, rfu.created_at, rfu.rowid AS event_rowid, 66 AS priority FROM review_finding_updates rfu JOIN tasks t ON t.id=rfu.task_id WHERE t.project_id=?
          UNION ALL
          SELECT c.task_id, c.id AS event_id, 'completion' AS source, CASE WHEN c.actor='ai' THEN 'completed_by_ai' ELSE 'completed_by_user' END AS status, c.created_at, c.rowid AS event_rowid, 70 AS priority FROM task_completions c JOIN tasks t ON t.id=c.task_id WHERE t.project_id=? AND COALESCE(c.invalidated_at, '')=''
        ),
        ranked AS (
          SELECT task_id, event_id, source, status, created_at, ROW_NUMBER() OVER (PARTITION BY task_id ORDER BY created_at DESC, priority DESC, event_rowid DESC) AS rank FROM events
        )
        SELECT task_id, event_id, source, status, created_at FROM ranked WHERE rank=1
        """,
        (project_id,) * 9,
    ).fetchall()
    return {row["task_id"]: store._decode_row(row) for row in rows}


def fast_project_tasks_and_recorded_statuses(store: Store, project_id: str) -> tuple[list[dict], dict[str, str]]:
    """Return project tasks and their latest recorded status without snapshot/audit work."""
    tasks = project_tasks_in_work_order(store, project_id)
    if not tasks:
        return tasks, {}
    rows = store.conn.execute(
        """
        WITH events AS (
          SELECT id AS task_id, id AS event_id, 'task' AS source, status AS status, created_at, rowid AS event_rowid, 10 AS priority FROM tasks WHERE project_id=?
          UNION ALL
          SELECT u.task_id, u.id AS event_id, 'understanding' AS source, u.status AS status, u.created_at, u.rowid AS event_rowid, 20 AS priority FROM understanding_checks u JOIN tasks t ON t.id=u.task_id WHERE t.project_id=?
          UNION ALL
          SELECT i.task_id, i.id AS event_id, 'instruction' AS source, 'instruction_generated' AS status, i.created_at, i.rowid AS event_rowid, 30 AS priority FROM instructions i JOIN tasks t ON t.id=i.task_id WHERE t.project_id=?
          UNION ALL
          SELECT r.task_id, r.id AS event_id, 'agent_report' AS source, 'agent_reported' AS status, r.created_at, r.rowid AS event_rowid, 40 AS priority FROM agent_reports r JOIN tasks t ON t.id=r.task_id WHERE t.project_id=?
          UNION ALL
          SELECT rr.task_id, rr.id AS event_id, 'review_request' AS source, CASE
            WHEN rr.status='requested' THEN 'review_requested'
            WHEN rr.status='reviewer_unavailable' THEN 'review_reviewer_unavailable'
            WHEN rr.status='claimed' THEN 'review_claimed'
            WHEN rr.status='in_progress' THEN 'review_in_progress'
            WHEN rr.status='stale' THEN 'review_stale'
            ELSE 'review_requested'
          END AS status, rr.updated_at AS created_at, rr.rowid AS event_rowid, 45 AS priority FROM review_requests rr JOIN tasks t ON t.id=rr.task_id WHERE t.project_id=? AND rr.status IN ('requested', 'reviewer_unavailable', 'claimed', 'in_progress', 'stale')
          UNION ALL
          SELECT v.task_id, v.id AS event_id, 'verification_run' AS source, CASE WHEN v.timed_out=1 THEN 'verification_timed_out' WHEN v.exit_code=0 THEN 'verification_passed' ELSE 'verification_failed' END AS status, v.created_at, v.rowid AS event_rowid, 55 AS priority FROM verification_runs v JOIN tasks t ON t.id=v.task_id WHERE t.project_id=?
          UNION ALL
          SELECT rv.task_id, rv.id AS event_id, 'review_result' AS source, CASE WHEN rv.verdict='approved' THEN 'review_approved' WHEN rv.verdict='changes_requested' THEN 'review_changes_requested' ELSE 'review_commented' END AS status, rv.created_at, rv.rowid AS event_rowid, 65 AS priority FROM review_results rv JOIN tasks t ON t.id=rv.task_id WHERE t.project_id=?
          UNION ALL
          SELECT rfu.task_id, rfu.id AS event_id, 'review_finding_update' AS source, 'review_changes_requested' AS status, rfu.created_at, rfu.rowid AS event_rowid, 66 AS priority FROM review_finding_updates rfu JOIN tasks t ON t.id=rfu.task_id WHERE t.project_id=?
          UNION ALL
          SELECT c.task_id, c.id AS event_id, 'completion' AS source, CASE WHEN c.actor='ai' THEN 'completed_by_ai' ELSE 'completed_by_user' END AS status, c.created_at, c.rowid AS event_rowid, 70 AS priority FROM task_completions c JOIN tasks t ON t.id=c.task_id WHERE t.project_id=? AND COALESCE(c.invalidated_at, '')=''
          UNION ALL
          SELECT e.entity_id AS task_id, e.id AS event_id, 'outcome' AS source, e.new_state AS status, e.created_at, e.rowid AS event_rowid, 75 AS priority FROM transition_events e JOIN tasks t ON t.id=e.entity_id WHERE t.project_id=? AND e.entity_type='task' AND e.transition='record_outcome_decision' AND e.new_state IN ('rejected', 'partial_accept', 'rework_required')
        ),
        ranked AS (
          SELECT task_id, source, status, ROW_NUMBER() OVER (PARTITION BY task_id ORDER BY created_at DESC, priority DESC, event_rowid DESC) AS rank FROM events
        )
        SELECT task_id, source, status FROM ranked WHERE rank=1
        """,
        (project_id,) * 10,
    ).fetchall()
    statuses = {task["id"]: task["status"] for task in tasks}
    review_update_task_ids = [row["task_id"] for row in rows if row["source"] == "review_finding_update"]
    unresolved_counts: dict[str, int] = {}
    latest_review_verdicts: dict[str, str] = {}
    if review_update_task_ids:
        placeholders = ",".join("?" for _ in review_update_task_ids)
        unresolved_rows = store.conn.execute(
            f"""
            SELECT task_id, COUNT(*) AS count
            FROM review_findings
            WHERE task_id IN ({placeholders}) AND status='unresolved'
            GROUP BY task_id
            """,
            tuple(review_update_task_ids),
        ).fetchall()
        unresolved_counts = {row["task_id"]: row["count"] for row in unresolved_rows}
        review_rows = store.conn.execute(
            f"""
            WITH ranked AS (
              SELECT task_id, verdict, ROW_NUMBER() OVER (PARTITION BY task_id ORDER BY created_at DESC, rowid DESC) AS rank
              FROM review_results
              WHERE task_id IN ({placeholders})
            )
            SELECT task_id, verdict FROM ranked WHERE rank=1
            """,
            tuple(review_update_task_ids),
        ).fetchall()
        latest_review_verdicts = {row["task_id"]: row["verdict"] for row in review_rows}
    for row in rows:
        status = row["status"]
        if row["source"] == "outcome":
            status = outcome_status(status)
        elif row["source"] == "review_finding_update" and not unresolved_counts.get(row["task_id"], 0):
            status = "review_approved" if latest_review_verdicts.get(row["task_id"]) == "approved" else "review_commented"
        statuses[row["task_id"]] = status
    return tasks, statuses


def project_tasks_in_work_order(store: Store, project_id: str) -> list[dict]:
    rows = store.conn.execute(
        """
        SELECT *
        FROM tasks
        WHERE project_id=?
        ORDER BY
          CASE task_type
            WHEN 'implementation' THEN 0
            WHEN 'verification' THEN 1
            WHEN 'review' THEN 2
            WHEN 'design' THEN 3
            WHEN 'research' THEN 4
            WHEN 'documentation' THEN 5
            ELSE 6
          END,
          created_at ASC,
          rowid ASC
        """,
        (project_id,),
    ).fetchall()
    return [store._decode_row(row, "tasks") for row in rows]


def fast_unfinished_verification_targets(store: Store, project_id: str) -> list[dict]:
    tasks, statuses = fast_project_tasks_and_recorded_statuses(store, project_id)
    blocked = {
        "accepted_by_user",
        "accepted_with_concerns",
        "cancelled",
        "canceled",
        "completed_by_ai",
        "completed_by_user",
        "completion_needs_review",
        "rejected_by_user",
    }
    return [task for task in tasks if statuses.get(task["id"], task["status"]) not in blocked]


def task_status_counts(tasks: list[dict], statuses: dict[str, str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in tasks:
        status = statuses[task["id"]]
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def recent_project_history(store: Store, tasks: list[dict], limit: int = 8) -> list[dict]:
    history: list[dict] = []
    task_ids = [task["id"] for task in tasks]
    event_tables = [
        ("instruction", "instructions"),
        ("agent_report", "agent_reports"),
        ("review_request", "review_requests"),
        ("review_result", "review_results"),
        ("verification_run", "verification_runs"),
        ("task_completion", "task_completions"),
    ]
    for task in tasks:
        history.append(
            {
                "event_id": task["id"],
                "task_id": task["id"],
                "event": "task_created",
                "summary": task["title"],
                "created_at": task["created_at"],
            }
        )
    if task_ids:
        for event_name, table in event_tables:
            for chunk in chunked(task_ids, SQLITE_IN_CHUNK_SIZE):
                placeholders = ",".join("?" for _ in chunk)
                for event in store.list_where(table, f"task_id IN ({placeholders})", tuple(chunk)):
                    summary = (
                        event.get("status")
                        or event.get("verdict")
                        or event.get("decision")
                        or event.get("command")
                        or event.get("claimed_status")
                        or event["id"]
                    )
                    history.append(
                        {
                            "event_id": event["id"],
                            "task_id": event["task_id"],
                            "event": event_name,
                            "summary": str(summary),
                            "created_at": event["created_at"],
                        }
                    )
    history.sort(key=lambda item: item["created_at"], reverse=True)
    return history[:limit]


def project_commit_mapping(store: Store, tasks: list[dict], cwd: Path | None = None) -> list[dict]:
    cwd = cwd or Path.cwd()
    mappings = []
    range_counts: dict[tuple[str | None, str | None], int] = {}
    commit_log_cache: dict[tuple[str, str], list[dict]] = {}
    git_log_budget = 8
    latest_verifications = latest_rows_for_tasks(store, "verification_runs", [task["id"] for task in tasks])
    for task in tasks:
        verification_run = latest_verifications.get(task["id"])
        key = (task.get("base_commit"), verification_run["git_head"] if verification_run else None)
        range_counts[key] = range_counts.get(key, 0) + 1

    for task in tasks:
        verification_run = latest_verifications.get(task["id"])
        base_commit = task.get("base_commit")
        latest_head = verification_run["git_head"] if verification_run else None
        commits: list[dict] = []
        if not verification_run:
            status = "unmapped"
            summary = "latest verification run is not recorded"
        elif not base_commit or not latest_head:
            status = "insufficient_git_metadata"
            summary = "base_commit or latest verification git_head is missing"
        elif base_commit == latest_head:
            status = "same_head"
            summary = "base_commit matches latest verification git_head"
        else:
            shared_range = range_counts.get((base_commit, latest_head), 0) > 1
            if shared_range:
                status = "ambiguous"
                summary = "commit range is shared by multiple tasks and needs human review"
            else:
                key = (base_commit, latest_head)
                commits = commit_log_cache.get(key)
                if commits is None:
                    if len(commit_log_cache) >= git_log_budget:
                        status = "ambiguous"
                        summary = "commit range needs human review; git log detail skipped for summary speed"
                        mappings.append(
                            {
                                "task_id": task["id"],
                                "base_commit": base_commit,
                                "latest_verification_head": latest_head,
                                "commits": [],
                                "status": status,
                                "summary": summary,
                            }
                        )
                        continue
                    commits = git_commit_log(cwd, base_commit, latest_head)
                    commit_log_cache[key] = commits
                if len(commits) > 1:
                    status = "ambiguous"
                    summary = "commit range needs human review"
                else:
                    status = "mapped_candidate"
                    summary = "base_commit and latest verification git_head are available"
        mappings.append(
            {
                "task_id": task["id"],
                "base_commit": base_commit,
                "latest_verification_head": latest_head,
                "commits": commits,
                "status": status,
                "summary": summary,
            }
        )
    return mappings


def latest_rows_for_tasks(store: Store, table: str, task_ids: list[str]) -> dict[str, dict]:
    if not task_ids:
        return {}
    latest: dict[str, dict] = {}
    for chunk in chunked(task_ids, SQLITE_IN_CHUNK_SIZE):
        placeholders = ",".join("?" for _ in chunk)
        rows = store.conn.execute(
            f"""
            WITH ranked AS (
              SELECT *, ROW_NUMBER() OVER (PARTITION BY task_id ORDER BY created_at DESC, rowid DESC) AS rank
              FROM {table}
              WHERE task_id IN ({placeholders})
            )
            SELECT * FROM ranked WHERE rank=1
            """,
            tuple(chunk),
        ).fetchall()
        latest.update({row["task_id"]: store._decode_row(row, table) for row in rows})
    return latest


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def project_design_residue(cwd: Path | None = None) -> list[dict]:
    root = cwd or Path.cwd()
    return parse_design_residue(root / "docs" / "design.md")


def project_summary_data(store: Store, project: dict, tasks: list[dict], statuses: dict[str, str]) -> dict:
    from .project_status import project_status_from_inputs

    return project_status_from_inputs(store, project, tasks, statuses)


def handson_language() -> str:
    language = (locale.getlocale()[0] or "").replace("-", "_").lower()
    if language.startswith("ja") or "japanese" in language:
        return "ja"
    return "en"


def handson_text(key: str, language: str) -> str:
    labels = {
        "ja": {
            "title": "# 作業進捗",
            "roadmap_position": "## ロードマップ現在地",
            "work_state": "## 現在の作業状態",
            "current_phase": "## 現在のフェーズ",
            "active_tasks": "## 進行中タスク",
            "recent_history": "## 直近履歴",
            "unexecuted_verifications": "## 未実行検証",
            "next_steps": "## 次のステップ",
            "commit_mapping": "## コミット対応",
            "design_residue": "## 設計との残差",
        },
        "en": {
            "title": "# Progress",
            "roadmap_position": "## Roadmap Position",
            "work_state": "## Work State",
            "current_phase": "## Current Phase",
            "active_tasks": "## Active Tasks",
            "recent_history": "## Recent History",
            "unexecuted_verifications": "## Unexecuted Verifications",
            "next_steps": "## Next Steps",
            "commit_mapping": "## Commit Mapping",
            "design_residue": "## Design Residue",
        },
    }
    return labels.get(language, labels["en"])[key]


def render_handson_next_action(action: str, language: str) -> str:
    if language != "ja":
        return action
    active_task_marker = ": perform the instructed work and import a completion report"
    if active_task_marker in action:
        task_id = action.split(active_task_marker, 1)[0]
        return f"{task_id}: 指示された作業を実施し、完了報告を import する"
    dirty_tree_marker = ": review dirty-tree verification metadata before accepting this task"
    if dirty_tree_marker in action:
        task_id = action.split(dirty_tree_marker, 1)[0]
        return f"{task_id}: dirty-tree の検証メタデータを確認してからタスクを完了する"
    roadmap_discuss_prefix = "run nilo roadmap discuss "
    if action.startswith(roadmap_discuss_prefix):
        command = action.removeprefix("run ")
        return f"ロードマップ相談用コンテキストを作成する: `{command}`"
    stdin_proposal_prefix = "draft a RoadmapProposal from the discussion context, import it with "
    if action.startswith(stdin_proposal_prefix):
        detail = action.removeprefix(stdin_proposal_prefix)
        if " using stdin, accept it with " in detail and ", then publish ROADMAP.md with " in detail:
            import_command, rest = detail.split(" using stdin, accept it with ", 1)
            accept_command, export_command = rest.split(", then publish ROADMAP.md with ", 1)
            return (
                f"相談用コンテキストを材料に RoadmapProposal を作成し、標準入力で `{import_command}` に渡して、"
                f"`{accept_command}` で承認してから `{export_command}` で `ROADMAP.md` を更新する"
            )
        return f"相談用コンテキストを材料に RoadmapProposal を作成する: {detail}"
    stdin_proposal_after_discuss = "draft a RoadmapProposal from the discussion context and import it with "
    if action.startswith(stdin_proposal_after_discuss):
        detail = action.removeprefix(stdin_proposal_after_discuss)
        if " using stdin; accept with " in detail and "; publish the human roadmap with " in detail:
            import_command, rest = detail.split(" using stdin; accept with ", 1)
            accept_command, export_command = rest.split("; publish the human roadmap with ", 1)
            return (
                f"相談用コンテキストを材料に RoadmapProposal を作成し、標準入力で `{import_command}` に渡して、"
                f"`{accept_command}` で承認してから `{export_command}` で人間向けロードマップを更新する"
            )
        return f"相談用コンテキストを材料に RoadmapProposal を作成する: {detail}"
    fresh_proposal_prefix = "write a fresh RoadmapProposal to "
    if action.startswith(fresh_proposal_prefix):
        path, detail = action.removeprefix(fresh_proposal_prefix).split(" from the discussion context, ", 1)
        if detail.startswith("import it, accept it with ") and ", then publish ROADMAP.md with " in detail:
            accept_command, export_command = detail.removeprefix("import it, accept it with ").split(", then publish ROADMAP.md with ", 1)
            return (
                f"相談用コンテキストを材料に `{path}` へ新しい RoadmapProposal を作成し、"
                f"`{accept_command}` で承認してから `{export_command}` で `ROADMAP.md` を更新する"
            )
        return f"相談用コンテキストを材料に `{path}` へ新しい RoadmapProposal を作成する: {detail}"
    stale_checked_proposal_prefix = "verify any existing "
    stale_checked_marker = " is not stale, then write a fresh RoadmapProposal to "
    if action.startswith(stale_checked_proposal_prefix) and stale_checked_marker in action:
        before, rest = action.removeprefix(stale_checked_proposal_prefix).split(stale_checked_marker, 1)
        path, detail = rest.split(" from the discussion context, ", 1)
        command = detail.split(" with ", 1)[1] if " with " in detail else detail
        return f"`{before}` が古い proposal ではないことを確認し、相談用コンテキストを材料に `{path}` へ新しい RoadmapProposal を作成して `{command}`"
    if action.startswith("edit the roadmap proposal, import it, then accept with nilo roadmap accept "):
        command = action.split("accept with ", 1)[1]
        return f"作成したロードマップ案を編集し、import 後に `{command}` で次の RoadmapCommitment として承認する"
    if action.startswith("no active task; create or select a Nilo task before implementation"):
        return "作業中のタスクはありません。次に扱う具体的な作業を人間が決める"
    residue_prefix = "create a task for open design residue: "
    if action.startswith(residue_prefix):
        return f"未解決の設計残差についてタスクを作成する: {action.removeprefix(residue_prefix)}"
    pending_prefix = "review pending roadmap revision "
    pending_marker = "; accept with "
    if action.startswith(pending_prefix) and pending_marker in action:
        detail, command = action.removeprefix(pending_prefix).split(pending_marker, 1)
        command = command.removesuffix(" or revise it")
        if " at " in detail:
            _revision_id, source_path = detail.split(" at ", 1)
            return f"作業計画を確認する: `{source_path}` を読み、これで進めてよければ承認する。承認後は Task 化する"
        return "作業計画を確認し、これで進めてよければ承認する。承認後は Task 化する"
    task_plan_prefix = "create tasks from accepted commitment "
    if action.startswith(task_plan_prefix):
        detail = action.removeprefix(task_plan_prefix)
        return f"承認された作業計画をもとに、具体的な Task に分ける: {detail}"
    assess_prefix = "run nilo roadmap assess "
    if action.startswith(assess_prefix):
        command = action.split(" and ", 1)[0]
        return f"ロードマップ達成状況を確認する: `{command}`"
    return action


def render_handson_roadmap_position(position: str, language: str) -> str:
    if language != "ja":
        return position
    accepted_prefix = "accepted commitment: "
    if position.startswith(accepted_prefix):
        return f"承認済み RoadmapCommitment: {position.removeprefix(accepted_prefix)}"
    design_residue_prefix = "design residue open: "
    if position.startswith(design_residue_prefix):
        return f"未解決の設計残差: {position.removeprefix(design_residue_prefix)}"
    active_task_prefix = "active task focus: "
    if position.startswith(active_task_prefix):
        return f"進行中タスクの焦点: {position.removeprefix(active_task_prefix)}"
    if position == "roadmap not configured; no open design residue detected":
        return "ロードマップ未設定。未解決の設計残差はありません。"
    return position


def render_handson_active_task_next_steps(task: dict, language: str) -> list[str]:
    if language == "ja":
        if task["status"] == "planned":
            return [f"{task['id']}: instruct を生成する"]
        if task["status"] == "instruction_generated":
            return [f"{task['id']}: 作業を実施して完了報告を取り込む"]
        if task["status"] == "verification_passed":
            return [
                f"{task['id']}: 差分、変更ファイル一覧、検証結果、未解決事項を確認する",
                f"{task['id']}: 人間が完了判断する場合は task complete --actor human --human-acceptance で記録し、コミットも任せる場合だけ --commit を付ける",
            ]
        if (
            task["task_type"] == "verification"
            and task["status"] == "evidence_submitted"
            and task["latest_verification_run"] != "none"
            and task["verification_working_tree"] == "clean"
        ):
            return [f"{task['id']}: clean な verification task として証跡は揃っています。人間の完了判断を待ってください"]
        return []

    if task["status"] == "planned":
        return [f"{task['id']}: generate instructions"]
    if task["status"] == "instruction_generated":
        return [f"{task['id']}: do the work and import the completion report"]
    if task["status"] == "verification_passed":
        return [
            f"{task['id']}: review the diff, changed files, verification output, and unresolved caveats",
            f"{task['id']}: if the human accepts, run task complete --actor human --human-acceptance; add --commit only when Nilo should commit too",
        ]
    if (
        task["task_type"] == "verification"
        and task["status"] == "evidence_submitted"
        and task["latest_verification_run"] != "none"
        and task["verification_working_tree"] == "clean"
    ):
        return [f"{task['id']}: clean verification evidence is ready; wait for the human completion decision"]
    return []


def render_handson_markdown(summary: dict) -> str:
    language = handson_language()
    lines = [
        handson_text("title", language),
        "",
        handson_text("roadmap_position", language),
        "",
        render_handson_roadmap_position(summary["roadmap_position"], language),
        "",
        handson_text("work_state", language),
        "",
        summary["work_state"],
        "",
        handson_text("current_phase", language),
        "",
        summary["current_phase"],
        "",
        handson_text("active_tasks", language),
        "",
    ]
    if summary["active_tasks"]:
        for task in summary["active_tasks"]:
            lines.append(f"- {task['id']} [{task['status']}] {task['task_type']} {task['risk_level']} {task['title']}")
            recipe_label = human_recipe_provenance_label(task.get("recipe_provenance"))
            if recipe_label:
                lines.append(f"  - recipe: {recipe_label}")
            lines.append(f"  - latest_verification_run: {task['latest_verification_run']}")
    else:
        lines.append("- none")

    lines.extend(["", handson_text("recent_history", language), ""])
    if summary["recent_history"]:
        for item in summary["recent_history"]:
            lines.append(f"- {item['created_at']} {item['task_id']} {item['event']}: {item['summary']}")
    else:
        lines.append("- none")

    lines.extend(["", handson_text("unexecuted_verifications", language), ""])
    if summary["unexecuted_verifications"]:
        for item in summary["unexecuted_verifications"]:
            lines.append(f"- {item['task_id']}: {item['issue']}")
    else:
        lines.append("- none")

    lines.extend(["", handson_text("next_steps", language), ""])
    next_steps = []
    for task in summary["active_tasks"]:
        next_steps.extend(render_handson_active_task_next_steps(task, language))
    if not next_steps:
        next_steps.extend(render_handson_next_action(action, language) for action in summary["next_actions"])
    if next_steps:
        for step in next_steps[:8]:
            lines.append(f"- {step}")
    else:
        lines.append("- none")

    lines.extend(["", handson_text("commit_mapping", language), ""])
    for item in summary["commit_mapping"]:
        lines.append(
            f"- {item['task_id']} [{item['status']}] "
            f"base_commit={item['base_commit'] or 'none'} "
            f"latest_verification_head={item['latest_verification_head'] or 'none'}: {item['summary']}"
        )
        for commit in item["commits"]:
            lines.append(f"  - {commit['hash']} {commit['subject']}")

    lines.extend(["", handson_text("design_residue", language), ""])
    for item in summary["design_residue"]:
        lines.append(f"- {item['source']} [{item['status']}] {item['suggested_task_type']}: {item['summary']}")

    return "\n".join(lines) + "\n"


def write_handson_markdown(store: Store, project_id: str, output: Path) -> None:
    project = store.get("projects", project_id)
    if not project:
        raise SystemExit(f"project not found: {project_id}")
    tasks, statuses = project_tasks_and_statuses(store, project_id)
    summary = project_summary_data(store, project, tasks, statuses)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_handson_markdown(summary), encoding="utf-8")
