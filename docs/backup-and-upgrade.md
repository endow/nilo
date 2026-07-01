# Backup と upgrade

Nilo の状態は `.nilo/nilo.db` に保存されます。ライブ DB や `.db-wal` / `.db-shm` をクラウド同期フォルダへ直接置く運用は推奨しません。

## DB バックアップ

バックアップも、人間がコマンドを覚えて実行する前提ではありません。必要になったら AI エージェントに頼みます。

```text
Nilo の DB をバックアップしておいて。
```

```text
Nilo の DB バックアップを外部退避できる形で作って。
```

```text
古いバックアップを安全な範囲で整理して。
```

バックアップは `.nilo/backups/` に `.db` と `.meta.json` として作られます。メタデータには `integrity_check` と sha256 が含まれます。

外部退避用の後処理は `.nilo/config.toml` に argv 形式で設定できます。シェルは経由しません。

```toml
[backup]
post_command = ["rclone", "copy", "{backup_path}", "remote:nilo-backups"]
```

## Upgrade

Git checkout からインストールした Nilo を更新したい場合も、普段は AI エージェントに頼めば十分です。

```text
Nilo を更新して。更新前後の状態も確認して。
```

```text
実際に更新する前に、何が実行されるかだけ確認して。
```

更新処理はローカルリポジトリの状態を確認し、fast-forward 更新、再インストール、migration を行います。`.nilo/nilo.db` がある場合は、migration 前に `.nilo/backups/` へ `reason=before-upgrade` のバックアップを作成します。

ローカル変更がある場合、更新前に停止します。変更をコミット、stash、または破棄してから再実行してください。

Backup / restore の設計境界は [design.md](design.md) を参照してください。
