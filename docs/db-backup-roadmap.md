# DB Backup Roadmap

## 背景

Nilo は AI 作業の現在地、検証、レビュー、人間の完了判断を `.nilo/nilo.db` に保存する evidence / audit / workflow discipline tool である。Nilo は security boundary ではないが、証跡をプロジェクト側に残すことを中核価値にしているため、DB の消失は単なる利便性の問題ではなく、作業判断の根拠喪失になる。

現在の README には `nilo upgrade` が migration 前に `.nilo/backups/` へバックアップを作る旨が書かれている。一方、実装上の `backup_database()` は `shutil.copy2()` による DB ファイル単体コピーであり、WAL モードや書き込み中 DB の整合性、ハッシュ、メタ情報、復元安全策までは扱っていない。

## 目的

- 稼働中の SQLite DB から整合したバックアップ DB を生成する。
- バックアップ DB に対して `PRAGMA integrity_check` と sha256 を記録する。
- `.meta.json` を一次記録として残し、DB が失われてもバックアップの由来を確認できるようにする。
- 復元は上書き前に必ず現在 DB を退避し、復元前後の検証を行う。
- `.nilo/nilo.db` 本体を直接クラウド同期せず、検証済みバックアップだけを `--export <dir>` で外部退避できるようにする。
- `nilo upgrade` / 将来の migration 前自動バックアップへ自然に接続できる設計にする。
- 後続で `age` 暗号化、世代管理、`postCommand` による `rclone` 等の外部連携を追加できる CLI / 設定構造を確保する。

## 非目的

- Google Drive / Dropbox / OneDrive / S3 などのクラウド API 認証を Nilo 本体に持ち込まない。
- `.nilo/nilo.db`、`.db-wal`、`.db-shm` をクラウド同期ディレクトリに直接置く運用を推奨しない。
- 暗号化を Phase 1 の必須条件にしない。
- DB 内の `BackupRecord` を一次記録にしない。DB が失われたときに同時に失われるため、一次記録はバックアップ DB と隣接する `.meta.json` とする。
- restore を人間確認なしで destructive に実行する設計にしない。

## 既存実装調査

- DB パス: `Store.default_db_path()` は `NILO_DB` があればそれを使い、なければ `Path.cwd() / ".nilo" / "nilo.db"` を使う。CLI 共通オプション `--db` でも上書きできる。
- `.nilo` 作成: `Store.__init__()` が `self.path.parent.mkdir(parents=True, exist_ok=True)` を実行する。`nilo init` / `agent install` は `.nilo/agent-instructions.md`、`.nilo/recipes/`、`.nilo/reviewers.toml` なども使う。
- SQLite 利用: Prisma は使っていない。Python 標準 `sqlite3` を直接使用し、`Store` が単一接続を持つ。
- WAL: `SCHEMA` 冒頭で `PRAGMA journal_mode=WAL;` を実行している。したがって単純な `.db` ファイルコピーでは最新コミット済み内容を取りこぼす可能性がある。
- migration: 専用 migration ファイル群はない。`Store.__init__()` が `SCHEMA` を流し、`_migrate()` が不足カラムを `ALTER TABLE` で補う。
- `nilo migrate`: 現状は主に旧 agent instruction block の移行コマンドであり、DB schema migration runner ではない。ただし `Store` を開くため、結果として `_migrate()` は走る。
- `nilo upgrade`: `src/nilo/upgrade.py` に存在する。更新、再インストール後、migration 前に `backup_database()` を呼ぶ。現状の `backup_database()` は `.nilo/backups/nilo-YYYYMMDD-HHMMSS.db` へ `shutil.copy2()` するだけ。
- CLI 構造: `argparse`。`src/nilo/cli_parsers/root.py` が top-level command を登録し、handler は `src/nilo/cli_handlers/*` に分かれる。新コマンドは `cli_parsers` と `cli_handlers` に追加するのが既存パターン。
- 設定ファイル: 汎用 Nilo 設定はまだない。既存の TOML 設定は主に `.nilo/reviewers.toml`。標準ライブラリでは読み取り用 `tomllib` が使えるが、書き込み用 TOML ライブラリは依存にない。
- git 情報: `src/nilo/gitmeta.py` に `head_commit()`、`working_tree_state()`、`git_output()` がある。`VerificationRun` は `git_head`、`git_diff_hash`、`working_tree_dirty` を保存する設計。
- version: `src/nilo/__init__.py` の `__version__`、CLI の `nilo_version()` が使える。
- テスト: `unittest` ベース。`tests/test_upgrade.py` は既存バックアップと migration 前停止を検証している。`tests/test_cli.py`、`tests/test_gitmeta.py`、`tests/test_mcp_server.py` などは一時ディレクトリと `--db` を多用する。
- snapshot: `src/nilo/snapshot.py` は `.nilo/backups/**` を snapshot 対象外にしており、バックアップ生成物を作業差分の証跡判定から外す既存方針がある。

## 設計方針

- バックアップ生成は Python 標準 `sqlite3.Connection.backup()` を第一候補にする。標準ライブラリで使えるため追加依存なしに、WAL モードでも整合したコピーを作れる。
- `better-sqlite3` は JavaScript 依存なので不要。Nilo は Python CLI であり、Python 標準 `sqlite3` の backup API と `PRAGMA integrity_check` で足りる。
- Prisma は現在使っていない。将来 Prisma 等を使う場合でも、DB ファイルを開く SQLite 接続から backup API 相当を使い、アプリ側 connection を閉じるか短時間 read lock を許容して整合バックアップを作る方針は変えない。
- 単純 file copy は稼働中 DB では避ける。特に WAL モードでは `.db` 本体だけでは不十分で、`.db-wal` / `.db-shm` のタイミング差もある。
- バックアップ成果物は基本的に単一 `.db` と隣接 `.meta.json` に正規化する。`.db-wal` / `.db-shm` は backup API の入力側の実装詳細として扱い、成果物としてコピーしない。
- backup API によるバックアップは、別 connection から source DB のコミット済み transaction を取得する設計にする。未コミットの in-flight write は含めず、`.db-wal` / `.db-shm` を成果物としてコピーして補完する設計にはしない。
- restore は destructive 操作なので、対象バックアップ検証、sha256 検証、現在 DB の `before-restore` バックアップ、復元後検証を必須にする。
- restore handler は通常の `Store` を開く CLI handler とは分け、DB 置換時点で Nilo 側の SQLite connection が残らない構造にする。Windows では open handle が `Path.replace()` を妨げるためである。
- 外部退避は `--export <dir>` のファイルコピーから始める。Nilo 本体はクラウド API 認証を持たず、将来の `postCommand` で `rclone` 等を呼び出せる余地を残す。
- メタ情報はバックアップ DB と同じ basename の `.meta.json` に保存する。DB 内 `BackupRecord` は Phase 6 以降の補助索引としてのみ検討する。

## CLI 案

既存 CLI は top-level command が多いため、利用頻度とわかりやすさを優先し、次を候補にする。

```bash
nilo backup
nilo backup --reason before-upgrade
nilo backup --export ~/NiloBackups
nilo backup --encrypt --export ~/OneDrive/NiloBackups
nilo backups
nilo restore .nilo/backups/nilo-20260624-180000.db
nilo restore --decrypt ~/OneDrive/NiloBackups/nilo-20260624-180000.db.age
```

実装上は `src/nilo/backup.py` に中核ロジック、`src/nilo/cli_parsers/backup.py` と `src/nilo/cli_handlers/backup.py` に CLI を置く案が自然である。`backup_database()` は互換 shim として残し、内部的に新しい backup service を呼ぶ。

推奨オプション:

- `nilo backup --reason <manual|before-upgrade|before-migration|before-restore|daily|other>`
- `nilo backup --export <dir>`
- `nilo backup --encrypt --recipient <age-recipient>` または設定ファイル指定
- `nilo backups --json`
- `nilo restore <backup-db-or-age-file>`
- `nilo restore --decrypt <file.age>`
- `nilo restore --yes` は将来検討。ただし既定は確認表示のみで、人間操作 CLI に限定する。

## データ構造

`.meta.json` の初期スキーマ:

```json
{
  "schema_version": 1,
  "created_at": "ISO8601 timestamp",
  "source": ".nilo/nilo.db",
  "reason": "manual",
  "git_head": "current git commit hash if available",
  "working_tree_dirty": true,
  "nilo_version": "0.1.4",
  "db_size_bytes": 0,
  "sha256": "backup db sha256",
  "integrity_check": "ok",
  "backup_path": ".nilo/backups/nilo-20260624-180000.db",
  "encrypted": false,
  "encryption": null,
  "exported_to": null
}
```

将来の暗号化時は `encrypted: true`、`encryption: {"tool": "age", "recipient_fingerprint": "...", "plaintext_sha256": "...", "ciphertext_sha256": "..."}` を追加する。秘密鍵パスや passphrase は meta に保存しない。

ファイル名規則:

- ローカルバックアップ: `.nilo/backups/nilo-YYYYMMDD-HHMMSS.db`
- メタ: `.nilo/backups/nilo-YYYYMMDD-HHMMSS.db.meta.json`
- 暗号化: `nilo-YYYYMMDD-HHMMSS.db.age` と `nilo-YYYYMMDD-HHMMSS.db.age.meta.json`
- restore 前退避: `.nilo/backups/nilo-YYYYMMDD-HHMMSS-before-restore.db`
- 衝突時: 同一秒内衝突を避けるため `-01`, `-02` を suffix として付ける。既存ファイルは上書きしない。

## バックアップ生成フロー

1. DB パスを解決する。優先順位は CLI `--db`、`NILO_DB`、`.nilo/nilo.db`。
2. source DB の存在を確認する。存在しない場合は明確に `skipped` またはエラーを返す。`upgrade` の互換動作では `None` を許容する。
3. `.nilo/backups/` を作成する。
4. `sqlite3.connect(source)` と `sqlite3.connect(destination)` を開き、`source_conn.backup(dest_conn)` で整合コピーを作る。この保証範囲は source DB のコミット済み内容であり、未コミットの in-flight write は含めない。通常は backup API に WAL 内容の取り込みを任せ、`wal_checkpoint` は成果物を整えるための必須手順にしない。
5. destination に対して `PRAGMA integrity_check` を実行し、結果が `ok` でない場合は失敗にする。
6. destination DB の sha256 とサイズを計算する。
7. git 情報を取得する。`head_commit(Path.cwd())` と `working_tree_state(Path.cwd())` を再利用する。
8. Nilo version、reason、source、created_at を含む `.meta.json` を書く。
9. `--export <dir>` が指定されていれば、DB と meta を export 先へコピーし、コピー後に sha256 を再計算して meta と一致することを確認する。
10. `--encrypt` が指定されていれば、暗号化成果物を作り、平文をローカルに残すかどうかの方針を CLI / 設定で明示する。初期は export 用暗号化のみを推奨する。

## 復元フロー

1. 復元対象ファイルの存在を確認する。
2. `.age` の場合は `--decrypt` が指定されていること、`age` コマンドが存在すること、復号先一時ファイルを安全に作れることを確認する。
3. 復元対象 DB に `PRAGMA integrity_check` を実行する。
4. 隣接 `.meta.json` があれば sha256 を検証する。meta が無い場合は既定で警告して停止し、明示オプションがある場合だけ続行を検討する。
5. 現在 DB が存在する場合、同じ backup service で `reason=before-restore` の退避バックアップを作る。ここでも integrity_check と sha256 を必須にし、退避バックアップ用に開いた SQLite connection は DB 置換前に必ず閉じる。
6. restore handler は通常の task/status/report 系 handler のように `Store` を開いたまま進めない。現在の SQLite 接続がない CLI プロセスで DB ファイルを置換する。Windows 対応のため、同一ディレクトリ内一時ファイルへの書き込み後に `Path.replace()` を使う。
7. 復元後 DB に対して `PRAGMA integrity_check` を実行する。
8. 成功時に、復元対象、退避バックアップ、復元後検証結果を表示する。

restore は DB を上書きするため、AI agent が勝手に実行する通常 workflow には入れない。人間の明示依頼がある場合のみ扱う。

## Export / Offsite Backup 方針

- `.nilo/nilo.db` 本体を OneDrive / Google Drive / Dropbox 等へ直接置かない。
- export 先には整合確認済み `.db` と `.meta.json`、または暗号化済み `.db.age` と `.meta.json` だけを置く。
- `--export <dir>` は任意ディレクトリを受け取り、Windows、WSL、macOS、Linux の `Path.expanduser()` と `Path.resolve()` で扱う。
- export 先の同名衝突は上書きせず、suffix を付けるかエラーにする。推奨はローカル backup 名と同じ衝突回避関数を使うこと。
- export 後はコピー先 DB または ciphertext の sha256 を再検証する。
- ドキュメントでは「クラウド同期対象に置くのは export 成果物であって、稼働中 DB ではない」と明記する。

## 暗号化方針

Phase 1-4 では暗号化なしの backup/export/restore を完成させる。Phase 5 で `age` を第一候補として追加する。

推奨設計:

- `nilo backup --encrypt --export <dir>` は `age` コマンドを探し、無ければ「暗号化コマンドが見つからない」として失敗する。暗号化なしへ silently fallback しない。
- recipient は `--recipient`、環境変数、または将来の `.nilo/config.toml` で指定する。
- 設定例は将来 `backup.age_recipient`、`backup.export_dir`、`backup.post_command` のような名前空間にする。
- `nilo restore --decrypt <file.age>` は `age` が無い場合に停止する。復号した一時 DB は restore 完了後に削除する。
- passphrase 入力や秘密鍵管理は `age` 側に任せ、Nilo は秘密情報を DB や meta に保存しない。

## 自動バックアップ接続方針

- `nilo upgrade` は既存の `backup_database()` を新 backup service に差し替え、`reason=before-upgrade` を設定する。
- `Store._migrate()` の前に自動バックアップするには、`Store.__init__()` で無条件に backup すると通常 CLI のたびに副作用が出るため避ける。migration apply 相当の明示経路、または schema version 差分を検知したときだけ backup する仕組みを先に作る。
- 将来的に DB schema version table を導入し、version が上がる直前に `reason=before-migration` を生成する。
- `restore` は復元前に必ず `reason=before-restore` を生成する。
- daily は `nilo backup --reason daily` を cron / task scheduler / agent workflow から呼べる形に留め、Nilo 常駐 daemon は作らない。

## フェーズ別ロードマップ

### Phase 1: ローカルバックアップ

- `src/nilo/backup.py` を追加し、DB パス解決、backup API、integrity_check、sha256、meta 生成を実装する。
- `nilo backup` を追加し、既定で `.nilo/backups/` へ保存する。
- `backup_database()` を新実装へ委譲し、`nilo upgrade` の既存テストを保つ。
- README の既存 backup 説明を「整合バックアップ + meta」へ更新する。

### Phase 2: 一覧と復元

- `nilo backups` で `.nilo/backups/*.meta.json` を読み、日時、reason、size、sha256 短縮、integrity 結果を表示する。
- `nilo restore <path>` を追加する。
- restore 対象の integrity_check と sha256 検証を行う。
- restore 前に現在 DB を `before-restore` として退避する。
- restore 後の integrity_check を必須にする。

### Phase 3: export

- `nilo backup --export <dir>` を追加する。
- export 先でのファイル名衝突回避とコピー後 sha256 再検証を実装する。
- ドキュメントに DB 本体を直接同期しない方針を書く。
- export 結果を `.meta.json` に反映する。

### Phase 4: 自動バックアップ接続

- `nilo upgrade` の backup を `reason=before-upgrade` にする。実装済み。
- `Store` が既存 DB の不足カラムを検知した場合だけ、schema migration の ALTER 前に `reason=before-migration` のバックアップを作成する。新規 DB 作成や最新 schema の通常起動では作成しない。migration 前バックアップに失敗した場合は fail-closed とし、schema 変更へ進まない。
- restore 前 backup を Phase 2 実装として固定する。実装済み。
- reason を固定 enum として validation する。実装済み。

### Phase 5: 暗号化

- `nilo backup --encrypt` と `nilo restore --decrypt` を追加する。
- `age` コマンド存在確認、recipient 指定、失敗時の停止を実装する。
- `.nilo/config.toml` または user config の候補を設計し、recipient を保存できるようにする。
- 暗号化 meta に plaintext/ciphertext sha256 を分けて保存する。

### Phase 6: 世代管理と外部連携

- `nilo backups prune --keep 30` または `nilo backup --prune --keep 30` を検討する。
- reason 別 keep policy を検討する。例: manual は残す、daily は最新 30 件、before-restore は明示 prune まで残す。
- `postCommand` を設定に追加し、`rclone copy ...` などを呼べるようにする。
- `postCommand` は shell 文字列ではなく argv 配列またはテンプレート制限付きにし、実行ログを meta または別 log に残す。

## リスクと対策

- WAL モードで `.db` 単体をコピーすると不完全になる: backup API を使い、成果物は単一 DB に正規化する。
- バックアップ中に書き込みがある: SQLite backup API を使い、必要なら pages/progress/sleep を指定して短時間 retry できる構造にする。
- restore で現在 DB を失う: restore 前 backup を必須化し、検証失敗時は復元しない。
- meta が DB 内だけにあると DB 消失時に失われる: `.meta.json` を一次記録にする。
- export 先で上書きする: 既存ファイルを上書きせず suffix かエラーにする。
- Windows でファイル置換に失敗する: restore は Nilo の DB 接続を閉じた別プロセスで実行し、同一ディレクトリ一時ファイルから `Path.replace()` する。
- `age` が無いのに暗号化済みと思い込む: `--encrypt` 指定時はコマンド不在で失敗し、平文 export に fallback しない。
- クラウド同期中の部分ファイル: export は一時ファイル名でコピーしてから最終名へ rename する案を検討する。
- 証跡 DB そのものの直接改竄: Nilo は security boundary ではないため防止対象にしない。ただし sha256 と meta により accidental corruption と取り違えを検出しやすくする。

## テスト方針

- `backup` unit tests: 一時ディレクトリに `Store` を作り、書き込み後に `nilo backup` が `.db` と `.meta.json` を作ることを確認する。
- WAL tests: WAL モードで未 checkpoint の書き込みを作り、backup API で復元 DB にデータが入ることを確認する。
- integrity tests: 正常 DB で `integrity_check == "ok"`、壊した DB では backup/restore が停止することを確認する。
- sha256 tests: meta の sha256 と実ファイル sha256 が一致し、改変後 restore が拒否されることを確認する。
- restore tests: restore 前 backup が作られ、復元後 DB が対象バックアップ内容になることを確認する。
- export tests: export 先コピー、衝突回避、コピー後 sha256 再検証を確認する。
- upgrade tests: 既存 `test_upgrade_with_updates_pulls_reinstalls_backs_up_database_and_migrates` を新 meta 付き backup に更新し、backup 失敗時に migration へ進まない既存保証を維持する。
- CLI parser tests: `nilo backup`、`nilo backups`、`nilo restore`、`--export`、`--encrypt`、`--decrypt` の parse と handler 呼び出しを確認する。
- encryption tests: `age` 不在時は失敗することを mock で確認し、実 `age` が無い環境でも deterministic に通るようにする。
- cross-platform path tests: `~`、相対パス、空白を含むパス、Windows 風 path の扱いを `Path` ベースで確認する。

## 未決事項

- `.nilo/config.toml` を新設するか、user-level `%USERPROFILE%/.nilo/config.toml` / `~/.nilo/config.toml` を併用するか。
- schema version table をいつ導入し、migration 前 backup の発火条件をどう判断するか。
- restore に interactive confirmation を入れるか、`--yes` 必須にするか。AI 経由では人間明示依頼が必要な human gate として扱う。
- encrypted backup 後にローカル平文 backup を残すか、export 暗号化だけを作るか。
- `postCommand` の設定形式を argv 配列にするか、テンプレート文字列にするか。
- `BackupRecord` を DB 内に補助索引として追加する場合、meta との整合をどう扱うか。
- daily backup を Nilo が提案だけするか、OS scheduler 設定まで補助するか。
