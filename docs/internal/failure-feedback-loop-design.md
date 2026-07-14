# 失敗ログの軽量フィードバックループ設計

## 1. 目的

`failure_logs` に蓄積した失敗観測から、繰り返し発生している改善可能な問題を低コストで検出し、人間が採用したものだけを通常の Task として改善する。

本機能は失敗ログを命令や恒久ルールへ自動昇格させない。通常経路では決定論的な集計だけを行い、LLM を使う分析は人間が要求した候補に限定する。改善の成否は Task の完了ではなく、改善後の同一失敗の再発率で評価する。

## 2. 制約と設計原則

- 通常の `status`、`next`、Task 実行で過去ログ本文を大量に読み込まない。
- 観測と集計は自動化するが、要件や実装方針を自動生成しない。
- 改善によるコード・設定・運用変更は必ず独立した change Task で行う。
- 候補の採用、却下、外部要因扱いは監査可能な人間判断として記録する。
- 単発失敗、利用者の明示的な中止、外部サービス障害を安易に再発防止対象にしない。
- 既存の `failure_logs` は観測の正本として維持し、候補は派生情報として再構築可能にする。
- 失敗ログを新しいプロンプト規則へ自動注入しない。
- 集計処理は SQLite の索引付きクエリで完結させ、LLM、embedding、ベクトル検索を必須にしない。

## 3. 対象範囲

### 対象

- 安定した fingerprint の生成
- fingerprint 単位の再発集計
- 改善候補の生成、観察、提案、採用、却下
- 改善候補から通常 Task を作成する handoff
- 改善前後の再発率による効果測定
- CLI と AI context への小さい要約表示

### 対象外

- failure log 本文からの常時 LLM 学習
- failure log に基づくソースコードの自動変更
- 候補の無承認 Task 化
- 組織横断・複数 Nilo DB 横断の学習
- semantic similarity による曖昧な自動統合
- 成功パターンや一般的なコーディング規約の自動生成

## 4. 全体フロー

```text
failure_logs への記録
  -> 決定論的 fingerprint を付与
  -> 安価な集計で閾値を評価
  -> improvement_candidate を observing/proposed に更新
  -> 人間が詳細を確認
  -> 必要な場合だけ通常の change Task を作成
  -> Task の実装・検証・レビュー・完了
  -> 観測窓を継続し、改善前後の再発率を比較
  -> effective / ineffective / inconclusive を判定または人間が確定
```

集計は failure の記録時に候補を軽く更新するか、表示時に期限切れ候補だけ遅延更新する。全履歴の毎回再走査は行わない。

## 5. データモデル

### 5.1 `failure_logs` の追加項目

| field | type | 用途 |
|---|---|---|
| `fingerprint` | TEXT NOT NULL DEFAULT '' | 同種失敗をまとめる安定キー |
| `operation` | TEXT NOT NULL DEFAULT '' | source 内の処理段階。`evidence_check`、`secret_scan` など |
| `error_code` | TEXT NOT NULL DEFAULT '' | 表示文言に依存しないエラーコード |
| `context` | TEXT NOT NULL DEFAULT '{}' | 小さい構造化 JSON。秘密情報や生の全出力は含めない |
| `preventability` | TEXT NOT NULL DEFAULT 'unknown' | `unknown` / `likely` / `external` |

既存行は空 fingerprint のまま保持できる。migration 時に message を解析して埋め戻さず、既知の `category`、`source`、`operation`、`error_code` だけで安全に構成できる行のみ backfill する。

`snapshot` と `context` は用途を分離する。既存の `snapshot` は失敗発生時の Git HEAD、diff hash、dirty 状態など repository snapshot だけを保持する。`context` は失敗固有の小さい機械判定情報だけを保持し、Git 状態、生の標準出力、秘密情報を重複して格納しない。

### 5.2 fingerprint

fingerprint は表示文やファイル名、Task ID、絶対パスを含めず、次の正規化済み要素から生成する。

```text
v1:<source>:<operation>:<category>:<error_code>
```

例:

```text
v1:report_import:evidence_check:metadata_mismatch:changed_files_mismatch
v1:verification_run:secret_scan:secret_detected:credential_pattern
v1:outcome_record:human_outcome:human_rejected:unspecified
```

生成規則は version prefix を持たせる。分類規則を変更しても過去の fingerprint を暗黙に書き換えない。`unspecified` が多すぎる category は自動候補化せず、まず記録元に安定した `error_code` を追加する。

### 5.3 `improvement_candidates`

| field | type | 用途 |
|---|---|---|
| `id` | TEXT PRIMARY KEY | 候補 ID |
| `project_id` | TEXT NOT NULL | Project scope |
| `fingerprint` | TEXT NOT NULL | 対象 fingerprint |
| `status` | TEXT NOT NULL | 候補の状態 |
| `first_seen_at` | TEXT NOT NULL | 集計対象での初回発生 |
| `last_seen_at` | TEXT NOT NULL | 最終発生 |
| `occurrence_count` | INTEGER NOT NULL | 現在の観測窓内の件数 |
| `distinct_task_count` | INTEGER NOT NULL | 影響 Task 数 |
| `high_count` | INTEGER NOT NULL | high 件数 |
| `score` | INTEGER NOT NULL | 決定論的優先度 |
| `representative_failure_ids` | TEXT NOT NULL | 最大3件の JSON 配列 |
| `reason_codes` | TEXT NOT NULL | 閾値到達理由の JSON 配列 |
| `improvement_task_id` | TEXT NOT NULL DEFAULT '' | 採用後の change Task |
| `baseline` | TEXT NOT NULL DEFAULT '{}' | 採用時点の効果測定基準 |
| `evaluation` | TEXT NOT NULL DEFAULT '{}' | 改善後の集計結果 |
| `decision_note` | TEXT NOT NULL DEFAULT '' | 採用・却下理由 |
| `decided_by` | TEXT NOT NULL DEFAULT '' | 原則 `human` |
| `human_confirmed` | INTEGER NOT NULL DEFAULT 0 | 人間確認 |
| `cooldown_until` | TEXT NOT NULL DEFAULT '' | 再提案抑制期限 |
| `created_at` / `updated_at` | TEXT NOT NULL | 監査時刻 |

`UNIQUE(project_id, fingerprint)` を基本とし、再発時は同じ候補を再オープンする。改善を複数回行った履歴が必要なため、Task との関係と評価履歴は次の別表へ記録する。

### 5.4 `improvement_attempts`

| field | type | 用途 |
|---|---|---|
| `id` | TEXT PRIMARY KEY | 改善試行 ID |
| `candidate_id` | TEXT NOT NULL | 候補 |
| `task_id` | TEXT NOT NULL | 実際の change Task |
| `baseline` | TEXT NOT NULL | 改善開始時の件数、Task 数、実行母数、期間 |
| `observation_started_at` | TEXT NOT NULL DEFAULT '' | Task 完了後の測定開始 |
| `observation_ends_at` | TEXT NOT NULL DEFAULT '' | 最短観測期限 |
| `result` | TEXT NOT NULL DEFAULT 'pending' | `pending` / `effective` / `ineffective` / `inconclusive` |
| `evaluation` | TEXT NOT NULL DEFAULT '{}' | 改善後指標 |
| `created_at` / `updated_at` | TEXT NOT NULL | 監査時刻 |

Task は既存の `tasks` を正本とし、この表に独自の実装進捗状態を持たせない。

## 6. 候補の状態遷移

```text
observing
  -> proposed       閾値到達
  -> dismissed      人間が対象外と判断

proposed
  -> accepted       人間が改善を承認し Task を作成
  -> observing      証拠不足として継続観察
  -> dismissed      外部要因、意図した挙動、費用対効果不足

accepted
  -> measuring      関連 Task が完了し観測開始
  -> proposed       Task が取消・拒否され、再検討可能

measuring
  -> effective      十分な母数で再発率が低下
  -> ineffective    十分な母数があり再発が継続または悪化
  -> inconclusive   観測期限後も母数不足

effective
  -> proposed       同じ fingerprint が再発閾値へ到達

ineffective
  -> proposed       自動的に再提案し、人間が次の改善試行を判断
  -> dismissed      人間が追加改善を行わないと判断

inconclusive
  -> measuring      人間が観測期間または最低母数を延長
  -> proposed       新しい再発が閾値へ到達し、改善方法を再検討
  -> dismissed      人間が測定継続の費用対効果がないと判断

dismissed
  -> proposed       cooldown 後に重大度または頻度が新閾値へ到達
```

自動遷移を許すのは `observing -> proposed`、`ineffective -> proposed` と、測定値を用いた評価案の作成までとする。`accepted`、`dismissed`、最終的な効果判定の上書きは人間確認を要求する。初期版では `effective` 等も自動確定せず、推奨結果として表示して人間が確定する。§7.1 の改善後再発閾値は `measuring` または `effective` の候補を `proposed` に戻すために評価し、すでに `ineffective` と確定した候補は件数にかかわらず再提案する。

## 7. 検出規則

### 7.1 初期閾値

観測窓は直近30日とし、`preventability=external`、`status=ignored`、fingerprint 空欄を除外する。次のいずれかで `proposed` とする。

- 同一 fingerprint が3件以上、かつ異なる Task が2件以上
- `severity=high` が2件以上、かつ異なる Task が2件以上
- 改善試行後の観測期間に同一 fingerprint が2件以上再発

人間による `rejected` や `rework` は重要だが理由が多様なため、安定した `error_code` がない間は件数だけで自動提案しない。

### 7.2 score

score は並び替え専用で、提案条件そのものには使わない。

```text
score = occurrence_count
      + 2 * distinct_task_count
      + 3 * high_count
      + 2 * post_improvement_recurrence_count
```

`preventability=likely` は `+2`、`external` は候補化対象外とする。将来係数を調整する場合も、候補に `reason_codes` と計算時の集計値を残し、判断根拠を再現可能にする。

### 7.3 重複と通知抑制

- 同一 project と fingerprint に候補は1件。
- `proposed` は新しい reason code、severity 上昇、または件数倍増がない限り再通知しない。
- `dismissed` の既定 cooldown は30日。
- `status --ai` には候補総数と先頭最大3件だけを表示する。
- Task context には、その Task で発生した該当 fingerprint の proposed 候補を最大3件だけ表示する。

## 8. CLI

### 観測と確認

```bash
nilo improvement list --project nilo [--status proposed]
nilo improvement show <candidate_id> [--json]
nilo improvement refresh --project nilo
nilo improvement evaluate <candidate_id>
```

`refresh` は決定論的集計のみを行い、LLM を呼ばない。通常は failure 記録後の増分更新で足りるため、修復・migration・診断用途として提供する。

### 判断

```bash
nilo improvement observe <candidate_id> --note "..."
nilo improvement dismiss <candidate_id> --reason "..." --human-confirm
nilo improvement accept <candidate_id> --human-confirm
```

`accept` は候補を直接実装しない。候補の短い事実要約を入力として、既存の work entrypoint と同じ境界で change Task を作る。作成する Task の objective には「何を直すか」を断定せず、fingerprint、発生頻度、代表例、期待する再発率低下、検証要求を含める。具体策が未確定なら調査 Task と実装 Task を分離する。

### オンデマンド分析

```bash
nilo improvement analyze <candidate_id>
```

このコマンドだけが LLM 利用を許される。入力は以下に制限する。

- 集計値と fingerprint
- 代表 failure 最大3件の compact message と小さい context
- 関連する改善試行の要約
- 必要に応じて利用者が明示した追加ファイル

出力は原因仮説、反証条件、改善選択肢、推奨検証に限定し、候補や Task を自動更新しない。分析結果の保存は明示的な import または accept 操作で行う。

## 9. AI context と通常表示

`status --ai` の追加情報は固定サイズにする。

```text
improvement_candidates:
- proposed: 2
- measuring: 1
- top:
  - candidate_x changed_files_mismatch 30日で5件/4 Task
details: nilo improvement list --project nilo --status proposed
```

通常プロンプトへ failure 本文、全候補、分析結果を注入しない。`next` は改善候補を通常の active Task より優先しない。active work がなく proposed 候補がある場合だけ、人間向けの選択肢として表示する。

## 10. 効果測定

### 基準値

候補採用時に次を `baseline` として固定する。

- 観測期間
- 同一 fingerprint の発生件数
- 異なる Task 数
- 対象 operation の実行回数を取得できる場合は failure rate
- severity 内訳

母数となる operation 実行回数が取れない初期版では、件数と異なる Task 数を使う。単なる利用量減少を改善と誤認しないため、最低観測母数を満たさない結果は `inconclusive` とする。

### 初期評価規則

関連 Task 完了後、次の早い方ではなく、両方を満たして評価する。

- 14日経過
- 対象 operation が20回、または関連 Task が5件

推奨判定は次のとおり。

- `effective`: 十分な母数があり、同一 fingerprint が0件、または rate が50%以上低下
- `ineffective`: 十分な母数があり、2件以上再発、かつ rate が50%未満しか低下していない
- `inconclusive`: 30日後も最低母数未達、または実行母数を取得できず件数比較にも十分な Task がない

閾値は初期値であり、実データを確認して変更する。変更時は設計判断と migration 不要な project policy として分離できるようにする。

## 11. 監査と安全境界

- candidate の採用・却下・効果確定は transition event を記録する。
- `accept` は既存の active recipe、Roadmap/Epic 判定、project boundary、human acceptance を迂回しない。
- high risk、複数 Task、schema や状態遷移の広範な変更が必要なら、生成後の Task 開始時に既存ルールで Roadmap/Epic 承認を要求する。
- failure の `resolve` は候補の削除を意味しない。過去観測として集計には残すが、同一事象の重複記録が `ignored` なら除外する。
- 秘密情報を fingerprint、context、分析入力へ含めない。既存の masking を通過した compact message のみ使う。
- 候補の生成失敗は Task 実行を失敗させない。failure 記録を正本として維持し、`refresh` で再構築可能にする。

## 12. 採用方針と代替案

### 採用: 決定論的集計 + 人間承認 + オンデマンド分析

通常経路のコストが小さく、改善変更を既存 Task の証拠・レビュー・完了判定へ載せられる。分類精度は fingerprint の品質に依存するが、誤統合を説明・修正しやすい。

### 不採用: 毎回 LLM で失敗分類・改善提案

トークン、待ち時間、非決定性が通常操作へ波及する。失敗がないプロジェクトにも固定費が発生し、同じ入力から候補を再現しにくい。

### 当面不採用: embedding による類似失敗クラスタリング

文言差には強いが、依存サービス、索引、再計算、誤統合の説明責任が増える。安定した error code を記録元へ追加する方を先に行う。

### 不採用: 閾値到達時の自動 Task 作成

外部要因や意図した失敗まで backlog を増やし、改善方法を暗黙に決める危険がある。候補提示と Task 作成の間に人間判断を置く。

### 不採用: failure から恒久プロンプト規則を自動生成

局所的な失敗から要件を増殖させ、コンテキスト肥大と相互矛盾を起こす。再発防止はコード、テスト、明示的な project policy のいずれかとして Task 内で判断する。

## 13. 導入段階

### Phase 1: 記録品質と読み取り専用集計

- `failure_logs` に fingerprint 関連列を追加
- 既知の記録元で安定した `operation` と `error_code` を設定
- fingerprint 集計を `failure summary` の拡張として提供
- 候補を永続化せず shadow report で閾値妥当性を確認

完了条件: 実データで `unspecified` と誤統合の割合を確認できる。

### Phase 2: 候補台帳と通知

- `improvement_candidates` と状態遷移を追加
- 増分更新と `refresh` を実装
- `list`、`show`、`observe`、`dismiss` を追加
- `status --ai` に固定サイズ要約を追加

完了条件: LLM を使わず候補生成・抑制・監査が再現できる。

### Phase 3: Task handoff

- 人間確認付き `accept` を追加
- 既存 work entrypoint を通して change Task を生成
- `improvement_attempts` と Task の関連を記録

完了条件: active recipe や Roadmap 判定を迂回せず改善 Task が作られる。

### Phase 4: 効果測定

- operation 母数を取得できる箇所から計測を追加
- baseline、観測窓、推奨評価を実装
- 再発時の再提案を追加

完了条件: 改善前後を同じ定義で比較し、母数不足を `inconclusive` にできる。

### Phase 5: オンデマンド分析

- 入力上限付き `analyze` を追加
- provider 不在でも候補管理が完全に動くことを維持
- 分析結果を自動適用しない import 境界を追加

完了条件: 通常操作のトークン消費を増やさず、選択した候補だけ分析できる。

## 14. 実装前の未決事項

以下は Phase 1 の shadow report を見てから決める。

1. operation 実行母数を共通 event として新設するか、既存 table から算出するか。
2. 初期閾値の「30日・3件・2 Task」が Nilo 自身の実行頻度に適切か。
3. `human_rejected` を改善候補化するため、拒否理由に安定した reason code を必須化するか。
4. project 単位で閾値を設定可能にするか。初期版では組み込み既定値を優先する。
5. `analyze` の provider 選択と分析結果の保存形式。Phase 1～4 には影響させない。
6. 効果判定を最終的に自動確定してよいか。初期版は人間確定とする。

## 15. 設計検証の範囲

本設計タスクの自動検証は、文書差分の whitespace、必須設計セクションの存在、既存 CLI に `improvement` サブコマンドがなく命名衝突しないことを対象とする。初期閾値の妥当性、状態遷移が実運用を十分に表現するか、operation の実行母数を既存データから正確に算出できるかは、文書だけでは検証できない。

閾値は Phase 1 の shadow report、状態遷移と CLI は Phase 2 の transition test、既存 schema との整合性は各 Phase の migration test で検証する。これらを実装前に検証済みとは扱わず、各 Phase の acceptance と verification evidence に含める。

## 16. 実装時の推奨 Task 分割

本設計全体は DB schema と状態遷移を含むため、一括実装せず Roadmap/Epic 候補として扱う。最低でも次へ分割する。

1. fingerprint schema、生成規則、既知記録元、shadow report
2. improvement candidate schema、集計、状態遷移、CLI
3. AI context の固定サイズ通知と Task handoff
4. improvement attempt と効果測定
5. オンデマンド分析（必要性を再評価してから着手）

Phase 1 の結果によって後続 acceptance と閾値を revision し、人間承認後に task plan を確定する。
