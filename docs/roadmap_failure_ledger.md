# Failure Ledger for failure_logs

## Intent

Make `failure_logs` usable as a human-readable Failure Ledger: a primary record of evidence issues, metadata mismatches, and human rejections that can be listed, summarized, inspected, resolved, and ignored without becoming automatic rules or completion gates.

## Success Criteria

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

## Non Goals

- Do not restore `failure_patterns`, `derived_rules`, `active_instruction_rules`, or `success_patterns`.
- Do not generate rules, instructions, classifications, summaries, or requirements from failure logs.
- Do not use failure logs to block `task complete` or `require_ai_completion_evidence`.
- Do not add LLM-based failure classification or summarization.

## Autonomy Scope

- Add CLI parser and handler modules following the existing project structure.
- Extend the SQLite schema with additive `ALTER TABLE ADD COLUMN` migrations.
- Refactor old `record_failure_and_rule` naming to `record_failure_log` or `record_failure`, keeping a compatibility wrapper only if needed.
- Update report import and outcome record call sites to populate the new ledger fields.
- Add compact display helpers for task AI context, project AI status, and doctor diagnostics.
- Add focused tests and documentation updates.

## Review Gates

- Human reviews any behavior that could look like instruction injection, generated rules, or completion gating before accepting the implementation.
- Human decides final completion, commit, and roadmap close.

## Evidence Policy

- Run the targeted failure ledger tests added for this commitment.
- Run the existing CLI or roadmap-related tests needed to prove legacy learning tables are not recreated.
- Record verification with `nilo check` after test execution.
