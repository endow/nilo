# Nilo

> English version: [README.en.md](README.en.md)
>
> この README が正本です。英語版は補助的な案内として提供しています。

Nilo は、AI エージェントに任せた開発作業の **現在地、完了条件、検証結果、レビュー結果** をプロジェクト内に残すためのローカル CLI です。

Codex、Claude Code、ChatGPT、ローカル LLM などを使っていると、「AI は終わったと言っているが、何を根拠に完了としてよいのか」が曖昧になりがちです。Nilo はその作業状態を `.nilo/nilo.db` に保存し、別の AI エージェントに引き継いでも同じ証跡を確認できるようにします。

## できること

- AI 作業の現在地を確認する
- 作業ごとの完了条件を残す
- 検証コマンドと結果を記録する
- レビュー結果と未解決指摘を残す
- 検証、レビュー、完了判断を git のスナップショットに結びつける
- AI の作業報告と人間の受け入れ判断を分けて記録する
- 失敗ログ、方針メモ、後続作業を後から参照できる形で残す
- 共通手順をレシピ化して、同じ種類の作業を始めやすくする
- `.nilo/nilo.db` を検証済みバックアップとして退避する

Nilo はタスク管理 SaaS でも、CI でも、セキュリティ境界でもありません。協力的な AI エージェントが CLI / MCP を通って作業する前提で、自己申告だけではなく、検証とレビューの記録を見て判断できるようにする道具です。

## インストール

必要なもの:

- Python 3.12 以上
- Git

```bash
git clone https://github.com/endow/nilo.git
cd nilo
python -m pip install -e .
```

インストール確認:

```bash
nilo --version
nilo status
```

このリポジトリ自体のテストを実行する場合:

```bash
python -m unittest discover tests
```

## 最短の使い方

使いたいプロジェクトのルートで一度だけ初期化します。

```bash
nilo init
```

主に作られるもの:

- `.nilo/nilo.db`: 作業状態を保存する SQLite DB
- `.nilo/agent-instructions.md`: AI エージェント向けの共通 runtime 指示
- `AGENTS.override.md`: Codex 向けのローカル指示
- `CLAUDE.local.md`: Claude Code 向けのローカル指示

これらの runtime ファイルは通常コミットしません。`nilo init` は local exclude も整えます。

ここから先、通常利用で人間が Nilo のコマンドを覚えて打つ必要はありません。あとは AI エージェントに普段どおり依頼します。

```text
Nilo の状態を確認してから進めて。
```

AI 側は Nilo を読んで、現在地、作業指示、完了条件、検証、報告を扱います。人間が毎回細かいコマンドを覚える必要はありません。

### 大きな作業では roadmap を推奨する

Nilo では、小さな修正は task としてそのまま進めます。

複数モジュールにまたがる変更、DB schema / migration、CLI 追加、AI向け状態表示の変更、README / docs / tests まで含む変更では、AI は roadmap で整理することを推奨します。人間が承認した場合だけ roadmap を作り、承認後に task へ分解します。

```bash
nilo roadmap discuss
nilo roadmap accept
nilo roadmap task-plan
```

roadmap は、AI に大きな作業を一気に実装させないための分解手順です。目的・非目的・成功条件を先に固定し、実装 task に分けてから進めます。

## 人間がよく使う聞き方

```text
次は？
```

```text
何か残ってる？
```

```text
検証は通ってる？
```

```text
この内容で完了にしていい？
```

望ましい返答は、たとえば次のようなものです。

```text
今はレビュー待ちです。
検証は通っています。
未解決の指摘が 1 件あります。
次はその指摘への対応です。
```

人間の役割は、AI の報告と Nilo に残った証跡を見て、受け入れるか、差し戻すか、追加確認するかを決めることです。AI が勝手に最終完了を確定する前提にはしていません。

## AI エージェントとの連携

Nilo は特定の AI エージェントに依存しません。AI は通常の CLI または MCP (Model Context Protocol) 経由で Nilo を使います。

AI エージェントには次のように依頼します。

```text
Nilo の状態を見てから進めて。
```

```text
検証結果と未解決事項を残してから報告して。
```

```text
必要なら別の AI にレビューを依頼して。
```

MCP や CLI が使えない場合は、代替手段で完了扱いにせず、Nilo を読めないことを人間に報告する運用を想定しています。

### MCP identity guard

Nilo MCP が呼べる場合でも、それが現在の repository の Nilo DB を見ているとは限りません。

Nilo MCP は、参照中の repository / project / git root / DB path を identity として表示します。

identity が現在の作業 repository と一致しない場合、その MCP は使わず、対象 repository の作業ディレクトリで CLI を使います。
MCP tool の `expected_project` は Nilo DB 内の任意の project id ではなく、通常は repository directory name として扱う repository identity guard です。
不一致時は `ok: false`、`error: "repository_mismatch"` を返し、通常の status payload は返しません。

```bash
nilo mcp doctor
nilo status --ai
nilo next
```

MCP が callable であることと、現在の repository に対して正しいことは別です。

### MCP multi-workspace

Nilo MCP は、既定では MCP server を起動した repository の `.nilo/nilo.db` を使います。

複数 repository を同時に扱う場合は、MCP tool の呼び出しに `project_root` または `workspace` を指定できます。

```json
{
  "project_root": "/path/to/Chiffon"
}
```

または、workspace を登録します。

```bash
nilo workspace add Chiffon --root /path/to/Chiffon
nilo workspace list
```

登録後は、MCP tool に workspace 名を渡せます。

```json
{
  "workspace": "Chiffon"
}
```

MCP response には identity が含まれます。
`repository_name` / `db_path` が対象 repository と一致していることを確認してください。

## レシピ

レシピは、よくある作業を同じやり方で始めるための仕組みです。

例:

- ドキュメントだけを更新する
- 実装前に設計メモを作る
- 小さな実装変更を、検証とレビュー観点つきで進める
- リリースバージョン、日英リリースノート、GitHub リリース準備を進める

人間は自然文で依頼できます。

```text
README 更新用のレシピで進めて。
```

```text
いまやった修正作業を、次回も同じ進め方でできるようにレシピ化して。
```

必要な場合だけ、レシピはプロジェクト内の `.nilo/recipes/` に保存されます。

### release recipe のバージョン提案

release recipe は、`target_version` が未指定の場合、現在バージョンと最新 git tag から次の候補を出します。

小さな修正だけなら patch version を候補にします。

```bash
0.1.9 -> 0.1.10
```

CLI 追加、DB schema / migration、recipe 変更、AI向け出力、roadmap / review / failure log など主要機能に関わる変更がある場合は、minor version を推奨します。

```bash
0.1.9 -> 0.2.0
```

minor が推奨される場合、Nilo は理由と再実行コマンドを表示します。
明示的に `target_version` を指定した場合、その値は上書きされません。

```bash
nilo recipe run release --project nilo --var target_version=0.2.0
```

## オーバードライブモード

オーバードライブモードは、受け入れ済みのロードマップ項目に沿って、AI エージェントの作業を連続して進めるためのモードです。

人間が細かい実行オプションを覚える必要はありません。使いたいときは、AI エージェントに自然文で依頼します。

```text
受け入れ済みのロードマップに沿って、オーバードライブモードで進めて。
```

```text
このロードマップ項目だけを対象にして、失敗が続いたら止めて。
```

オーバードライブモードでも、人間の最終判断は不要になりません。Nilo は approval gate を必要に応じて迂回できますが、次のような safety gate では停止します。

- 破壊的操作 (`destructive_operation`)
- 認証情報や秘密情報へのアクセス (`secret_or_credential_access`)
- 課金や外部公開を伴う操作 (`billing_or_external_publication`)
- 削除操作 (`delete_operation`)
- 失敗回数の上限超過 (`max_failure_exceeded`)
- スコープ外の設計変更 (`out_of_scope_design_change`)
- 仕様が曖昧な状態 (`ambiguous_specification`)
- 予期しない未コミット変更 (`unexpected_dirty_working_tree`)

## DB バックアップ

Nilo の状態は `.nilo/nilo.db` に保存されます。ライブ DB や `.db-wal` / `.db-shm` をクラウド同期フォルダへ直接置く運用は推奨しません。バックアップ成果物として退避してください。

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

## Nilo を更新する

git checkout からインストールした Nilo を更新したい場合も、普段は AI エージェントに頼めば十分です。

```text
Nilo を更新して。更新前後の状態も確認して。
```

```text
実際に更新する前に、何が実行されるかだけ確認して。
```

更新処理はローカルリポジトリの状態を確認し、fast-forward 更新、再インストール、migration を行います。`.nilo/nilo.db` がある場合は、migration 前に `.nilo/backups/` へ `reason=before-upgrade` のバックアップを作成します。

ローカル変更がある場合、更新前に停止します。変更をコミット、stash、または破棄してから再実行してください。

## 保存されるもの

Nilo はプロジェクトルートの `.nilo/nilo.db` に作業状態を保存します。

このリポジトリでは、作業中に生成されるローカルファイルは Git に入れません。

- `.nilo/`: 作業状態 DB、検証ログ、レポート一時ファイル
- `HANDOFF.md`: 必要時に生成する人間向け引き継ぎファイル
- `.mcp.json`: ローカル MCP 設定
- Python キャッシュ、仮想環境、coverage 出力、build artifact

Git に残すのは、ソースコード、テスト、README、設計文書、AI エージェント向け手順など、プロジェクトとして共有したいファイルです。

### 表示言語

Nilo の内部状態値、DB、JSON 出力は英語の安定した識別子を使います。

通常のコマンド出力は日本語を基本にしています。  
`--ai` 出力も、人間が確認しやすいように日本語を基本にし、必要な箇所では内部値を括弧で併記します。

`--json` 出力は外部連携向けのため、日本語化しません。

## 仕組み

Nilo の考え方は単純です。

> Evidence Before Trust
> AI の自己申告より、実体のある変更と検証証跡を先に見る。

Nilo は次の状態を同じものとして扱いません。

- AI が「完了しました」と報告しただけの状態
- Nilo が検証コマンドを実行して記録した状態
- 外部 AI が検証したと報告した状態
- レビュー担当が特定のスナップショットに対して指摘した状態
- 人間が特定のスナップショットを完了として受け入れた状態

検証、レビュー、完了判断は、対象にした `git_head`、`git_diff_hash`、`working_tree_dirty` と結びつけて保存されます。作業後にコードが変わった場合、古い検証やレビューは現在の完了根拠として扱わず、stale な証跡として区別します。

失敗ログも保存しますが、Nilo は失敗ログから自動でルールや隠れた要件を生成しません。次に作業する人間や AI が「前回どこで詰まったか」を確認するための台帳です。

詳細な設計境界は [docs/design.md](docs/design.md) を参照してください。

## 開発者向け

ここは Nilo 本体を開発する人向けです。通常利用では読む必要はありません。

CLI の詳細:

```bash
nilo --help
nilo start --help
nilo check --help
nilo review --help
nilo roadmap --help
```

テストは目的に合わせて `quick` / `targeted` / `full` を選びます。timeout は選んだ範囲の保険であり、全体テストを常用するための前提ではありません。

```bash
nilo check "python -m unittest tests.test_verification" --project nilo --mode quick --timeout 60
nilo check "python tests/run_cli_group.py verification" --project nilo --mode targeted --timeout 120
nilo check "python -m unittest discover tests" --project nilo --mode full --timeout 300
```

`quick` は狭い smoke check、`targeted` は変更領域や `tests.test_cli` の一部、`full` は release や広範囲の変更で使います。`tests.test_cli` の focused group は helper で実行できます:

```bash
python tests/run_cli_group.py review
python tests/run_cli_group.py verification
python tests/run_cli_group.py roadmap
```

設計の詳細は [docs/design.md](docs/design.md) を参照してください。

## ライセンス

Apache License 2.0

詳細は [LICENSE](LICENSE) を参照してください。
