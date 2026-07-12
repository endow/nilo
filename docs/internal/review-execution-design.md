# レビュー実行基盤の再設計

> 実装状況: 本設計の共通状態モデル、coordinator、direct CLI/MCP 入口、fallback policy、remote lease/reaper、provider adapter registry は実装済み。実装履歴は `3cc44b9` から `5faf854` を参照。

## 目的

Claude、Codex、Grokなどのreviewerへ、利用者が1回の操作でレビューを依頼できるようにする。reviewerのrate limit、quota枯渇、timeout、異常終了、MCP切断が発生しても、レビュー依頼を`claimed`や`in_progress`のまま残さない。

通常経路の最適化が主目的であり、MCPの利用自体は目的にしない。同期実行できるCLI/APIはdirect transportを使い、別セッションや常駐workerが必要な場合だけMCP transportを使う。

## 制約

- 利用者向けの通常操作は1コマンドまたは1 tool callとする。
- Claude固有の状態や例外を共通ライフサイクルへ持ち込まない。
- 既存の`review_requests`と`review_results`を段階的に移行し、既存データを読み取れるようにする。
- review対象は依頼時点の`based_on_event_id`と`based_on_snapshot`へ固定する。
- 失敗後の後始末を、reviewer自身や後続の手動コマンドへ依存させない。
- remote worker向けのregister、heartbeat、claimは内部プロトコルとし、通常操作として要求しない。

## 採用方針

### 1. 論理的な依頼と実行試行を分離する

`review_requests`は「このtaskをレビューしてほしい」という論理的な依頼を表す。新設する`review_attempts`は、特定のreviewerとtransportで実行した1回の試行を表す。

1つのrequestに複数attemptを関連付けられる。たとえばClaudeがrate limitで終了した後、明示されたfallback policyに従ってCodexへ引き継げる。requestを再利用しても、各失敗の証跡はattemptに残る。

`review_attempts`が最低限保持する値は次のとおりとする。

- `review_request_id`
- `reviewer`
- `backend_kind`
- `transport`: `direct_cli`、`direct_api`、`mcp_worker`
- `status`
- `attempt_number`
- `started_at`、`finished_at`
- `lease_id`、`lease_expires_at`、`worker_instance_id`（remote時のみ）
- `error_class`、`error_code`、`retry_after`
- mask済みの診断情報
- `based_on_event_id`とsnapshot hash

### 2. 状態を必ず確定させる

requestの状態は次に限定する。

- active: `requested`、`running`
- non-blocking: `deferred`
- terminal: `completed`、`failed`、`cancelled`、`stale`、`superseded`

attemptの状態は次に限定する。

- active: `starting`、`running`
- terminal: `succeeded`、`rate_limited`、`quota_exhausted`、`timed_out`、`failed`、`cancelled`、`stale`

`claimed`はrequestの利用者向け状態から外し、remote attemptのlease情報として扱う。direct transportにはclaimを作らない。

実行責任を持つNilo側のcoordinatorは、reviewer呼び出しを例外境界で囲み、成功、既知エラー、未知例外のすべてでattemptをterminalへ更新する。同じトランザクションでrequestを次の状態へ更新する。

| attempt結果 | request結果 |
| --- | --- |
| `succeeded` | `completed` |
| `rate_limited` / `quota_exhausted` | fallbackなしなら`deferred`、fallback成功なら`completed` |
| `timed_out` / 一時的通信障害 | retry可能なら`requested`、上限到達なら`failed` |
| 認証・設定・不正出力 | `failed` |
| snapshot不一致 | `stale` |
| cancellation | `cancelled` |

`deferred`、`failed`、`cancelled`、`stale`はtaskを「レビュー実行中」としてブロックしない。reviewが完了していない事実はstatusとnext actionに明示する。

プロセス強制終了などでcoordinatorの後処理が走らない場合だけreaperを使う。reaperは期限切れleaseまたは古いactive attemptをterminalへ移し、requestも同時に解放する。通常のrate limit処理をreaper待ちにしてはならない。

### 3. backendとtransportを分離する

reviewer adapterは次の共通契約を実装する。

```text
readiness(context) -> Ready | Unavailable
execute(context) -> ReviewOutput | BackendError
classify_error(error) -> ErrorClass
cancel(run_id) -> CancelResult
```

`ErrorClass`は少なくとも`rate_limited`、`quota_exhausted`、`authentication`、`configuration`、`timeout`、`transport`、`invalid_output`、`unknown`を持つ。各adapterはClaude、Codex、Grok固有のexit code、HTTP status、構造化エラーをこの分類へ正規化する。

transportは次の2種類を持つ。

- direct: NiloがCLIまたはAPIを起動して完了まで監視する。通常経路とする。
- remote: MCP workerへqueueし、lease付きで実行する。別セッションが必要な場合だけ使う。

MCP tool、CLI handlerのどちらも同じcoordinatorを呼び、独自のライフサイクルを持たない。

### 4. 通常入口を1操作にする

推奨CLIは次の形とする。

```text
nilo review run --task <task_id> --reviewer <reviewer>
```

対応する高水準MCP toolも同じ入力と結果を返す。coordinatorは設定済みadapterからtransportを決定する。`register_reviewer`、`claim_next_review`、`import_review_result`はremote workerと診断用途に残すが、利用者向けの通常手順には表示しない。

同期実行ではコマンド終了時点で必ず`completed`、`deferred`、`failed`、`cancelled`、`stale`のいずれかを返す。remote実行を非同期で返す場合も、単一のrequest IDを返し、利用者にclaimやimportを要求しない。

### 5. retryとfallbackを明示的なpolicyにする

暗黙に別reviewerへレビュー内容を送らない。requestは次のpolicyを保持する。

- `max_attempts`
- `retryable_error_classes`
- `fallback_reviewers`
- `fallback_requires_confirmation`

既定ではrate limitを自動連打しない。`retry_after`を記録して`deferred`にする。fallback reviewerが明示されている場合だけ、次のattemptを作る。fallbackは最大回数を持ち、循環を禁止する。

同じattemptの再送にはidempotency keyを使う。最低限、`review_request_id`、`attempt_number`、`reviewer`、snapshot hashから生成し、二重結果importを防ぐ。

## エラー処理の必須保証

- reviewer processの非ゼロ終了を検出した直後にattemptとrequestを更新する。
- stdoutが`{"error":"rate limited"}`のようなエラーpayloadなら、不正レビュー結果ではなく`rate_limited`へ分類する。
- HTTP 429、provider固有quota code、CLIのusage-limit文言をadapterごとにfixture化する。
- timeout時は子プロセスを終了し、終了確認後に状態を確定する。
- DB更新に失敗した場合は元エラーを隠さず、次回起動時のreconciliation対象をローカルdispatch記録から特定できるようにする。
- secretをstdout、stderr、DB、完了payloadへ保存しない。

## 既存実装からの移行

1. 現在の`dispatch_review`をcoordinatorの最初のcallerへ変更し、既存CLIを互換aliasとして維持する。
2. `review_attempts`を追加し、現在の`review_dispatches`から診断情報を移す。移行期間は両者を読めるview modelを用意する。
3. direct実行では`requested → claimed → in_progress`という中間更新を廃止し、requestを`running`、attemptを`starting → running`へ更新する。
4. Claude専用の`nilo review claude`は`review run --reviewer claude-code`の薄いaliasにする。
5. Codex、Grok adapterを同じregistryへ登録する。未設定backendはrequestを作る前、または即時terminal attemptとして`configuration`を返し、active requestを残さない。
6. remote MCP workflowへleaseとreaperを導入した後、旧claim statusを互換表示から削除する。

## 検証方針

backend共通contract testを作り、Claude、Codex、Grok adapterへ同じケースを適用する。

- 成功時にrequestとattemptがともに完了する。
- rate limit時にactive requestが0件になり、`retry_after`が保存される。
- quota、認証失敗、command not found、timeout、異常終了、不正出力でactive requestが残らない。
- coordinator自身へ未知例外を注入してもreconciliation可能な証跡が残る。
- fallback成功時に1 request、2 attempts、1 resultとなる。
- 二重実行と二重importでresultが重複しない。
- snapshot変更後の結果が`stale`になり、完了扱いされない。
- MCP worker切断後にleaseが回収される。
- direct経路ではregister、heartbeat、claimの呼び出しが発生しない。
- CLIとMCP toolが同じ最終状態、error class、next actionを返す。

## 代替案と不採用理由

### MCP reviewer workflowを通常経路に戻す

別セッションのregister、heartbeat、claim、importが通常処理の遅延と故障点を増やす。remote reviewには必要だが、同期CLI/APIにも強制する理由がないため不採用とする。

### 現在のrequestへattempt情報を追加するだけにする

fallbackやretryで履歴が上書きされ、どのreviewerがなぜ失敗したかを表現できない。requestとattemptを分離する。

### rate limit時にrequestを`failed`にする

再試行可能性と恒久失敗を区別できない。active状態は解除しつつ、再試行可能であることを表す`deferred`を採用する。

## 実装前の未決事項

- GrokをCLI、公式API、OpenAI互換APIのどれで提供するか。adapter契約には影響しないが初期実装範囲を決める必要がある。
- 既定のfallbackを無効にするか、同一ユーザーが設定したreviewer順だけ許可するか。
- `deferred`をstatus/nextでどの優先度で表示し、明示的なretryをどのコマンド名にするか。
- `review_dispatches`を完全移行後に削除するか、監査ログとして残すか。
- remote attemptの既定lease時間と最大実行時間。providerの応答時間とは分離して設定する必要がある。

これらは実装順序や既定値に関する未決事項であり、状態確定、request/attempt分離、backend/transport分離という採用方針は変更しない。
