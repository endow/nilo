# Nilo Phase 2 Transition Hardening

## Intent

Harden Nilo's transition layer so core state changes are atomic, auditable, and consistently validated across CLI, MCP, and internal automation. The work should preserve existing database compatibility while moving important write paths toward transition-owned APIs.

## Success Criteria

- All core transition writes that mutate state and create `transition_events` run inside a single `Store.transaction()` boundary.
- Task completion, completion invalidation, review result import, review finding update, roadmap accept/reject/adopt/close, todo triage/convert/promote, failure resolve/ignore, verification record, and agent report import are atomic.
- A write path allowlist documents each important table, its allowed writer, whether a transition is required, and any migration exception.
- Production direct writes to core tables are detected by tests, with low-level helper and initialization/configuration exceptions documented.
- Review request lifecycle and overdrive lifecycle writes are either transition-owned or explicitly isolated behind documented internal transition helpers.
- Dangerous transitions have optimistic concurrency guards or a staged warning-to-strict migration plan.
- New data is protected by staged schema invariants or doctor warnings for invalid actors, statuses, human confirmation values, and missing transition audit evidence.
- CLI and MCP paths for equivalent state changes call the same transition policy and leave equivalent audit events.
- Queue, status, and doctor surfaces do not hide transition audit inconsistencies.
- Existing Nilo databases remain readable and are not broken by constraint rollout.

## Non Goals

- Do not rewrite the entire storage layer or replace SQLite.
- Do not introduce irreversible schema constraints before legacy data has doctor warnings and repair paths.
- Do not force all project/configuration writes through the transition layer unless they mutate core work state.
- Do not close existing roadmap commitments or complete stale existing tasks as part of this hardening.

## Autonomy Scope

- Implementation may add focused internal APIs, transaction support, tests, and documentation.
- Implementation may move direct writes from handlers/helpers into `transitions.py` or a dedicated transition module when that reduces risk.
- Human confirmation remains required for human-only decisions such as roadmap acceptance, failure ignore, accepted-risk review findings, and dangerous todo closure.
- Public, destructive, or release operations remain outside autonomous execution and require explicit human approval.

## Review Gates

- Review after Phase 2-A because transaction boundaries affect the core write path.
- Review after Phase 2-B because allowlist enforcement may change handler/helper responsibilities.
- Review before enabling strict direct-write rejection by default.
- Review before adding non-compatible DB constraints.

## Evidence Policy

- Prefer focused tests for each transaction rollback and success invariant before broader CLI regression runs.
- Run targeted tests for `tests/test_transitions.py`, review import/update paths, todo paths, roadmap paths, failure paths, and MCP parity after each phase.
- Run a full test pass only after Phase 2-A and again before enabling strict write guards or schema constraints.
- Record verification with `nilo check` for each implementation task before completion.

## Suggested Tasks

- Phase 2-A: add `Store.transaction()` and make completion, review import, roadmap, todo, failure, verification, and agent report transitions atomic.
- Phase 2-A: add rollback tests for completion, review import, roadmap accept, todo conversion, and transition success event persistence.
- Phase 2-B: add `docs/internal/write-paths.md` with the production write allowlist and exception rationale.
- Phase 2-B: add tests that detect production direct writes to core tables outside allowed transition/helper paths.
- Phase 2-B: move review request lifecycle and overdrive lifecycle writes behind transition-owned APIs or documented internal helpers.
- Phase 2-C: add optimistic concurrency tokens for task, roadmap, todo, failure, and review finding transitions.
- Phase 2-C: add doctor checks and staged DB constraints for invalid status, actor, human confirmation, and missing transition events.
- Phase 2-C: add CLI/MCP parity tests for review import, review finding update, verification record, task completion, todo transitions, failure transitions, roadmap transitions, and understanding approval.
