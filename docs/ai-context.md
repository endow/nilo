# AI context と `status --ai`

この文書は、AI エージェントが Nilo の状態を読むときの補足です。

## 入口

AI は作業開始時に、対象 repository で次を確認します。

```bash
nilo status --ai --project <project_id>
nilo next --project <project_id>
```

Active task がある場合は、`nilo next` の先頭 action に従います。Active recipe 中の next は recipe の指示だけを対象にします。

## `status --ai` の既定出力

`nilo status --ai` の既定出力は短い作業カードです。project、active task、next action、blocker summary、latest verification、latest review、required commands、detail commands だけを出し、証跡や roadmap や review findings の本文は毎回展開しません。

詳細が必要な場合は、目的に応じて次を使います。

```bash
nilo status --ai --verbose
nilo task status --task <task_id> --ai
nilo evidence show --task <task_id> --ai
nilo review status --task <task_id> --format json
nilo roadmap status --project <project_id> --ai
nilo failure list --project <project_id>
```

証跡は消さず、completion / audit / evidence show 側で厳密性を維持します。

## コンテキスト量

Compact AI context の文字数上限は `NILO_AI_CONTEXT_MAX_CHARS` で調整できます。この値は Nilo プロセス起動時に読み込まれます。

## 完了扱い

Evidence が stale / missing / failed の場合は完了扱いにしません。Unresolved review finding がある場合も、完了前の確認事項として残します。

最終完了、commit、force、roadmap close は人間の判断を必要とします。AI は判断材料を集め、検証とレビューの状態を報告します。
