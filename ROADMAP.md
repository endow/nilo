# ロードマップ

- プロジェクト: Nilo
- 今の方向: 採用済みのロードマップ項目: Failure Ledger for failure_logs
- 作業の状態: 進行中の作業はありません
- 作業の種類: 完了

## 現在のロードマップ項目

### Failure Ledger for failure_logs

- 目的: Make `failure_logs` usable as a human-readable Failure Ledger: a primary record of evidence issues, metadata mismatches, and human rejections that can be listed, summarized, inspected, resolved, and ignored without becoming automatic rules or completion gates.

#### 成功条件

- `nilo failure list`, `nilo failure summary`, `nilo failure show <failure_id>`, `nilo failure resolve <failure_id>`, and `nilo failure ignore <failure_id>` are available.
- `failure_logs` includes `source`, `actor`, `related_id`, `snapshot`, `status`, `resolved_at`, `resolved_by`, and `resolution_note` with backward-compatible migrations.
- Report import failure records include `source=report_import`, `actor=nilo`, `related_id`, `report_id`, and `status=open`.
- Human `outcome record` rejections and rework-required decisions create open failure logs with `source=outcome_record` and `actor=human`.
- `task show --ai` displays at most five open failure observations for the current task and explicitly says they are observations, not mandatory rules.
- `status --ai` displays only a compact project-level failure summary and points to `nilo failure list --project <project_id>` for details.
- `nilo doctor ai-context` reports compact failure-log context sizing metrics.
- Legacy learning tables are not recreated: `failure_patterns`, `task_failure_pattern_matches`, `derived_rules`, `active_instruction_rules`, and `success_patterns`.
- Failure logs are never injected as generated instructions and never used as a completion gate.
- README documentation states that failure logs are observations, not automatic rules or hidden requirements.
- Tests cover failure recording, listing filters, resolve, ignore, AI context display, compact status summary, and legacy learning-table absence.

## 確認待ちの案

- なし

## 進行中の作業

- なし

## 次に確認すること

- no active task; ask the user for the next concrete task within the current roadmap
- open todo を triage する: nilo todo triage --item todo_roadmap_docs_intake_gap --status ready --reason "..."
