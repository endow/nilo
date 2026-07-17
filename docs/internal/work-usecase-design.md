# `nilo work` ユースケース境界設計

## 目的と制約

`nilo work` を CLI 固有の状態判定と複数コマンドの合成から切り離し、`WorkProjection` を唯一の判断入力とするアプリケーションユースケースへ移す。CLI、MCP、将来の UI は同じ `run_work_usecase` を呼び、状態判定、対象 Task 選択、安全ゲート、更新後の再射影を共有する。

次の境界は維持する。

- `WorkProjection` と `NextActionCode` の優先順位を `work` 側で再実装しない。
- 判定前に DB を更新しない。更新は選択済み operation の executor だけが行う。
- commit、push、PR、release、delete、force、外部公開、secret access、restore、破壊的 migration は自動実行しない。
- AI actor は人間完了判断、Roadmap 承認、UnderstandingCheck 通過、ReviewFinding 解決を確定しない。
- stale context、project boundary、write fence の既存検査を迂回しない。
- handler は引数変換、store lifetime、サービス呼び出し、rendering だけを担当する。

## 採用方針

### モジュールと公開契約

アプリケーション層として `src/nilo/work_service.py` を追加する。既存構成には `src/nilo/application/` package がないため、新しい package 階層は作らない。

公開型は immutable dataclass とする。

- `WorkRequest`: `project_id`、`user_request`、`actor`、`cwd`、任意の `task_id`、`allow_task_creation`、出力 `format` を保持する。
- `WorkResult`: `before`、`after`、`action_taken`、`task_id`、`instruction`、`acceptance_criteria`、`warnings`、`diagnostics` を保持する。
- `WorkOperation`: 判定結果を表す内部値。projection の code、対象 ID、更新可否、停止理由を renderer 非依存の値で保持する。
- `WorkActionTaken`: 実行した操作を安定した enum 値で表す。表示文はこの enum と projection から CLI/MCP adapter が生成する。

`run_work_usecase(store, request)` は次の順で処理する。

1. `current_git_snapshot_full(request.cwd)` を一度だけ取得する。
2. `project_work_projection` または明示 `task_id` に対する `task_work_projection` を取得する。
3. `decide_work_operation(before, request)` で operation を決める。
4. mutation が許可された operation だけを既存ユースケースへ委譲する。
5. mutation があった場合だけ同じ snapshot、または mutation が evidence を変え得る場合に更新 snapshot を使って再射影する。read-only operation では `after is before` とする。
6. Instruction と acceptance criteria を既存 Task データから組み立て、`WorkResult` を返す。

snapshot 一回という要件と「更新後の状態を再取得」を両立するため、Task の作成・開始のように Git 状態を変更しない DB mutation は最初の snapshot を再利用する。verification の自動実行は初期実装では行わないため、work service 内の追加 snapshot は不要になる。

### operation の対応

`decide_work_operation` は `NextActionCode` の網羅的な match とし、状態優先順位は持たない。

- `NONE`: 実行依頼かつ安全条件を満たす場合だけ Task 作成候補。それ以外は no-op。
- `TRIAGE_TODO`: Todo を自動開始しない。明示依頼との一致判定は既存 Todo promotion フローに委譲できるまで診断のみ。
- `REVIEW_ROADMAP` / `APPROVE_ROADMAP`: no-op とし承認待ちを返す。
- `CREATE_TASK`: 承認済み Roadmap item の既存 provenance-aware 作成処理だけを呼ぶ。
- `START_TASK`: 既存 instruction/start 処理を冪等に呼ぶ。開始済みなら更新せず Instruction を返す。
- `CONFIRM_UNDERSTANDING`: no-op。目的、制約、完了条件を返す。
- `CONTINUE_WORK`: no-op。既存 Instruction と acceptance criteria を返す。
- `IMPORT_AGENT_REPORT`: report 本文を `WorkRequest` の将来拡張で受けるまでは必要形式の診断だけを返す。
- `RUN_VERIFICATION` / `RERUN_VERIFICATION`: verification service を自動起動せず、必要コマンドを返す。
- `REQUEST_REVIEW`: reviewer transport を選ばず、既存 request 作成ユースケースへ委譲する。必要な reviewer が request にない場合は診断のみ。
- `WAIT_FOR_REVIEW`: no-op。現在の request と待機理由を返す。
- `RESOLVE_REVIEW_FINDINGS`: no-op。未解決 finding と Instruction を返す。
- `ACCEPT_COMPLETION`: no-op。特に AI actor では完了イベントを作らない。
- `REASSESS_STATE` / `RESOLVE_BLOCKER`: no-op。projection の blocker、reasons、diagnostics を返す。

### Task 自動作成ゲート

自動作成は `NextActionCode.NONE` かつ実行依頼が明示され、`allow_task_creation=True` の場合だけ候補にする。さらに projection と store から次を確認する。

- active Task がない。
- 承認待ち Roadmap がない。
- unresolved ReviewFinding がない。
- verification、review、completion、人間判断の待機を示す projection ではない。
- Todo 登録だけの依頼ではない。

実行依頼か inspect 依頼かの判定は CLI の文言推測をサービスへ移さない。CLI の `--intent` を `WorkRequest` の明示値へ正規化し、MCP は schema 上の明示 intent を渡す。既存の後方互換として intent 未指定時の `read_only_work_route` は adapter で実行し、Task 作成前に `allow_task_creation=False` とする。

### CLI 統合

`cmd_facade_work` は段階的に薄くする。

1. boundary と project の存在確認を共通 bootstrap に残す。
2. parser 引数を `WorkRequest` に変換する。
3. `run_work_usecase` を一度呼ぶ。
4. human/json renderer へ `WorkResult` を渡す。
5. `--check` は work service の責務に含めず、明示 verification service 呼び出しへ分離する。互換期間は CLI adapter が service 結果を確認して既存 `cmd_facade_check` を呼ぶ。

active Task 選択、verification/review/completion/roadmap/todo の優先順位、projection blocker 判定、Task 作成可否は handler から削除する。

### MCP 統合

MCP に `work` tool を追加し、repository identity guard と stale context guard の通過後に同じ `WorkRequest` と `run_work_usecase` を使う。レスポンスは `WorkResult.to_dict()` を基礎に、`action_taken`、`task_id`、`before.phase`、`before.next_action`、`after.phase`、`after.next_action`、`instruction`、`acceptance_criteria` を必須にする。

既存の `get_agent_work_context` と `get_next_step` は read-only projection API のまま維持する。Task 更新を伴う MCP `work` だけ、既存 `context_token` / `last_seen_event_id` 検証を mutation の直前に適用する。

### 冪等性

冪等性は新しい DB schema を追加せず、既存レコードを operation executor が照合して保証する。

- started Task: status/event を確認し、既存 Instruction を返す。
- Instruction: Task に保存済みの同一内容を再利用する。
- Roadmap item: provenance の commitment/item ID で既存 Task を検索する。
- review request: active request を検索して再利用する。
- verification/review/completion 待ち: read-only operation のためイベントを作らない。

各 mutation は「既存確認 → 必要時のみ作成 → 再射影」を同一 store transaction 境界で行える既存 API を優先する。既存 API が transaction を内包しない場合は executor 側に限定して transaction を設ける。

## 代替案と不採用理由

### `cmd_facade_work` の既存ロジックを整理するだけ

CLI と MCP が同じ判断を共有できず、handler が projection 以外の優先順位を保持し続けるため不採用。

### `WorkProjection` に更新処理を追加する

射影の read model と command execution が混ざり、`status` / `next` の読み取りが副作用を持つ危険があるため不採用。

### NextAction ごとに別サービスを公開する

個々の既存ユースケースへの委譲先としては有効だが、入口ごとの operation 選択が再び重複する。`run_work_usecase` を共通入口とし、内部 executor から個別サービスを呼ぶ。

### 初期実装から verification subprocess を自動実行する

snapshot 回数、timeout、出力記録、write fence の責務が膨らみ、指示書もデフォルトは案内としているため不採用。`--check` の互換 adapter は別責務として残す。

## 実装順序

1. `work_service.py` に型、operation decision、read-only code の結果生成を実装し単体テストを追加する。
2. Task 作成と START_TASK の executor を既存 API へ接続し、冪等性テストを追加する。
3. CLI renderer と adapter を追加し、`cmd_facade_work` の独自判定を除去する。
4. MCP `work` schema/handler を追加し stale context と identity guard を接続する。
5. CLI/MCP 一致、status/next/work 一致、安全境界、full suite、ruff を検証する。

## 実装前に分離した未決事項

- 現在の MCP surface には作業開始用 `work` tool がない。新規 tool 名を `work` とするか、API 命名規約に合わせ `start_or_continue_work` とするかは互換性判断が必要。本設計では指示書に合わせ `work` を既定とする。
- `START_TASK` に対応する既存の「Instruction 生成」が CLI handler に密結合している可能性がある。実装時に副作用のない instruction builder と開始イベント writer へ分離する。
- Todo と明示依頼の「一致」は文字列類似度で推測しない。既存 provenance または明示 `todo_id` がない限り初期実装では診断だけにする。
- `REQUEST_REVIEW` は reviewer 選択を service に持ち込めないため、reviewer 未指定時は自動 request を作らず診断にする。
- `format` は domain decision に影響させず adapter の renderer 選択だけに使う。将来削除可能だが指示書の入力契約との整合のため初期型には残す。

これらは初期実装を止める未決事項ではない。安全側の既定を上記のとおり採用し、外部公開や破壊的 migration は行わない。
