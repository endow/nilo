# Nilo 設計

Nilo は、AI 開発の **意図・現在地・証跡・レビュー・人間の判断** を、一つのプロジェクト状態として残す CLI / MCP ツールである。

AI の会話や完了報告を正本にせず、何を依頼し、何を変更し、何を検証し、何が未解決かを SQLite に記録する。

Nilo はタスク管理サービス、AI 実行基盤、CI、サンドボックスの代替ではない。AI 開発の作業状態と判断境界を保持する層である。

---

## 1. 設計契約

### DB が正本

プロジェクト状態の正本は `.nilo/nilo.db` である。

`ROADMAP.md`、完了報告、レビュー用 Markdown、CLI 出力は入力または再生成可能な表示であり、DB より優先しない。

### AI の自己申告と実体を分ける

- `AgentReport`: AI が何をしたと報告したか
- `EvidenceCheck`: 報告形式、Git 差分、必要証跡の整合性
- `VerificationRun`: 実行したコマンド、結果、実行主体
- `ReviewResult` / `ReviewFinding`: 成果物の評価と指摘
- `OutcomeReview`: 採用、差し戻し、懸念付き採用の判断
- `TaskCompletion`: Task を終了した明示記録

これらは別の事実を表す。AI の完了報告だけで Task を完了にしない。検証成功は品質を保証せず、レビュー承認はコマンド実行の証明にならない。

### 人間が意味を確定する

人間の明示判断が必要な操作は次である。

- `implementation`、`refactor`、`test_addition` の完了確定
- `RoadmapCommitment` の承認、変更、終了
- 証跡不足や未解決リスクの受容
- commit、force、削除、外部公開、課金、認証情報を伴う操作

AI は判断材料を集め、提案し、明示された判断を記録する。actor 名は監査用ラベルであり、認可情報ではない。

### 不明な状態を成功として補完しない

Git 情報を取得できない、検証が未実行、reviewer が利用不能、MCP の参照状態が古い、仕様や範囲が曖昧な場合は、理由を持つ停止状態にする。

代表例は `needs_human_review`、`blocked`、`stale`、`needs_reassessment` である。

### 特定の AI に依存しない

Codex、Claude Code、ローカル LLM、人間 reviewer を交換可能な実行者として扱う。製品名だけでなく capability、availability、provenance、limitations を保存する。

### 自律実行は中断可能

各段階の状態を DB に残し、停止、再開、別 AI への引き継ぎを会話履歴だけに依存させない。Overdrive も安全境界と最終的な人間判断を迂回しない。

### 受付と実行許可を分ける

`Todo` は依頼や発見事項の受付であり、作成だけでは実行許可にならない。実行する作業は、明示された単発依頼または承認済み `RoadmapCommitment` に接続された `Task` として扱う。

---

## 2. 状態モデル

```text
Human intent
  ↓
Todo / RoadmapCommitment / Task
  ↓
Instruction → AI work → AgentReport
                         ↓
                 EvidenceCheck
                         ↓
                 VerificationRun
                         ↓
              ReviewResult / Finding
                         ↓
              OutcomeReview / TaskCompletion
```

`Instruction` は AI に渡した目的、制約、完了条件のスナップショットである。

Task の現在状態は関連記録から射影する。CLI と MCP は同じ DB と同じ判定ロジックを使う。

MCP の Task 書き込みは、最後に参照した event ID または context token を照合し、古い状態からの更新を拒否する。

`FailureLog`、`DerivedRule`、`FailurePattern` は、過去の失敗を次回の指示と再発防止条件へ反映する。

---

## 3. 証跡と安全境界

`VerificationRun.source` は最低限、次を区別する。

- `nilo_executed`: Nilo がローカルで実行した結果
- `agent_reported`: 外部 AI が提出した結果

Nilo が実行した結果も、テストの十分性、本番での動作、変更の正しさまでは証明しない。

ローカルコマンドは現在の OS ユーザー権限で実行され、サンドボックス、ネットワーク隔離、ファイルシステム隔離を持たない。

レビュー結果は `VerificationRun` の代わりにしない。未解決の blocking finding は完了前の確認事項として残す。

---

## 4. 文書の役割

- `README.md`: 利用者向けの説明と使い方
- `docs/design.md`: 変えてはいけない設計境界
- `ROADMAP.md`: DB から生成する現在の方向と作業状態
- `nilo --help`: 個別コマンドの仕様
- `AGENTS.md` / `CLAUDE.md`: AI エージェント向けの運用手順

実装履歴、完了済みフェーズ、個別コマンド一覧、現在の開発予定を本書へ蓄積しない。

設計と実装が食い違った場合は、実装を本書へ合わせるか、設計判断の変更として本書を同じ変更単位で更新する。
