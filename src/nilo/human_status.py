from __future__ import annotations


SEVERITIES = {"ok", "info", "warning", "blocked", "error"}


TASK_STATUS_MESSAGES: dict[str, dict[str, str]] = {
    "planned": {
        "state": "作業指示の作成待ちです。",
        "summary": "タスクは作成されていますが、AI が着手するための作業指示はまだ作られていません。",
        "next_action": "作業指示を生成してください。",
        "severity": "info",
    },
    "instruction_generated": {
        "state": "AI が作業中です。",
        "summary": "作業指示は作成済みです。AI の作業報告はまだ記録されていません。",
        "next_action": "指示された作業を進め、完了報告を取り込んでください。",
        "severity": "info",
    },
    "agent_reported": {
        "state": "作業報告が届いています。",
        "summary": "作業報告は記録されていますが、検証結果がまだ十分に記録されていません。",
        "next_action": "検証コマンドを実行して結果を記録してください。",
        "severity": "warning",
    },
    "evidence_submitted": {
        "state": "証跡が提出されています。",
        "summary": "作業報告または検証証跡が記録されています。完了判断には内容確認が必要です。",
        "next_action": "証跡と検証結果を確認してください。",
        "severity": "info",
    },
    "verification_passed": {
        "state": "検証は成功しています。",
        "summary": "成功した検証が記録されています。この作業は人間の完了判断待ちです。",
        "next_action": "差分、変更ファイル、検証結果、未解決事項を確認し、完了として受け入れるか判断してください。",
        "severity": "ok",
    },
    "verification_failed": {
        "state": "検証に失敗しています。",
        "summary": "直近の検証が失敗したため、この作業はまだ完了扱いにできません。",
        "next_action": "検証出力を確認し、修正してから再度検証してください。",
        "severity": "error",
    },
    "verification_timed_out": {
        "state": "検証がタイムアウトしています。",
        "summary": "直近の検証が時間内に完了しませんでした。この作業はまだ完了扱いにできません。",
        "next_action": "検証コマンドや失敗箇所を確認し、必要なら再実行してください。",
        "severity": "error",
    },
    "review_requested": {
        "state": "レビュー依頼中です。",
        "summary": "レビュー依頼は作成されていますが、レビュー結果はまだ記録されていません。",
        "next_action": "レビュー担当が依頼を受け取り、レビュー結果を取り込むのを待ってください。",
        "severity": "info",
    },
    "review_reviewer_unavailable": {
        "state": "レビュー担当の起動待ちです。",
        "summary": "レビュー依頼先が利用可能な状態ではありません。レビューはまだ進んでいません。",
        "next_action": "レビュー担当を起動し、レビュー依頼を受け取れる状態にしてください。",
        "severity": "blocked",
    },
    "review_claimed": {
        "state": "レビュー担当が依頼を受け取りました。",
        "summary": "レビュー担当がレビュー依頼を確保しています。レビュー結果はまだ記録されていません。",
        "next_action": "レビュー結果が取り込まれるのを待ってください。",
        "severity": "info",
    },
    "review_in_progress": {
        "state": "レビュー中です。",
        "summary": "レビュー担当が内容を確認しています。レビュー結果はまだ記録されていません。",
        "next_action": "レビュー結果が取り込まれるのを待ってください。",
        "severity": "info",
    },
    "review_stale": {
        "state": "レビューが停止しています。",
        "summary": "レビュー担当からの結果が一定時間戻っていません。",
        "next_action": "レビュー担当を再確認し、再割り当てまたは再依頼してください。",
        "severity": "blocked",
    },
    "review_approved": {
        "state": "レビューは承認されています。",
        "summary": "レビューでは重大な修正要求は出ていません。完了判断には証跡の確認が必要です。",
        "next_action": "検証結果と差分を確認し、完了として受け入れるか判断してください。",
        "severity": "ok",
    },
    "review_commented": {
        "state": "レビューコメントがあります。",
        "summary": "レビューコメントが記録されています。対応が必要か確認してください。",
        "next_action": "レビューコメントを確認し、修正、受け入れ、または完了判断を行ってください。",
        "severity": "warning",
    },
    "review_changes_requested": {
        "state": "レビューで修正が必要です。",
        "summary": "レビュー指摘が残っているため、この作業はまだ完了扱いにできません。",
        "next_action": "未解決のレビュー指摘を確認し、修正してから再度検証してください。",
        "severity": "warning",
    },
    "needs_human_review": {
        "state": "人間の確認待ちです。",
        "summary": "現在タスクの完了条件診断では、人間の確認が必要です。",
        "next_action": "証跡、差分、検証結果を確認し、完了として受け入れるか判断してください。",
        "severity": "warning",
    },
    "completed_by_user": {
        "state": "このタスクは人間が完了として受け入れました。",
        "summary": "人間の判断により、このタスクは完了済みです。",
        "next_action": "追加対応は不要です。",
        "severity": "ok",
    },
    "completed_by_ai": {
        "state": "このタスクはAIが完了として記録しました。",
        "summary": "AI により完了が記録されています。人間が受け入れた完了とは区別されます。",
        "next_action": "必要に応じて完了記録と証跡を確認してください。",
        "severity": "info",
    },
    "completion_needs_review": {
        "state": "完了記録の確認が必要です。",
        "summary": "完了記録はありますが、証跡・レビュー・失敗ログなどに確認すべき不整合があります。",
        "next_action": "完了記録と監査結果を確認し、必要なら completion を invalidate してください。",
        "severity": "warning",
    },
}


PROJECT_WORK_STATE_PRIORITY = [
    ("review_reviewer_unavailable", "レビュー担当の起動待ちです。"),
    ("review_stale", "レビューが停止しています。"),
    ("review_claimed", "レビュー中です。"),
    ("review_in_progress", "レビュー中です。"),
    ("review_changes_requested", "レビューで修正が必要です。"),
    ("review_requested", "レビュー依頼中です。"),
    ("review_commented", "レビュー結果の確認待ちです。"),
    ("verification_timed_out", "検証がタイムアウトしています。"),
    ("verification_failed", "検証に失敗しています。"),
    ("review_approved", "人間の確認待ちです。"),
    ("completion_needs_review", "完了記録の確認が必要です。"),
    ("needs_human_review", "人間の確認待ちです。"),
    ("verification_passed", "人間の完了判断待ちです。"),
    ("evidence_submitted", "人間の確認待ちです。"),
    ("agent_reported", "検証待ちです。"),
    ("instruction_generated", "作業報告待ちです。"),
    ("planned", "作業指示の作成待ちです。"),
]


def human_task_status(machine_status: str, task: dict | None = None, latest: dict | None = None) -> dict:
    message = TASK_STATUS_MESSAGES.get(machine_status)
    if not message:
        return {
            "state": "状態を確認してください。",
            "summary": f"Nilo が未定義の状態を返しました: {machine_status}",
            "next_action": "最新のタスク状態と履歴を確認してください。",
            "severity": "warning",
            "machine_status": machine_status,
        }
    severity = message["severity"]
    if severity not in SEVERITIES:
        severity = "warning"
    return {
        "state": message["state"],
        "summary": message["summary"],
        "next_action": message["next_action"],
        "severity": severity,
        "machine_status": machine_status,
    }


def human_project_work_state(machine_statuses: set[str]) -> str:
    if not machine_statuses:
        return "作業中のタスクはありません。"
    for status, label in PROJECT_WORK_STATE_PRIORITY:
        if status in machine_statuses:
            return label
    return "状態を確認してください。"


def human_next_action_text(action: str) -> str:
    replacements = {
        "review current task state": "最新のタスク状態を確認してください。",
        "no action available": "次に必要な対応を確認してください。",
        "perform the instructed work and import a completion report": "指示された作業を実施し、完了報告を取り込んでください。",
        "start a real MCP reviewer worker; reviewer-start only records a heartbeat and does not perform review work": "レビュー担当を起動し、レビュー依頼を受け取れる状態にしてください。",
        "wait for the MCP reviewer to import_review_result, or mark the review stale if its claim has expired": "レビュー結果が取り込まれるのを待ち、期限切れならレビュー状態を確認してください。",
        "retry by letting an available MCP reviewer claim the stale review, or reassign before falling back to human review": "利用可能なレビュー担当に再依頼するか、担当を見直してください。",
        "review imported findings and decide whether to address them, accept risk, or complete the task": "レビューコメントを確認し、修正、受け入れ、または完了判断を行ってください。",
        "run required verification or complete the task if evidence is already sufficient": "必要な検証を実行し、証跡が十分なら完了として受け入れるか判断してください。",
        "review the diff, reported changed files, verification output, and unresolved caveats": "差分、変更ファイル、検証結果、未解決事項を確認してください。",
        "review dirty-tree verification metadata before accepting this task": "未コミット差分を含む検証情報を確認してから完了判断してください。",
        "confirm the verification covered the intended uncommitted files": "検証が対象の未コミットファイルを含んでいたか確認してください。",
        "inspect verification output and fix or create a follow-up task": "検証出力を確認し、修正するか follow-up task を作成してください。",
    }
    if action in replacements:
        return replacements[action]
    if action.startswith("run nilo instruct --task "):
        return "作業指示を生成してください。"
    if action.startswith("run nilo verification run --task "):
        return "検証コマンドを実行して結果を記録してください。"
    if action.startswith("wait for a real MCP reviewer worker to claim review"):
        return "レビュー担当が依頼を受け取り、レビュー結果を取り込むのを待ってください。"
    if action.startswith("no active task; create or select a Nilo task before implementation"):
        return "作業中のタスクはありません。次に扱う具体的な作業を人間が決めてください。"
    if action.startswith("no active task; ask the user for the next concrete task"):
        return "作業中のタスクはありません。次に扱う具体的な作業を人間が決めてください。"
    if action.startswith("no active task; current roadmap scope is satisfied"):
        return "作業中のタスクはありません。現在の範囲を終えて次の方向性を人間が決めてください。"
    if action.startswith("no active task; roadmap evidence needs internal review"):
        return "作業中のタスクはありません。ロードマップ証跡の不足を確認してください。"
    if action.startswith("roadmap update pending"):
        return "作業計画を確認し、これで進めてよいか判断してください。承認後、この計画をもとに Task 化します。修正したい場合は、どこを変えるか指示してください。"
    if action.startswith("create a task for open design residue:"):
        return "未解決の設計残差についてタスクを作成してください。"
    if action.startswith("possible large work; recommend roadmap planning"):
        return (
            "この依頼は大きめなので、実装前に作業計画として整理することを推奨します。"
            "人間が承認したら作業計画を作り、その計画をもとに Task 化します。"
        )
    if action.startswith("requires_roadmap todo は作業計画を推奨して人間の承認を待つ"):
        return (
            "この依頼は大きめなので、実装前に作業計画として整理することを推奨します。"
            "人間の承認を待ってから作業計画を作成してください。"
        )
    if action.startswith("ready todo から task を作成する"):
        return "実行できる依頼を具体的な Task にします。"
    if action.startswith("create tasks from accepted commitment"):
        return "承認された作業計画をもとに、具体的な Task に分けます。"
    return action
