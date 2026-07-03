# Overdrive

Overdrive は、確認待ちを減らして現在のタスクを進めるためのモードです。受け入れ済みの roadmap 項目に沿った連続実行にも使えますが、既定では現在の依頼対象だけを進めます。

人間が細かい実行オプションを覚える必要はありません。使いたいときは、AI エージェントに自然文で依頼します。

```text
受け入れ済みのロードマップに沿って、オーバードライブモードで進めて。
```

```text
このロードマップ項目だけを対象にして、失敗が続いたら止めて。
```

Overdrive でも、人間の最終判断は不要になりません。Nilo は approval gate を必要に応じて迂回できますが、安全上の停止条件では止まります。

## Scope

`nilo run --overdrive` と `nilo roadmap execute --overdrive` は `--scope` を受け付けます。

- `task`: 既定。現在の task の実装、検証、report、completion まで進めます。unrelated な next task には自動で進みません。
- `commitment`: 現在の roadmap commitment 配下の task まで進めます。
- `project`: 現在 project 内の作業を対象にします。
- `queue`: キュー全体を対象にします。別 task に進む自動進行を明示的に許すときだけ使います。

人間が「全部オーバードライブで」と言っても、既定では `task` として扱います。別 task に移る前には、AI は次のような区切りを出して停止します。

```text
ここまでで依頼された検証タスクは完了しました。
次に別タスク task_xxx が残っています。
overdrive を続けるには --scope queue または明示承認が必要です。
```

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
