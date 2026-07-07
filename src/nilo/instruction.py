from __future__ import annotations

from pathlib import Path

from .project_language import human_readable_language_policy, project_primary_language
from .snapshot import current_git_snapshot, evidence_status as computed_evidence_status
from .update_check import cached_instruction_note

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


def build_instruction(
    project: dict,
    task: dict,
    *,
    plan: str = "",
) -> tuple[str, str]:
    degraded = task["degradation_mode"] == "degraded"
    primary_language = project_primary_language(project)
    language_policy = human_readable_language_policy(project)
    criteria = "\n".join(f"- {item}" for item in project["default_completion_criteria"])
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
    elif task.get("mode") == "overdrive":
        mode_note = (
            "\n## Overdrive モード\n"
            "- 「全部オーバードライブ」と言われても、既定では現在の依頼対象だけを進める\n"
            "- 実装、検証、完了報告、completion までは現在 task の範囲で進めてよい\n"
            "- `nilo next` で unrelated な別 task に進む前に止まり、人間に区切りを報告する\n"
            "- 同一 roadmap commitment や全キューへ広げる場合は `--scope commitment` / `--scope project` / `--scope queue` または明示承認を必要とする\n"
            "- 人間への報告では、実装ファイル、テスト、Nilo 帳票 md、docs md を分けて説明する\n"
        )
    update_note = cached_instruction_note()
    if update_note:
        update_note = "\n" + update_note
    light_plan_note = ""
    roadmap_progress_note = ""
    if plan == "light":
        light_plan_note = "\n" + build_light_plan_section(task)
        roadmap_progress_note = "\n" + roadmap_progress_guidance()

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
- primary_language: {primary_language}
{update_note}

## 作業ルール
{chr(10).join(f"- {rule}" for rule in project["rules"]) or "- 作業ブランチを切り替えない"}
- {language_policy}
- 他タスクの変更を混在させない
- 完了報告前にコミットやステージを行ってもよいが、変更ファイル一覧は作業開始時点からの全差分を記載する

## 完了条件
{criteria or "- 変更内容と検証結果を完了報告に記載する"}

{light_plan_note}

{roadmap_progress_note}

## 完了報告一時ファイル
- 完了報告 markdown は `.nilo/reports/{task["id"]}.md` に一時作成する
- 取り込み前に `git status --short` と `git ls-files --others --exclude-standard` で untracked を含む差分を確認する
- 変更ファイル一覧はパスだけの行にする（同じ行に説明文を付けない）
- 取り込み前に `nilo report validate --task {task["id"]} --file .nilo/reports/{task["id"]}.md` で形式と changed_files を検査する
- 取り込みは `nilo report import --task {task["id"]} --file .nilo/reports/{task["id"]}.md` で行う
- import 成功後は DB を正本とし、一時ファイルは削除してよい

{mode_note}
## 完了報告フォーマット
以下の形式を維持し、空欄、TODO、N/A、[ここに記述] を残さないこと。

{REPORT_FORMAT}
"""
    return body, REPORT_FORMAT


def build_light_plan_section(task: dict) -> str:
    description = task.get("description") or task["title"]
    acceptance = task.get("acceptance_criteria") or []
    acceptance_lines = "\n".join(f"- {item}" for item in acceptance) or "- 変更内容と検証結果を完了報告に記載する"
    return f"""## Light plan

目的:
{description}

やること:
- 現状の該当箇所を確認する
- 受け入れ条件を満たす最小変更を実装する
- 関連する focused tests を追加または更新する
- 検証結果を Nilo に記録する

触るファイル:
- 実装時に特定する

検証:
- 変更範囲に対応する focused tests
- 必要に応じて関連 CLI tests

完了条件:
{acceptance_lines}
"""


def roadmap_progress_guidance() -> str:
    return """## ロードマップ/overdrive の人間向け説明ルール
- 最初に実装タスクが残っているかを答える。
- 作業完了と証跡注意を分けて説明する。
- 内部状態名は、明示的に求められない限りそのまま出さない。
- 小〜中規模の作業は Light plan または通常 task で進める。
- 複数タスク・複数コミット・実装と検証の分離が必要な作業は Roadmap を推奨する。
- DB schema、状態遷移、リリース基盤、複数サブシステムにまたがる大改修だけ Epic 扱いを提案する。
- Epic 扱いが必要な場合は理由を示し、明示承認があるまで roadmap revision / acceptance / task plan を進めない。
- overdrive を、キュー全体を空にする許可として扱わない。
- overdrive の既定スコープは現在 task とする。
- 無関係な次 task に移る前に停止する。
- 最終報告では、コード変更・テスト・ドキュメント・Nilo帳票を分けて説明する。
"""


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
