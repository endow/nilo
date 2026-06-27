from __future__ import annotations

STATUS_LABELS_JA = {
    "missing": "未提出",
    "present": "提出あり",
    "failed": "失敗",
    "stale": "古い証跡",
    "current": "現在の差分に対応",
    "open": "未解決",
    "resolved": "解決済み",
    "ignored": "無視",
    "accepted": "受理",
    "accepted_with_concerns": "懸念付き受理",
    "rejected": "差し戻し",
    "rework_required": "修正必要",
    "pending": "保留中",
    "completed": "完了",
    "in_progress": "作業中",
    "todo": "未着手",
    "blocked": "ブロック中",
    "active": "有効",
    "inactive": "無効",
    "waiting": "待機中",
    "running": "実行中",
    "passed": "成功",
    "error": "エラー",
    "planned": "計画済み",
    "instruction_generated": "作業指示作成済み",
    "agent_reported": "作業報告あり",
    "evidence_submitted": "証跡提出済み",
    "verification_passed": "検証成功",
    "verification_failed": "検証失敗",
    "verification_timed_out": "検証タイムアウト",
    "review_requested": "レビュー依頼中",
    "review_reviewer_unavailable": "レビュー担当起動待ち",
    "review_claimed": "レビュー確保済み",
    "review_in_progress": "レビュー中",
    "review_stale": "レビュー停止中",
    "review_approved": "レビュー承認済み",
    "review_commented": "レビューコメントあり",
    "review_changes_requested": "レビュー修正要求あり",
    "needs_human_review": "人間の確認待ち",
    "completed_by_user": "人間が完了",
    "completed_by_ai": "AIが完了",
    "unresolved": "未解決",
    "addressed": "対応済み",
    "accepted-risk": "リスク受け入れ",
    "allowed": "許可",
    "completion_allowed": "完了可能",
    "completion_blocked": "条件未充足",
    "no_active_task": "作業中のタスクなし",
    "understanding_required": "理解確認待ち",
    "understanding_reported": "理解確認報告あり",
    "approved_to_implement": "実装承認済み",
    "deferred": "延期",
    "ready": "着手可能",
    "requires_roadmap": "ロードマップ確認待ち",
    "converted_to_task": "タスク化済み",
    "superseded": "置き換え済み",
}

SEVERITY_LABELS_JA = {
    "critical": "致命的",
    "high": "高",
    "medium": "中",
    "low": "低",
    "info": "情報",
    "warning": "警告",
}

CATEGORY_LABELS_JA = {
    "secret_detected": "秘密情報検出",
    "metadata_mismatch": "メタデータ不一致",
    "evidence_missing": "証跡不足",
    "human_rejected": "人間による差し戻し",
    "human_rework_required": "人間による修正要求",
}

FIELD_LABELS_JA = {
    "id": "ID",
    "status": "状態",
    "state": "状態",
    "next_action": "次の作業",
    "next_actions": "次の作業",
    "next_required_actions": "次の作業",
    "task": "タスク",
    "tasks": "タスク",
    "task_id": "タスク",
    "task_type": "タスク種別",
    "risk_level": "リスク",
    "requires_understanding_check": "理解確認が必要",
    "mode": "モード",
    "recipe": "レシピ",
    "recipe_provenance": "レシピ証跡",
    "description": "説明",
    "acceptance_criteria": "受け入れ条件",
    "base_commit": "基準コミット",
    "project": "プロジェクト",
    "project_id": "プロジェクト",
    "project_name": "プロジェクト名",
    "title": "タイトル",
    "evidence": "証跡",
    "evidence_status": "証跡",
    "verification": "検証",
    "verification_runs": "検証実行",
    "verification_run": "検証実行",
    "latest_verification": "最新の検証",
    "latest_verification_run": "最新の検証実行",
    "review": "レビュー",
    "reviews": "レビュー",
    "review_findings": "レビュー指摘",
    "unresolved_findings": "未解決の指摘",
    "unresolved_review_count": "未解決レビュー指摘数",
    "unresolved_blocking_count": "未解決のブロック指摘数",
    "failure_logs": "失敗ログ",
    "failure_summary": "失敗ログ概要",
    "open_failures": "未解決の失敗",
    "high_open_failures": "重大な未解決失敗",
    "latest_open_failure": "最新の未解決失敗",
    "latest_report": "最新レポート",
    "latest_instruction": "最新の作業指示",
    "latest_review_request": "最新レビュー依頼",
    "latest_review_result": "最新レビュー結果",
    "latest_understanding_check": "最新の理解確認",
    "latest_review": "最新レビュー",
    "working_tree_dirty": "作業ツリー変更あり",
    "verification_source": "検証元",
    "verification_command": "検証コマンド",
    "verification_working_tree": "検証時の作業ツリー",
    "verification_evidence_check": "検証証跡チェック",
    "completion_warnings": "完了時の警告",
    "completed_reason": "完了理由",
    "completed_with_reservations": "留保付き完了",
    "created_at": "作成日時",
    "updated_at": "更新日時",
    "completed_at": "完了日時",
    "reason": "理由",
    "concerns": "懸念",
    "category": "分類",
    "severity": "重大度",
    "message": "内容",
    "source": "発生元",
    "actor": "記録者",
    "resolved_at": "解決日時",
    "resolved_by": "解決者",
    "resolution_note": "解決メモ",
    "total": "合計",
    "open": "未解決",
    "resolved": "解決済み",
    "ignored": "無視",
    "by_severity": "重大度別",
    "by_category": "分類別",
    "by_status": "状態別",
    "recent_high_failures": "直近の重大失敗",
    "completion": "現在タスク完了診断",
    "blocking_reasons": "ブロック理由",
    "none": "なし",
}


def status_label(value: str) -> str:
    return STATUS_LABELS_JA.get(value, value)


def severity_label(value: str) -> str:
    return SEVERITY_LABELS_JA.get(value, value)


def category_label(value: str) -> str:
    return CATEGORY_LABELS_JA.get(value, value)


def field_label(value: str) -> str:
    return FIELD_LABELS_JA.get(value, value)


def bool_label(value: bool) -> str:
    return "はい" if value else "いいえ"


def ai_value_label(value: str) -> str:
    label = status_label(value)
    if label == value:
        return value
    return f"{label} ({value})"
