# DB schema 棚卸し

本書は Nilo の SQLite schema を「一次事実」「派生情報」「運用情報」「人間注記」「旧モデル」「設定/registry」に分離する。即時削除やdata migrationの指示書ではない。機械可読な分類の正本は `src/nilo/schema_catalog.py`、実コードの参照箇所は `docs/schema-usage-report.md` である。

## 分類と共通方針

- **Primary Fact**: 起きたこと、依頼されたこと、誰が判断したかを保持する。通常運用で削除しない。
- **Derived Projection**: 一次事実と現在snapshotから都度計算する。新しいDB正本を作らない。
- **Operational Transport**: lease、retry、worker、外部processなど配送・実行の都合。core factと別のretentionを設定できる。
- **Human Annotation**: 後から参照する評価や失敗記録。自動完了ruleにしない。
- **Legacy Compatibility**: 旧CLI・旧状態モデルのために残る。新規依存を増やさない。
- **Configuration / Registry**: project、model、reviewerなどの設定・登録情報。

推奨の意味は `Keep`（現行概念を維持）、`Encapsulate`（service/repository境界へ直接table accessを集約）、`Freeze`（新規writeを停止）、`Remove Later`（互換期間後の別変更候補）である。この棚卸しではtable/columnを削除しない。

`created by`、`read by`、`updated by` は、各table別に [schema-usage-report.md](schema-usage-report.md) へ列挙した。`Store.get`、`Store.list_where`、`Store.latest_for_task`、`insert`、`update` とraw SQL/table文字列を `scripts/schema_usage_report.py` が静的解析する。動的SQLは手動確認が必要である。

## 全table一覧

| table | concept | class | 提案 | source of truth / derived from | 現行・旧用途 |
|---|---|---|---|---|---|
| `projects` | Project | Configuration / Registry | Keep | project設定 / operator input | identity、language、model既定値 |
| `tasks` | Task | Primary Fact | Encapsulate | 実行許可された作業 / Todoまたは明示依頼 | intakeの正本。`status` は互換保存値 |
| `instructions` | Instruction | Primary Fact | Keep | 発行済み指示snapshot / Taskとrule | agent handoff |
| `agent_reports` | AgentReport | Primary Fact | Keep | AI自己申告 / agent output | 実体検証ではない |
| `evidence_checks` | ReportValidation | Legacy Compatibility | Freeze | 旧report形式確認 / AgentReport | production writerなし。`VerificationRun`のnullable linkだけ残る |
| `verification_runs` | VerificationRun | Primary Fact | Encapsulate | command結果とgit snapshot / runner実行 | EvidenceStatus、完了判断の入力 |
| `failure_logs` | FailureLog | Human Annotation | Encapsulate | 失敗の参照記録 / failure観測 | 自動rule・完了gateではない |
| `model_profiles` | ModelProfile | Configuration / Registry | Keep | model capability設定 | model選択metadata |
| `model_usage_logs` | ModelUsageLog | Operational Transport | Keep | model利用audit / invocation | active write、completion gateではない |
| `outcome_reviews` | LegacyOutcomeDecision | Legacy Compatibility | Encapsulate | 旧outcome判断 / reportとevidence | cancellationで現行write/readあり。正の完了はTaskCompletion |
| `quality_reviews` | QualityAnnotation | Human Annotation | Encapsulate | score注記 / reviewer入力 | quality CLIで現行利用。ReviewResultとは別 |
| `review_requests` | ReviewRequest | Primary Fact | Encapsulate | snapshot-bound依頼 / review transition | review lifecycle |
| `review_attempts` | ReviewAttempt | Operational Transport | Encapsulate | 配送attempt / ReviewRequest | lease、retry、worker、diagnostics |
| `review_reviewers` | ReviewerRegistryEntry | Configuration / Registry | Encapsulate | reviewer登録/heartbeat | capabilityと同時実行数 |
| `review_results` | ReviewResult | Primary Fact | Encapsulate | snapshot-bound verdict / reviewer応答 | review evidence |
| `review_dispatches` | ReviewDispatchLog | Operational Transport | Encapsulate | legacy adapter process log | active legacy adapterのみ。将来Remove Later候補 |
| `review_findings` | ReviewFinding | Primary Fact | Encapsulate | 具体的指摘 / ReviewResult | blocking判断 |
| `review_finding_updates` | ReviewFindingUpdate | Primary Fact | Keep | 指摘状態遷移 / resolution判断 | append-only audit |
| `quality_score_schemas` | QualityScoreSchema | Configuration / Registry | Encapsulate | score設定 / operator入力 | quality CLI設定、completion gateではない |
| `understanding_checks` | UnderstandingDecision | Primary Fact | Keep | 理解確認判断 / Task precondition | instruction readiness |
| `task_completions` | TaskCompletion | Primary Fact | Encapsulate | snapshot完了承認 / evidenceを引用する判断 | invalidate可能、削除しない |
| `recipe_task_provenance` | RecipeTaskProvenance | Primary Fact | Keep | Task作成時recipe snapshot / resolved recipe | 再現可能なprovenance |
| `recipe_runs` | RecipeRun | Operational Transport | Encapsulate | workflow cursor / recipe操作 | active stepと公開操作待ち |
| `roadmap_commitments` | RoadmapCommitment | Primary Fact | Encapsulate | 承認済み計画 / accepted revision | multi-task scope |
| `roadmap_revisions` | RoadmapRevision | Primary Fact | Keep | 提案・判断済み計画text / discussion/import | plan decision audit |
| `todos` | Todo | Primary Fact | Encapsulate | 受付・候補 / requestまたは発見 | 作成だけでは実行許可にならない |
| `overdrive_runs` | OverdriveRun | Operational Transport | Encapsulate | 自律実行cursor / overdrive command | scopeとfailure budget |
| `overdrive_events` | OverdriveEvent | Operational Transport | Keep | 実行log / OverdriveRun activity | resume/operator診断 |
| `transition_events` | TransitionEvent | Primary Fact | Keep | 状態変更audit / domain transition | concurrency contextと監査 |

全29 tableの retention、deletion policy、migration risk、より詳しい current usage は `SCHEMA_CATALOG` の各entryに必須fieldとして保持する。実schema table集合との一致はtestで強制する。DB上に **Derived Projection専用tableは存在しない**。これは意図した状態である。

## 派生状態

次は保存正本ではない。

- `EvidenceStatus`: 最新の `verification_runs` と現在のgit snapshotから `missing` / `failed` / `current` / `stale` を計算する。
- `CompletionProjection`: `task_completions`、verification、review finding、snapshotから計算する。
- `WorkProjection`: Task、Todo、Roadmap、verification、review、completionから現在地と次actionを計算する。
- human status、current task、next action、review state: 上記projectionを人間向けに翻訳する。

`tasks.status`、`review_requests.status`、`recipe_runs.status/current_step` などの保存値は、外部入力または再開cursorを含む互換・運用状態である。横断的な「現在のTask状態」の正本として単独利用してはならない。

## JSON TEXT列

SQLiteではJSON型を使わずTEXTへencodeする。現行列別schemaは `schema_catalog.JSON_FIELD_SCHEMAS` が機械可読な一覧である。`OPTIONAL_LEGACY_JSON_FIELDS` は現行schemaが作成しないが旧DBに存在すればdecodeする列を分離しており、現在は `instructions.applied_failure_pattern_ids` だけである。共通型は次の通り。

- `SnapshotRef object`: `git_head`、`git_diff_hash`、`working_tree_dirty`。補助的に`git_status_porcelain`、`observed_paths`を持ち得る。
- `list[str]`: ID、path、warning、stepなどの文字列配列。
- `object`: feature固有keyを持つJSON object。secretを含むdiagnosticは保存前にredactする。
- `scores`: score名から数値へのmap。完了gateには使わない。

曖昧さが残る `metadata` / `diagnostics` / `context` / `summary_json` はversion discriminatorを現状持たない。新規consumerを追加する場合は、producer別のTypedDict/validatorとversion keyを先に定義することを推奨する。

## 重点的な重複評価

- `evidence_checks` と `verification_runs`: 前者は旧AgentReport形式確認、後者はcommandとsnapshotの一次事実。completionは後者を使う。前者はFreeze候補。
- `outcome_reviews` と `task_completions`: 前者は旧outcome/cancellation modelで、現在もcancel transitionに残る。後者は誰がどのsnapshotを完了扱いしたかの現行正本。cancel readをTransitionEvent等へ移すまでは削除しない。
- `quality_reviews` と `review_results`: 前者はsnapshot非依存のscore注記、後者はReviewRequestとsnapshotに紐づくverdict。quality scoreはcompletion gateではない。
- `model_usage_logs`: activeなmodel利用auditだがcore business factではない。容量観測後にretention期間を決める。
- `review_attempts`: ReviewRequest配送のtransport log。ReviewResultやTaskCompletionを作るdomain判断をadapterへ持たせない。

## schema version、migration、backup

DB自体に単一の`schema_version`/`PRAGMA user_version`はない。現行互換性は `SCHEMA` の `CREATE TABLE IF NOT EXISTS` と `MIGRATION_COLUMN_DEFINITIONS` / `MIGRATION_TABLES` の構造検査で管理する。backup metadataの`schema_version: 1`はbackup metadata形式であり、DB schema versionではない。

migrationは現状additive（不足tableのCREATE、不足columnの`ALTER TABLE ... ADD COLUMN`）で、legacy列削除やdata rewriteはない。`Store`はpending migrationを検出すると`reason=before-migration` backupを作成してから適用する。`nilo upgrade`もmigration前に`before-upgrade` backupを作る。rollback migrationはなく、問題時はsha256/integrity確認済みbackupからrestoreする。したがってschema変更はbackup/restore、existing DB open、legacy DB migration、upgrade testを必須とする。

## 今後の境界

第一段階は `TaskRepository`、`VerificationRepository`、`ReviewRepository`、`CompletionRepository`、`RoadmapRepository`、`FailureLogRepository` ごとにread queryを集約する。巨大な汎用repositoryは作らない。今回の変更では既存accessを移動しない。

望ましい依存方向:

```text
CLI / MCP / View
      ↓
Application Services
      ↓
Projection / Domain Logic
      ↓
Repositories / Store
      ↓
SQLite
```

`Projection -> CLI handler`、`Domain model -> MCP`、`Store -> renderer`、`Adapter -> TaskCompletion`は禁止する。
