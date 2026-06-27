from __future__ import annotations

import locale
import re
from pathlib import Path

from .design_residue import parse_design_residue
from .display_labels import field_label, status_label
from .human_status import human_next_action_text, human_project_work_state, human_task_status
from .reviewer_registry import latest_reviewer_row, reviewer_availability, reviewer_is_registered_available
from .snapshot import current_git_snapshot, evidence_status
from .store import Store
from .task_logic import is_task_completed_status, projected_task_status, unresolved_blocking_review_findings
from .timeutil import iso_age_seconds, now_iso


REVIEW_CLAIM_STALE_AFTER_SECONDS = 900
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
    active = [task for task in tasks if not is_task_completed_status(statuses[task["id"]])]
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
    active = [task for task in tasks if not is_task_completed_status(statuses[task["id"]])]
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


def roadmap_task_evidence(store: Store, task: dict, status: str) -> dict:
    report = store.latest_for_task("agent_reports", task["id"])
    verification_run = store.latest_for_task("verification_runs", task["id"])
    current_snapshot = current_git_snapshot(Path.cwd())
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
        "latest_evidence_status": evidence_status(verification_run, current_snapshot),
        "latest_verification_run_id": verification_run["id"] if verification_run else "",
        "latest_verification_status": verification_status,
        "latest_verification_source": verification_run.get("source", "nilo_executed") if verification_run else "",
        "latest_verification_command": verification_run["command"] if verification_run else "",
        "diff_verification": diff_aware_verification_summary(report, verification_run),
        "recipe_provenance": recipe_provenance_summary(store, task["id"]),
    }


def roadmap_commitment_assessment(store: Store, commitment: dict, tasks: list[dict], statuses: dict[str, str]) -> dict:
    related_tasks = related_tasks_for_commitment(tasks, commitment)
    task_evidence = [roadmap_task_evidence(store, task, statuses[task["id"]]) for task in related_tasks]
    has_task = bool(task_evidence)
    has_report = any(item["latest_report_id"] for item in task_evidence)
    has_passed_verification = any(item["latest_verification_status"] == "passed" for item in task_evidence)
    has_failed_verification = any(item["latest_verification_status"] in ("failed", "timed_out") for item in task_evidence)
    has_diff_human_review = any(item["diff_verification"]["status"] == "needs_human_review" for item in task_evidence)
    active = [item for item in task_evidence if not is_task_completed_status(item["status"])]

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
        unresolved = "related task has no agent report"
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
            reason = "agent report not imported"
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


def roadmap_assessments(store: Store, project_id: str, tasks: list[dict], statuses: dict[str, str]) -> list[dict]:
    return [roadmap_commitment_assessment(store, commitment, tasks, statuses) for commitment in accepted_roadmap_commitments(store, project_id)]


def auto_close_ready_roadmap_commitments(
    store: Store,
    project_id: str,
    actor: str,
    reason: str,
    commitment_id: str | None = None,
) -> list[dict]:
    tasks, statuses = project_tasks_and_statuses(store, project_id)
    closed = []
    for commitment in accepted_roadmap_commitments(store, project_id):
        if commitment_id and commitment["id"] != commitment_id:
            continue
        related = related_tasks_for_commitment(tasks, commitment)
        if not related:
            continue
        if any(not is_task_completed_status(statuses[task["id"]]) for task in related):
            continue
        assessment = roadmap_commitment_assessment(store, commitment, tasks, statuses)
        if not assessment["closure_ready"]:
            continue
        closed_at = now_iso()
        store.update(
            "roadmap_commitments",
            commitment["id"],
            {
                "status": "closed",
                "closed_by": actor,
                "closed_at": closed_at,
                "closure_reason": reason,
            },
        )
        closed.append({**commitment, "closed_by": actor, "closed_at": closed_at, "closure_reason": reason})
    return closed


def human_roadmap_assessment_status(status: str) -> dict:
    mapping = {
        "task_plan_required": {
            "state": "タスク計画が必要です。",
            "reason": "ロードマップ項目に対応する実装タスクがまだありません。",
            "next_decisions": ["対応する実装タスクを作成する", "ロードマップ項目を見直す"],
        },
        "needs_verification": {
            "state": "検証記録待ちです。",
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
        "needs_human_review": {
            "state": "人間確認待ちです。",
            "reason": "Nilo が、変更ファイルとテストコマンドの対応を自動確認しきれませんでした。これはテスト失敗ではありません。",
            "next_decisions": ["記録済みテストで十分としてロードマップ項目を閉じる", "追加で targeted test を記録してから閉じる"],
        },
        "evidence_present": {
            "state": "完了候補です。",
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
    active_tasks = [task for task in related_tasks if not is_task_completed_status(task["status"])]
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
    return {
        "commitment_id": assessment["commitment_id"],
        "title": assessment["title"],
        "status": assessment["status"],
        "closure_ready": assessment["closure_ready"],
        "state_label": status_text["state"],
        "reason": status_text["reason"],
        "next_decisions": status_text["next_decisions"],
        "has_related_tasks": bool(related_tasks),
        "active_task_count": len(active_tasks),
        "passed_verification_count": len(passed_verifications),
        "failed_verification_count": len(failed_verifications),
        "needs_diff_human_review": bool(diff_review_tasks),
        "related_task_ids": [task["task_id"] for task in related_tasks],
        "changed_files": changed_files,
        "missing_tests": missing_tests,
        "unknown_files": unknown_files,
    }


def human_roadmap_summary(assessments: list[dict]) -> dict:
    items = [human_roadmap_assessment_summary(assessment) for assessment in assessments]
    if not items:
        conclusion = "現在、受理済みロードマップ項目はありません。"
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
    return {"conclusion": conclusion, "next_judgement": next_judgement, "items": items}


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
        active = [task for task in related if not is_task_completed_status(statuses[task["id"]])]
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


def roadmap_discussion_path_for_project(project_id: str) -> str:
    return f".nilo/roadmap/{project_id}/roadmap_discussion.md"


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
        if not is_task_completed_status(statuses[task["id"]])
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
    active = [task for task in tasks if not is_task_completed_status(statuses[task["id"]])]
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
    active_tasks = [task for task in tasks if not is_task_completed_status(statuses[task["id"]])]
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
                    f"ask the user for the next concrete task within roadmap commitment {commitment['id']}"
                )
            ]
        if assessment["status"] != "evidence_present":
            return [no_active_task_action(f"roadmap evidence needs internal review ({assessment['unresolved_reason']})")]
        if assessment["closure_ready"]:
            return [no_active_task_action("current roadmap scope is satisfied, ask the user for the next direction")]
        return [no_active_task_action("ask the user for the next concrete task")]
    open_residue = [item for item in design_residue if item["status"] != "resolved"]
    if open_residue:
        return [f"create a task for open design residue: {open_residue[0]['summary']}"]
    return [no_active_task_action("ask the user for the next concrete task or design direction")]


def no_active_task_action(next_step: str) -> str:
    return f"no active task; create or select a Nilo task before implementation; {next_step}"


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
    if not verification_run:
        return "none"
    result = "timed_out" if verification_run["timed_out"] else f"exit_code={verification_run['exit_code']}"
    source = verification_run.get("source", "nilo_executed")
    mode = verification_run.get("metadata", {}).get("verification_mode", "targeted")
    return f"{verification_run['id']} ({result}, source={source}, mode={mode})"


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
    if not verification_run:
        return "直近の検証結果はまだ記録されていません。"
    if verification_run["timed_out"]:
        return "直近の検証はタイムアウトしています。"
    if verification_run["exit_code"] == 0:
        return "直近の検証は成功しています。"
    return "直近の検証は失敗しています。"


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
    metadata = verification_run["metadata"] if verification_run else {}
    return {
        "available": bool(metadata.get("working_tree_available", False)),
        "dirty": bool(metadata.get("working_tree_dirty", False)),
        "files": metadata.get("working_tree_files", []),
    }


def verification_working_tree_summary(verification_run: dict | None) -> str:
    if not verification_run:
        return "none"
    state = verification_working_tree_state(verification_run)
    if not state["available"]:
        return "unavailable"
    if not state["dirty"]:
        return "clean"
    count = len(state["files"])
    return f"dirty ({count} file{'s' if count != 1 else ''})"


def verification_snapshot_policy_summary(verification_run: dict | None) -> dict:
    metadata = verification_run["metadata"] if verification_run else {}
    excluded_paths = metadata.get("snapshot_excluded_paths", [])
    hashed_paths = metadata.get("snapshot_hashed_paths", [])
    reasons: dict[str, int] = {}
    for item in excluded_paths:
        reason = item.get("reason", "unknown") if isinstance(item, dict) else "unknown"
        reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "observed_paths": len(metadata.get("working_tree_files", [])),
        "hashed_paths": len(hashed_paths),
        "skipped_paths": len(excluded_paths),
        "skipped_reasons": reasons,
    }


def verification_snapshot_policy_lines(verification_run: dict | None) -> list[str]:
    summary = verification_snapshot_policy_summary(verification_run)
    if not summary["skipped_paths"]:
        return []
    reasons = ", ".join(f"{reason}={count}" for reason, count in sorted(summary["skipped_reasons"].items())) or "none"
    return [
        "snapshot:",
        f"  observed paths: {summary['observed_paths']}",
        f"  hashed paths: {summary['hashed_paths']}",
        f"  skipped paths: {summary['skipped_paths']}",
        f"  skipped reasons: {reasons}",
    ]


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
            f"accept with nilo task complete --task {task_id} --reason \"...\" --actor ai --commit or request rework",
        ]
    if verification_run and not verification_run["timed_out"] and verification_run["exit_code"] == 0:
        if clean_verification_task_ready(status, verification_run, unexecuted, task_type):
            return [f"run nilo task complete --task {task_id} --reason \"verification evidence accepted\" --actor ai"]
        if verification_working_tree_state(verification_run)["dirty"]:
            return [
                "review dirty-tree verification metadata before accepting this task",
                "confirm the verification covered the intended uncommitted files",
                f"if accepted, run nilo task complete --task {task_id} --reason \"...\" --actor ai",
                "add --commit only when you want Nilo to commit the accepted changes",
            ]
        return [
            "review the diff, reported changed files, verification output, and unresolved caveats",
            f"if accepted, run nilo task complete --task {task_id} --reason \"...\" --actor ai",
            "add --commit only when you want Nilo to commit the accepted changes",
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
        store.update("review_requests", request["id"], {"status": next_status, "updated_at": now})
        updated = store.get("review_requests", request["id"])
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
    tasks = store.list_where("tasks", "project_id=?", (project_id,))
    tasks = list(reversed(tasks))
    statuses = {task["id"]: projected_task_status(store, task) for task in tasks}
    return tasks, statuses


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


def task_status_counts(tasks: list[dict], statuses: dict[str, str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in tasks:
        status = statuses[task["id"]]
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def recent_project_history(store: Store, tasks: list[dict], limit: int = 8) -> list[dict]:
    history: list[dict] = []
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
        for event_name, table in event_tables:
            for event in store.list_where(table, "task_id=?", (task["id"],)):
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
                        "task_id": task["id"],
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
    for task in tasks:
        verification_run = store.latest_for_task("verification_runs", task["id"])
        key = (task.get("base_commit"), verification_run["git_head"] if verification_run else None)
        range_counts[key] = range_counts.get(key, 0) + 1

    for task in tasks:
        verification_run = store.latest_for_task("verification_runs", task["id"])
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
            commits = git_commit_log(cwd, base_commit, latest_head)
            shared_range = range_counts.get((base_commit, latest_head), 0) > 1
            if len(commits) > 1 or shared_range:
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


def project_design_residue(cwd: Path | None = None) -> list[dict]:
    root = cwd or Path.cwd()
    return parse_design_residue(root / "docs" / "design.md")


def project_summary_data(store: Store, project: dict, tasks: list[dict], statuses: dict[str, str]) -> dict:
    active_tasks = [task for task in tasks if not is_task_completed_status(statuses[task["id"]])]
    active_summaries = []
    unexecuted = []
    design_residue = project_design_residue()
    commitments = ordered_roadmap_commitments(
        store,
        accepted_roadmap_commitments(store, project["id"]),
        tasks,
        statuses,
    )
    closed_commitments = closed_roadmap_commitments(store, project["id"])
    pending_revisions = pending_roadmap_revision_summaries(store, project["id"])
    for task in active_tasks:
        status = statuses[task["id"]]
        verification_run = store.latest_for_task("verification_runs", task["id"])
        pending_review = latest_pending_review_request(store, task["id"])
        blocking_findings = unresolved_blocking_review_findings(store, task["id"])
        recipe_provenance = recipe_provenance_summary(store, task["id"])
        active_summaries.append(
            {
                "id": task["id"],
                "title": task["title"],
                "status": status,
                "human_status": human_task_status(status, task, {"verification_run": verification_run}),
                "task_type": task["task_type"],
                "risk_level": task["risk_level"],
                "latest_verification_run": verification_summary(verification_run),
                "verification_working_tree": verification_working_tree_summary(verification_run),
                "verification_working_tree_dirty": verification_working_tree_state(verification_run)["dirty"],
                "verification_working_tree_available": verification_working_tree_state(verification_run)["available"],
                "verification_working_tree_files": verification_working_tree_state(verification_run)["files"],
                "verification_snapshot_policy": verification_snapshot_policy_summary(verification_run),
                "pending_review_request": pending_review["id"] if pending_review else "",
                "pending_review_reviewer": pending_review["reviewer"] if pending_review else "",
                "pending_review_status": pending_review["status"] if pending_review else "",
                "unresolved_blocking_review_findings": [finding["id"] for finding in blocking_findings],
                "recipe_provenance": recipe_provenance,
            }
        )
        for item in unexecuted_verifications_for_task(status, verification_run):
            unexecuted.append({"task_id": task["id"], "issue": item})
    agent_state = roadmap_agent_state(store, project["id"], tasks, statuses)
    base_next_actions = project_level_next_actions(store, tasks, statuses, design_residue, commitments, pending_revisions, project["id"])
    next_actions = base_next_actions
    if not active_tasks:
        next_actions = no_active_task_next_actions(store, project["id"], base_next_actions)
    return {
        "project_id": project["id"],
        "project_name": project["name"],
        "roadmap_position": project_roadmap_position(tasks, statuses, design_residue, commitments),
        "roadmap_commitments": commitments,
        "closed_roadmap_commitments": closed_commitments,
        "pending_roadmap_revisions": pending_revisions,
        "roadmap_assessments": roadmap_assessments(store, project["id"], tasks, statuses),
        "roadmap_agent_state": agent_state,
        "roadmap_agent_next_actions": roadmap_agent_next_actions(store, project["id"], agent_state),
        "work_state": project_work_state(tasks, statuses),
        "current_phase": project_current_phase(tasks, statuses),
        "next_actions": next_actions,
        "human_next_actions": [human_next_action_text(action) for action in next_actions],
        "todo_status_counts": todo_status_counts(store, project["id"]),
        "task_status_counts": task_status_counts(tasks, statuses),
        "recent_history": recent_project_history(store, tasks),
        "active_tasks": active_summaries,
        "unexecuted_verifications": unexecuted,
        "commit_mapping": project_commit_mapping(store, tasks),
        "design_residue": design_residue,
    }


def print_project_summary_text(summary: dict) -> None:
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
            print(f"- {assessment['commitment_id']} [{assessment['status']}] {assessment['title']}")
            print(f"  closure_ready: {str(assessment['closure_ready']).lower()}")
            if assessment["unresolved_reason"]:
                print(f"  unresolved_reason: {assessment['unresolved_reason']}")
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


def print_human_project_status(store: Store, project: dict, active_tasks: list[dict], statuses: dict[str, str]) -> None:
    from .failure import summarize_failure_logs

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
    for task in active_tasks[:3]:
        task = {**task, "status": statuses[task["id"]]}
        verification_run = store.latest_for_task("verification_runs", task["id"])
        blocking = unresolved_blocking_review_findings(store, task["id"])
        evidence = evidence_status(verification_run, current_git_snapshot(Path.cwd()))
        print(f"- {task['title']}")
        print(f"  {field_label('status')}: {status_label(task['status'])}")
        print(f"  {field_label('evidence')}: {status_label(evidence)}")
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
            f"{task['id']}: 承認する場合は task complete で完了を記録し、コミットも任せる場合だけ --commit を付ける",
        ]
    if (
        task["task_type"] == "verification"
        and task["status"] == "evidence_submitted"
        and task["latest_verification_run"] != "none"
        and task["verification_working_tree"] == "clean"
    ):
        return [f"{task['id']}: clean な verification task として task complete を実行する"]
    return []
    if task["status"] == "planned":
        return [f"{task['id']}: generate instructions"]
    if task["status"] == "instruction_generated":
        return [f"{task['id']}: do the work and import the completion report"]
    if task["status"] == "verification_passed":
        return [
            f"{task['id']}: review the diff, changed files, verification output, and unresolved caveats",
            f"{task['id']}: if accepted, run task complete; add --commit only when Nilo should commit too",
        ]
    if (
        task["task_type"] == "verification"
        and task["status"] == "evidence_submitted"
        and task["latest_verification_run"] != "none"
        and task["verification_working_tree"] == "clean"
    ):
        return [f"{task['id']}: run task complete for the clean verification task"]
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
