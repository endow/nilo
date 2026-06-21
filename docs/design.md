# Nilo 設計

Nilo は、AI 開発の **意図・現在地・証跡・レビュー・人間の判断** を、一つのプロジェクト状態として残す CLI / MCP ツールである。

AI の会話や完了報告を正本にせず、何を依頼し、何を変更し、何を検証し、何が未解決かを SQLite に記録する。

Nilo はタスク管理サービス、AI 実行基盤、CI、サンドボックスの代替ではない。security boundary ではなく、evidence / audit / workflow discipline tool である。

---

## 1. 設計契約

### DB が正本

プロジェクト状態の正本は `.nilo/nilo.db` である。

`ROADMAP.md`、完了報告、レビュー用 Markdown、CLI 出力は入力または再生成可能な表示であり、DB より優先しない。

### AI の自己申告と実体を分ける

- `AgentReport`: AI が何をしたと報告したか
- `VerificationRun`: 実行したコマンド、結果、実行主体、観測した git snapshot
- `ReviewResult` / `ReviewFinding`: 特定 snapshot に対する評価と指摘
- `TaskCompletion`: 特定 snapshot を誰が完了扱いにしたか、懸念付き完了かどうか

これらは別の事実を表す。AI の完了報告だけで Task を完了にしない。検証成功は品質を保証せず、レビュー承認はコマンド実行の証明にならない。

Nilo separates primary facts from derived checks.

Primary facts are stored:

- what an agent reported
- what command was executed
- what code snapshot it observed
- what a reviewer found
- who accepted completion

Derived checks are computed:

- whether evidence is missing
- whether evidence is stale
- whether review results match the current snapshot
- whether completion has enough support

### 人間が意味を確定する

人間の明示判断が必要な操作は次である。

- `implementation`、`refactor`、`test_addition` の完了確定
- 証跡不足や未解決リスクの受容
- commit、force、削除、外部公開、課金、認証情報を伴う操作

AI は判断材料を集め、提案し、明示された判断を記録する。actor 名は監査用ラベルであり、認可情報ではない。Human-only completion は security boundary ではなく、誰が何を完了扱いにしたかを残す workflow discipline である。

### 不明な状態を成功として補完しない

Git 情報を取得できない、検証が未実行、reviewer が利用不能、MCP の参照状態が古い、仕様や範囲が曖昧な場合は、理由を持つ停止状態にする。

代表例は `needs_human_review`、`blocked`、`stale`、`needs_reassessment` である。

### 特定の AI に依存しない

Codex、Claude Code、ローカル LLM、人間 reviewer を交換可能な実行者として扱う。製品名だけでなく capability、availability、provenance、limitations を保存する。

### 自律実行は中断可能

各段階の状態を DB に残し、停止、再開、別 AI への引き継ぎを会話履歴だけに依存させない。Overdrive も安全境界と最終的な人間判断を迂回しない。

### 受付と実行許可を分ける

`Todo` は依頼や発見事項の受付であり、作成だけでは実行対象にならない。実行する作業は、明示された単発依頼から切り出された `Task` として扱う。方針メモは参照専用であり、Task の実行を許可する権限を持たない。

---

## 2. 状態モデル

```text
Human intent
  ↓
Todo / Task
  ↓
Instruction → AI work → AgentReport
                         ↓
                 VerificationRun + git snapshot
                         ↓
              ReviewResult / Finding + based_on_snapshot
                         ↓
              TaskCompletion + completed_snapshot
```

`Instruction` は AI に渡した目的、制約、完了条件のスナップショットである。

Task の現在状態は関連記録から射影する。CLI と MCP は同じ DB と同じ判定ロジックを使う。

MCP の Task 書き込みは、最後に参照した event ID または context token を照合し、古い状態からの更新を拒否する。

`FailureLog` は、過去の失敗を人間が参照するために保存する。FailureLog から規則を自動生成したり、次回指示へ自動注入したりしない。

### Snapshot reference

検証、レビュー、完了判断は共通の snapshot reference を持つ。

- `git_head`
- `git_diff_hash`
- `working_tree_dirty`

`git_status_porcelain` と `observed_paths` は人間表示の補助情報であり、stale 判定の正本ではない。stale 判定は `git_diff_hash` を含む共通 snapshot reference と現在 snapshot の比較で行う。

`EvidenceStatus` は保存せず、対象 Task の最新 `VerificationRun` と現在 snapshot から表示時に計算する。検証がなければ `missing`、検証が失敗していれば `failed`、snapshot が一致すれば `current`、一致しなければ `stale` と表示する。AgentReport の形式確認は report import 時の表示・FailureLog 記録に留め、独立した一次事実にしない。

並行 reviewer は Task の現在状態を直接承認しない。review result は `based_on_event_id` / `based_on_snapshot` を持ち、reviewer が実際に観測した snapshot に対する結果として残る。レビュー中に Task が進んだ場合、その result は `stale` と表示し、参考情報として残す。現在の TaskCompletion に使えるのは、現在 snapshot と一致する review result のみである。

---

## 3. 証跡と安全境界

`VerificationRun.source` は最低限、次を区別する。

- `nilo_executed`: Nilo がローカルで実行した結果
- `agent_reported`: 外部 AI が提出した結果

Nilo が実行した結果も、テストの十分性、本番での動作、変更の正しさまでは証明しない。

ローカルコマンドは現在の OS ユーザー権限で実行され、サンドボックス、ネットワーク隔離、ファイルシステム隔離を持たない。

actor 名は監査用ラベルであり、認可情報ではない。Nilo は「実行を安全化するツール」ではなく「証跡と判断材料を記録するツール」である。将来的に runner policy、allowed commands、sandbox runner を opt-in 機能として検討する余地はあるが、Phase 1 は実行コマンド、実行結果、誰が提案したかの記録と表示に留める。危険コマンドの判断や実行確認の強制は Phase 1 の既定に含めない。

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
