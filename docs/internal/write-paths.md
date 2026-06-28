# Nilo Internal Write Paths

This allowlist documents production writes to core state tables. The Phase 2-B
guard is warning-only for compatibility; strict rejection is a later migration
step after legacy paths have transition coverage or explicit exceptions.

| Table | Allowed writer | Transition required | Rationale / exception |
| --- | --- | --- | --- |
| `transition_events` | `src/nilo/transitions.py` | Yes | Audit events are emitted inside transition transactions. |
| `task_completions` | `src/nilo/transitions.py`, `src/nilo/cli_handlers/task.py` commit metadata update | Yes for completion creation | Completion creation is transition-owned; post-commit snapshot annotation is a compatibility exception. |
| `review_results` | `src/nilo/transitions.py` | Yes | Review result import is transition-owned. |
| `review_findings` | `src/nilo/transitions.py` | Yes | Findings are created with review result import. |
| `review_finding_updates` | `src/nilo/transitions.py` | Yes | Finding status changes are transition-owned. |
| `review_requests` | `src/nilo/review_lifecycle.py`, `src/nilo/transitions.py` completion update | Helper required | Request lifecycle writes go through the internal review lifecycle helper; review-result completion is part of transition import. |
| `agent_reports` | `src/nilo/agent_report_import.py` | Indirect | Agent report import is isolated in a low-level helper and called by transition-owned import paths. |
| `evidence_checks` | No production writer | No | Retained schema; current verification uses `verification_runs`. |
| `verification_runs` | `src/nilo/transitions.py` | Yes | Verification recording is transition-owned. |
| `failure_logs` | `src/nilo/failure.py`, `src/nilo/transitions.py` | Yes for resolve/ignore | Failure creation is isolated in a low-level helper; resolution and ignore are transition-owned. |
| `roadmap_commitments` | `src/nilo/transitions.py`, `src/nilo/cli_handlers/roadmap.py` proposal staging | Yes for accept/reject/adopt/close | Human decision transitions are transition-owned; proposal import staging remains a documented exception. |
| `roadmap_revisions` | `src/nilo/transitions.py`, `src/nilo/cli_handlers/roadmap.py` proposal staging | Yes for decisions | Revision decisions are transition-owned; proposal import staging remains a documented exception. |
| `todos` | `src/nilo/transitions.py`, `src/nilo/cli_handlers/todo.py`, `src/nilo/mcp_server.py` create endpoint | Yes for triage/convert/promote | Todo lifecycle changes are transition-owned; initial intake creation is allowed. |
| `tasks` | `src/nilo/transitions.py`, task creation handlers, roadmap/task planning helpers, MCP create endpoint, `src/nilo/cli_handlers/workflow.py` base commit update | Yes for todo conversion; create/update exceptions documented | Task creation and instruction-time metadata update remain compatibility exceptions until write routing is fully split. |
| `instructions` | `src/nilo/cli_handlers/workflow.py` | No | Instruction generation is append-only task workflow output. |
| `understanding_checks` | `src/nilo/transitions.py`, `src/nilo/cli_handlers/workflow.py` prepare/import | Yes for approval | Approval is transition-owned; prepare/import are append-only workflow inputs. |
| `overdrive_runs` | `src/nilo/overdrive.py` | Internal helper | Overdrive lifecycle writes are isolated in the overdrive module. |
| `overdrive_events` | `src/nilo/overdrive.py` | Internal helper | Overdrive audit events are isolated in the overdrive module. |

Static tests enforce this allowlist for literal `Store.insert` and
`Store.update` calls in `src/nilo`. Test fixtures and low-level SQLite
migrations are outside this production write-path check.
