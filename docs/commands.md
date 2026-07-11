# コマンドと保存されるもの

この文書は、README から外した Nilo の基本コマンド、保存対象、表示言語の補足です。

## 通常作業

通常の作業入口は `nilo work` です。呼出側が副作用の有無を`--intent inspect|change`で宣言します。Niloは依頼文の特定語句からintentを推測しません。未指定かつwork optionがない場合は安全側で`inspect`となり、Taskを作成しません。

```bash
nilo work "READMEを短く整理して" --intent change
nilo work "現在の状態を説明して" --intent inspect
nilo work --recipe bugfix "review result import が落ちる問題を直して" --intent change
nilo check --task <task_id> "python -m unittest tests.test_cli"
```

`--intent inspect`は`--task`、`--recipe`、`--no-recipe`、`--check`と併用できません。`nilo work --check` は、検証が成功した場合に AI の完了報告まで記録するショートカットです。完了判断前に検証だけを記録したい場合は `nilo check --task <task_id> "..."` を使います。

既存環境をこのintent契約へ更新した後は、`nilo agent install --project <project_id> --target all`を実行し、Codex／Claudeの生成済み運用ルールを再生成してください。旧ルールの`nilo work "<依頼>"`を残すと変更依頼がread-onlyになるため、upgrade時の再生成は必須です。

`status`、`next`、`start`、`check`、`done` は補助/advanced/fallback の入口です。

## 状態確認

`nilo status` は軽量な現在地確認です。通常表示では diff hash や roadmap / commit / history の重い集計を行わず、git の dirty 表示は tracked file の変更だけを対象にします。

```bash
nilo status
nilo status --verbose
nilo status --audit
nilo status --ai
nilo next --do
```

詳細な状態を見たい場合は `--verbose`、厳密な証跡確認は `--audit`、AI 向けコンテキストは `--ai` を使います。
`nilo next --do` は安全な次アクション候補だけを確認する日常用入口です。初期実装では実行せず、理由と次の一手だけを返します。

## 人間向けビュー

```bash
nilo view
```

`nilo view` は読み取り専用のローカルビューです。既定では `127.0.0.1:8765` にだけ公開され、DB への書き込みは行いません。

ブラウザを自動で開かない場合は `--no-open`、ポートを変える場合は `--port`、概要 JSON だけを見る場合は `--format json` を使います。

## 保存されるもの

Nilo はプロジェクトルートの `.nilo/nilo.db` に作業状態を保存します。

このリポジトリでは、作業中に生成されるローカルファイルは Git に入れません。

- `.nilo/`: 作業状態 DB、検証ログ、レポート一時ファイル
- `HANDOFF.md`: 必要時に生成する人間向け引き継ぎファイル
- `.mcp.json`: ローカル MCP 設定
- Python キャッシュ、仮想環境、coverage 出力、build artifact

Git に残すのは、ソースコード、テスト、README、設計文書、AI エージェント向け手順など、プロジェクトとして共有したいファイルです。

## 表示言語

Nilo の内部状態値、DB、JSON 出力は英語の安定した識別子を使います。

通常のコマンド出力は日本語を基本にしています。`--ai` 出力も、人間が確認しやすいように日本語を基本にし、必要な箇所では内部値を括弧で併記します。

`--json` 出力は外部連携向けのため、日本語化しません。

## ヘルプ

個別コマンドの正確なオプションは CLI の help を正本にします。

```bash
nilo --help
nilo work --help
nilo start --help
nilo check --help
nilo review --help
nilo roadmap --help
```
