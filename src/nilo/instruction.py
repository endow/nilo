from __future__ import annotations

from pathlib import Path

from .snapshot import current_git_snapshot, evidence_status as computed_evidence_status

from .failure import render_recurrence_prevention


REPORT_FORMAT = """# 完了報告

## 1. 実施内容

## 2. 変更ファイル一覧

## 3. 実行した検証
### テストコマンド
### テスト結果
### 型チェック
### lint

## 4. 未実行の検証（理由を記載）

## 5. 既知の問題 / 仕様から外れた判断

## 6. 人間に確認してほしい点
"""

UNDERSTANDING_FORMAT = """# 実装前確認

## 1. タスク目的の理解
このタスクで達成すべきことを一文で説明する。

## 2. 変更対象候補
変更が必要になりそうなファイル・モジュールを列挙する。

## 3. 変更しない領域
今回触らない領域を明示する。

## 4. 想定リスク
仕様誤読、既存影響、テスト不足などのリスクを書く。

## 5. 実装前に人間確認が必要な点
判断が必要な点があれば明示する。
"""


def build_understanding_prompt(task: dict) -> str:
    return f"""# 実装前確認依頼

## タスク
{task["title"]}

## タスク種別
- 種別: {task.get("task_type", "implementation")}
- リスク: {task.get("risk_level", "medium")}

以下の形式で実装前確認を提出すること。まだコード変更しないこと。

{UNDERSTANDING_FORMAT}
"""


def build_review_prompt(task: dict) -> str:
    return f"""# レビュー指示

## タスク
{task["title"]}

## レビュー方針
- コード変更は禁止
- 仕様逸脱、過剰実装、未検証箇所、保守性リスクを優先して指摘する
- 証跡が揃っているかではなく、成果物の品質と妥当性を見る

## レビュー観点
- requirement_fit: 仕様意図に合っているか
- design_fit: 既存設計と合っているか
- maintainability: 保守しやすいか
- scope_control: 変更範囲が適切か
- simplicity: 過剰実装していないか
- risk: 将来の不具合リスクがないか
- user_value: 目的に対して価値があるか

## 出力形式
# QualityReview

## Summary

## Issues

## Scores
任意。1-5で評価できる場合のみ記載する。
"""


def build_autoscore_prompt(
    task: dict,
    report: dict | None,
    evidence_check: dict | None,
    verification_run: dict | None,
    required_scores: list[str],
) -> str:
    description = task.get("description") or "未設定"
    acceptance = "\n".join(f"- {item}" for item in task.get("acceptance_criteria", [])) or "未設定"
    scores = "\n".join(f"- {score}" for score in required_scores) or "- 任意の品質観点を必要最小限"
    report_body = report["body_md"] if report else "未提出"
    evidence_status = computed_evidence_status(verification_run, current_git_snapshot(Path.cwd()))
    if verification_run:
        verification = (
            f"command: {verification_run['command']}\n"
            f"exit_code: {verification_run['exit_code']}\n"
            f"timed_out: {bool(verification_run['timed_out'])}\n"
            f"stdout:\n{verification_run['stdout']}\n"
            f"stderr:\n{verification_run['stderr']}"
        )
    else:
        verification = "未実行"
    return f"""# Quality Autoscore 指示

## タスク
{task["title"]}

## タスク説明
{description}

## 受け入れ条件
{acceptance}

## 必須スコア
{scores}

## 完了報告
{report_body}

## EvidenceStatus
status: {evidence_status}

## VerificationRun
{verification}

## 採点方針
- コード変更は禁止
- 各 score は 1-5 の整数で採点する
- 必須スコアがある場合はすべて出力する
- 判断理由は Rationale に簡潔に書く
- 不明な点や未検証リスクは Issues に書く

## 出力形式
# QualityReview

## Summary

## Issues

## Scores

## Rationale
"""


def build_rules_derive_prompt(project: dict, failures: list[dict]) -> str:
    if failures:
        failure_lines = "\n".join(
            (
                f"- id: {failure['id']}\n"
                f"  task_id: {failure['task_id']}\n"
                f"  category: {failure['category']}\n"
                f"  severity: {failure['severity']}\n"
                f"  message: {failure['message']}"
            )
            for failure in failures
        )
    else:
        failure_lines = "- 失敗履歴なし"
    return f"""# DerivedRule 生成指示

## プロジェクト
{project["name"]} ({project["id"]})

## FailureLog
{failure_lines}

## 方針
- コード変更は禁止
- FailureLog を次回以降の作業指示に入れる短い行動規則へ集約する
- 事実にない内容を補わない
- 過剰に一般化せず、同じ失敗を防ぐための具体的な規則にする
- tag は #testing, #git, #evidence, #lint, #typecheck, #architecture, #review, #general から必要最小限を選ぶ
- severity は low / medium / high のいずれかにする
- confidence は 0.1 から 1.0 の数値にする

## 出力形式
# DerivedRules

## Rule
source_failures: failure_id_1, failure_id_2
rule: 次回の作業で守る短い行動規則
tags: #evidence, #testing
severity: medium
confidence: 0.6

必要な数だけ ## Rule ブロックを繰り返す。
"""


def build_instruction(
    project: dict,
    task: dict,
    selected_rules: list[tuple[dict, dict]],
    success_patterns: list[dict] | None = None,
    failure_patterns: list[dict] | None = None,
) -> tuple[str, str]:
    degraded = task["degradation_mode"] == "degraded"
    criteria = "\n".join(f"- {item}" for item in project["default_completion_criteria"])
    rules = "\n".join(f"{index}. {rule['rule_text']}" for index, (rule, _) in enumerate(selected_rules, start=1))
    if not rules:
        rules = "なし"
    patterns = "\n".join(
        f"{index}. {pattern['pattern_text']}"
        for index, pattern in enumerate(success_patterns or [], start=1)
    )
    if not patterns:
        patterns = "なし"
    recurrence = render_recurrence_prevention(failure_patterns or [])
    if recurrence:
        recurrence = "\n" + recurrence + "\n"
    description = task.get("description") or "未設定"
    acceptance = "\n".join(f"- {item}" for item in task.get("acceptance_criteria", []))
    if not acceptance:
        acceptance = "未設定"

    task_type_note = task_type_guidance(task)
    mode_note = ""
    if degraded:
        mode_note = (
            "\n## Degraded モード\n"
            "- 作業範囲を小さく保つ\n"
            "- 自動完了を宣言しない\n"
            "- 判断が必要な点は human_review_required として報告する\n"
        )

    body = f"""# 作業指示

## タスク
{task["title"]}

## タスク説明
{description}

## 受け入れ条件
{acceptance}

## タスク種別
- 種別: {task.get("task_type", "implementation")}
- リスク: {task.get("risk_level", "medium")}
- 実装前確認: {"必要" if task.get("requires_understanding_check") else "不要"}
{task_type_note}

## プロジェクト
- 名前: {project["name"]}
- 技術スタック: {", ".join(project["tech_stack"]) or "未設定"}

## 作業ルール
{chr(10).join(f"- {rule}" for rule in project["rules"]) or "- 作業ブランチを切り替えない"}
- 他タスクの変更を混在させない
- 完了報告前にコミットやステージを行ってもよいが、変更ファイル一覧は作業開始時点からの全差分を記載する

## 完了条件
{criteria or "- 変更内容と検証結果を完了報告に記載する"}

## 完了報告一時ファイル
- 完了報告 markdown は `.nilo/reports/{task["id"]}.md` に一時作成する
- 取り込みは `nilo report import --task {task["id"]} --file .nilo/reports/{task["id"]}.md` で行う
- import 成功後は DB を正本とし、一時ファイルは削除してよい

## 今回必須の行動規則
{rules}

## 参考にする成功パターン
{patterns}
{recurrence}
{mode_note}
## 完了報告フォーマット
以下の形式を維持し、空欄、TODO、N/A、[ここに記述] を残さないこと。

{REPORT_FORMAT}
"""
    return body, REPORT_FORMAT


def task_type_guidance(task: dict) -> str:
    task_type = task.get("task_type", "implementation")
    if task_type in ("research", "design", "review", "verification"):
        return "- コード変更: 禁止"
    if task_type == "documentation":
        return "- コード変更: ドキュメント更新に必要な範囲だけ許可"
    if task_type == "refactor":
        return "- コード変更: 振る舞いを変えない整理のみ許可"
    if task_type == "test_addition":
        return "- コード変更: テスト追加と必要最小限の補助変更のみ許可"
    return "- コード変更: 指定範囲のみ許可"
