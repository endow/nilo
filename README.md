# Nilo

> English version: [README.en.md](README.en.md)
>
> この README が正本です。英語版は補助的な案内として提供しています。

Nilo は、AI エージェントに任せた開発作業の **現在地、完了条件、検証結果、レビュー結果、人間の受け入れ判断** を、プロジェクト内に残すためのローカル CLI です。

合言葉は **Evidence Before Trust**。AI の「完了しました」という自己申告だけでは完了扱いにせず、何を変更し、何を検証し、誰がレビューし、最終的に人間がどう判断したかを分けて保存します。

Nilo はタスク管理 SaaS でも、CI でも、セキュリティ境界でもありません。協力的な AI エージェントが CLI / MCP を通って作業する前提で、会話履歴に埋もれがちな判断材料を `.nilo/nilo.db` に残すための道具です。

## なぜ必要か

AI と一緒に開発していると、「どこまで終わったのか」「検証は本当に通ったのか」「別の AI に引き継いでも分かるのか」が曖昧になりがちです。

Nilo は、作業の意図、実行結果、検証、レビュー、未解決事項を同じプロジェクト状態として保存します。人間は細かいコマンドを覚えなくても、AI に自然文で頼むだけで Nilo の証跡を確認しながら進められます。

## できること

- AI 作業の現在地と次にやることを確認する
- 作業ごとの完了条件を残す
- 検証コマンドと結果を記録する
- レビュー結果と未解決指摘を残す
- 検証、レビュー、完了判断を git のスナップショットに結びつける
- AI の作業報告と人間の受け入れ判断を分けて記録する
- 失敗ログ、方針メモ、後続作業を後から参照する
- 定型作業を recipe として再利用する
- Nilo DB を検証済みバックアップとして退避する

## インストール

必要なもの:

- Python 3.12 以上
- Git

```bash
git clone https://github.com/endow/nilo.git
cd nilo
python -m pip install -e .
```

確認:

```bash
nilo --version
nilo status
```

## 最短の使い方

使いたいプロジェクトのルートで一度だけ初期化します。

```bash
nilo init
```

主に `.nilo/nilo.db`、AI エージェント向け runtime 指示、ローカル exclude が作られます。生成される runtime ファイルは通常コミットしません。

ここから先、通常利用で人間が Nilo の細かいコマンドを覚えて打つ必要はありません。AI エージェントに普段どおり依頼します。

```text
Nilo の状態を確認してから進めて。
```

```text
検証結果と未解決事項を残してから報告して。
```

```text
この内容で完了にしていいか、Nilo の証跡を見て教えて。
```

AI 側は Nilo を読んで、現在地、作業指示、完了条件、検証、報告を扱います。人間の役割は、AI の報告と Nilo に残った証跡を見て、受け入れるか、差し戻すか、追加確認するかを決めることです。

## 検証レベル

Nilo は検証を省略するための道具ではありません。作業段階に応じて、記録する検証レベルを切り替えます。

- `changed check`: 変更ファイルから必要な shard を選ぶ、作業中の高速確認。
- `smoke` / `compat`: CLI の基本動作や互換性を短時間で見る確認。
- `full check`: release や広い変更の前提になる、全 shard 相当の確認。
- `audit snapshot`: 証跡や完了判断が現在の git 状態と一致するかを見る厳密な確認。

release prepare は、安全な場合に full check を publish 前へ defer できます。ただし release publish は、公開操作の前に有効な full check が存在するか確認し、なければ実行します。

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

望ましい返答は、たとえば次のようなものです。

```text
今はレビュー待ちです。
検証は通っています。
未解決の指摘が 1 件あります。
次はその指摘への対応です。
```

## `nilo view`

```bash
nilo view
```

`nilo view` は、Nilo に残ったタスク、検証、レビュー、失敗ログ、タスク分析をブラウザで確認するための読み取り専用ローカルビューです。

## recipe / roadmap / overdrive

Recipe は、よくある作業を同じやり方で始めるための仕組みです。ドキュメント更新、不具合修正、パフォーマンス調査、リリース準備のような定型作業を、完了条件や検証観点つきで通常の Nilo Task にできます。

Roadmap は、大きな作業を一気に実装させないための作業計画です。目的、非目的、成功条件を先に固定し、人間が承認した後で実行 Task に分けます。

Overdrive は、受け入れ済みの roadmap 項目に沿って AI エージェントの作業を連続して進めるモードです。人間の最終判断は不要にならず、破壊的操作、外部公開、秘密情報アクセス、仕様の曖昧さなどでは停止します。

## MCP 連携

Nilo は特定の AI エージェントに依存しません。Codex、Claude Code、ChatGPT、ローカル LLM などは、通常の CLI または MCP (Model Context Protocol) 経由で Nilo を使えます。

MCP が呼べることと、現在の repository の Nilo DB を見ていることは別です。複数 workspace を扱う場合は identity を確認し、違う repository の DB を根拠にしない運用を前提にしています。

## 詳細ドキュメント

- [コマンドと保存されるもの](docs/commands.md)
- [AI context と `status --ai`](docs/ai-context.md)
- [Roadmap](docs/roadmap.md)
- [MCP 連携](docs/mcp.md)
- [Recipes](docs/recipes.md)
- [Overdrive](docs/overdrive.md)
- [Backup と upgrade](docs/backup-and-upgrade.md)
- [開発者向け手順](docs/development.md)
- [設計思想と境界](docs/design.md)

## ライセンス

Apache License 2.0

詳細は [LICENSE](LICENSE) を参照してください。
