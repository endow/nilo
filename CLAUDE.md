

<!-- BEGIN NILO MANAGED BLOCK -->
## Nilo 必須プロトコル

このプロジェクトでは Nilo を AI 作業の状態管理装置として使う。日常運用の表の入口（daily surface）は `nilo init` / `nilo start` / `nilo status` / `nilo next` / `nilo check` / `nilo report` / `nilo done` / `nilo reject` に寄せ、roadmap / review / quality / rules / MCP などは必要時に使う裏側の機能として扱う。

## Nilo MCP Reviewer Protocol

When acting as the `claude-code` reviewer through Nilo MCP, always refresh reviewer availability before claiming any review.

Before calling `claim_next_review`, call `register_reviewer` with:

```json
{
  "reviewer": "claude-code",
  "capabilities": [
    "review"
  ],
  "max_concurrent": 1,
  "metadata": {
    "worker_path": "claude-code-mcp-session",
    "dispatch_capable": true,
    "source": "real Claude Code session"
  }
}
```

Then call `claim_next_review`.

Do not use `reviewer-start` for Claude Code reviews. It is heartbeat-only and not a real reviewer worker.

Do not use `reviewer-worker --result-file` as a substitute for Claude Code review.

Do not create or import results under the `claude-code` reviewer name unless this Claude Code session actually claimed the review through Nilo MCP.

The required flow is:

1. `register_reviewer`
2. `claim_next_review`
3. generate a real review response
4. `import_review_result`

A connected Nilo MCP server does not by itself make the reviewer available. The reviewer becomes available only after this Claude Code session calls `register_reviewer` with dispatch-capable metadata.


## 必須手順

1. 現在地を確認する。Nilo MCP が利用可能な場合は、作業開始前に `get_agent_work_context(project_id="nilo")` / `get_next_step(project_id="nilo")` で現在地と次の許可 action を確認する。Nilo MCP が設定済みでも callable tool として見えない場合は、まず tool discovery / `tool_search` で Nilo MCP の lazy loading を試す。それでも Nilo MCP tool が露出しない、または起動に失敗している場合は、「Nilo MCP が current session にロードされていない」と報告したうえで `nilo status --project nilo` と `nilo next --project nilo` を CLI fallback として実行し、先頭の next action だけに従う。
2. 明確なユーザー依頼があり active task が無い場合は、ユーザーに Nilo 操作を依頼せず、`nilo start --project nilo ...` で依頼内容に対応する task を作成してから進める。task 作成は裏側の作業として扱い、ユーザー向け説明は依頼された変更内容を中心にする。
3. active task に着手する前に必ず `nilo instruct --task <task_id>` を実行し、指示・完了条件・禁止事項に従う。
4. `next` / `next_actions` が複数ある場合は先頭の next action だけを実行する。迷ったらコマンドを推測せず、`status` / `next` を再確認し、先頭の next action だけを実行して、再度 status を報告して停止する。
5. 検証結果と報告を Nilo に戻す。通常は `nilo check --task <task_id> "<command>"` と `nilo report --task <task_id> --file .nilo/reports/<task_id>.md` を使い、必要な場合だけ `nilo report import` 相当の裏側の取り込みや MCP の `submit_agent_report` / `record_test_result` を使う。
6. 完了・commit・force・roadmap close は human gate として扱い、人間の明示指示なしに進めない。

## AI 間依頼プロトコル

- AI エージェント間の作業依頼・レビュー依頼は Nilo MCP 経由だけにする。相手エージェントのローカル CLI やプロセス起動コマンドは直接実行しない。
- 「Claudeにレビューして」「Codexにレビューして」という通常の AI 間レビュー依頼では、依頼する側は `request_task_review` を直接使わず、高レベル API の `dispatch_review` / `run_agent_review` / `request_and_run_review` のいずれかを使う。
- `request_task_review` は低レベル API として残すが、review request 作成だけで reviewer process 起動、claim、review 実行、`import_review_result`、final status 確認までは行わないため、通常の AI 間レビュー依頼では使わない。
- AI 間依頼に必要な MCP tool が callable tool として見えない場合は、代替 CLI に逃げず、「Nilo MCP が current session にロードされていない」と報告して停止する。

## 禁止事項

- 対応タスクなしに勝手に実装へ進まない。明確なユーザー依頼がある場合は先に task を作成してから進める
- 検証していない成果を検証済みまたは完了として報告しない
- ユーザーの明示指示なしに `nilo task complete` や `nilo roadmap close` を実行しない
- ユーザーの明示許可なしに `--commit` を使わない
- ユーザーの明示許可なしに `--force` や人間承認を代替する操作を使わない
<!-- END NILO MANAGED BLOCK -->
