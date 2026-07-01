# Overdrive

Overdrive は、受け入れ済みの roadmap 項目に沿って、AI エージェントの作業を連続して進めるためのモードです。

人間が細かい実行オプションを覚える必要はありません。使いたいときは、AI エージェントに自然文で依頼します。

```text
受け入れ済みのロードマップに沿って、オーバードライブモードで進めて。
```

```text
このロードマップ項目だけを対象にして、失敗が続いたら止めて。
```

Overdrive でも、人間の最終判断は不要になりません。Nilo は approval gate を必要に応じて迂回できますが、安全上の停止条件では止まります。

## Safety gates

- 破壊的操作 (`destructive_operation`)
- 認証情報や秘密情報へのアクセス (`secret_or_credential_access`)
- 課金や外部公開を伴う操作 (`billing_or_external_publication`)
- 削除操作 (`delete_operation`)
- 失敗回数の上限超過 (`max_failure_exceeded`)
- スコープ外の設計変更 (`out_of_scope_design_change`)
- 仕様が曖昧な状態 (`ambiguous_specification`)
- 予期しない未コミット変更 (`unexpected_dirty_working_tree`)

停止した場合、AI は理由と次に必要な人間判断を報告します。
