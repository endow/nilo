from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  tech_stack TEXT NOT NULL,
  rules TEXT NOT NULL,
  default_completion_criteria TEXT NOT NULL,
  available_models TEXT NOT NULL,
  fallback_models TEXT NOT NULL,
  requires_local_execution INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  acceptance_criteria TEXT NOT NULL DEFAULT '[]',
  parent_task_id TEXT,
  split_index INTEGER,
  task_type TEXT NOT NULL DEFAULT 'implementation',
  risk_level TEXT NOT NULL DEFAULT 'medium',
  requires_understanding_check INTEGER NOT NULL DEFAULT 0,
  roadmap_commitment_id TEXT NOT NULL DEFAULT '',
  roadmap_item_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  assigned_model_profile TEXT NOT NULL,
  degradation_mode TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'normal',
  base_commit TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instructions (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  applied_rule_ids TEXT NOT NULL,
  applied_failure_pattern_ids TEXT NOT NULL DEFAULT '[]',
  degradation_mode TEXT NOT NULL,
  body_md TEXT NOT NULL,
  report_format_md TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_reports (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  agent TEXT NOT NULL,
  claimed_status TEXT NOT NULL,
  changed_files TEXT NOT NULL,
  body_md TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_checks (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  report_id TEXT NOT NULL,
  status TEXT NOT NULL,
  issues TEXT NOT NULL,
  metadata TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verification_runs (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  evidence_check_id TEXT,
  source TEXT NOT NULL DEFAULT 'nilo_executed',
  command TEXT NOT NULL,
  cwd TEXT NOT NULL,
  stdout TEXT NOT NULL,
  stderr TEXT NOT NULL,
  exit_code INTEGER,
  timed_out INTEGER NOT NULL,
  timeout_seconds REAL NOT NULL,
  git_head TEXT,
  metadata TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS failure_logs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  report_id TEXT,
  category TEXT NOT NULL,
  message TEXT NOT NULL,
  severity TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_rules (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  source_failure_ids TEXT NOT NULL,
  source TEXT NOT NULL,
  auto_activated INTEGER NOT NULL,
  manually_disabled INTEGER NOT NULL,
  rule_text TEXT NOT NULL,
  tags TEXT NOT NULL,
  severity TEXT NOT NULL,
  confidence REAL NOT NULL,
  recurrence_count INTEGER NOT NULL,
  success_count INTEGER NOT NULL,
  last_seen_at TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS active_instruction_rules (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  instruction_id TEXT NOT NULL,
  derived_rule_id TEXT NOT NULL,
  selection_score TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS failure_patterns (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  severity TEXT NOT NULL,
  scope TEXT NOT NULL,
  trigger_phrases TEXT NOT NULL,
  failure_summary TEXT NOT NULL,
  required_behavior TEXT NOT NULL,
  preflight_checks TEXT NOT NULL,
  completion_evidence TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_failure_pattern_matches (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  failure_pattern_id TEXT NOT NULL,
  match_reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_profiles (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  status TEXT NOT NULL,
  capabilities TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_usage_logs (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  model_profile_id TEXT NOT NULL,
  purpose TEXT NOT NULL,
  degradation_mode TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outcome_reviews (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  agent_report_id TEXT,
  evidence_check_id TEXT,
  decision TEXT NOT NULL,
  reason TEXT NOT NULL,
  concerns TEXT NOT NULL,
  rework_required INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quality_reviews (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  scores TEXT NOT NULL,
  summary TEXT NOT NULL,
  issues TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_requests (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  requester TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  status TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_reviewers (
  id TEXT PRIMARY KEY,
  reviewer TEXT NOT NULL,
  status TEXT NOT NULL,
  capabilities TEXT NOT NULL,
  max_concurrent INTEGER NOT NULL,
  metadata TEXT NOT NULL,
  last_heartbeat_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_results (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  review_request_id TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  verdict TEXT NOT NULL,
  summary TEXT NOT NULL,
  body_md TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_dispatches (
  id TEXT PRIMARY KEY,
  actor TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  task_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  review_request_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  command TEXT NOT NULL,
  args TEXT NOT NULL,
  working_directory TEXT NOT NULL,
  exit_code INTEGER,
  stdout TEXT NOT NULL,
  stderr TEXT NOT NULL,
  failure_stage TEXT NOT NULL,
  failure_reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_findings (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  review_request_id TEXT NOT NULL,
  review_result_id TEXT NOT NULL,
  title TEXT NOT NULL,
  severity TEXT NOT NULL,
  status TEXT NOT NULL,
  file_path TEXT NOT NULL,
  line TEXT NOT NULL,
  blocking INTEGER NOT NULL,
  description TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_finding_updates (
  id TEXT PRIMARY KEY,
  finding_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  previous_status TEXT NOT NULL,
  new_status TEXT NOT NULL,
  reason TEXT NOT NULL,
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quality_score_schemas (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL UNIQUE,
  required_scores TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS success_patterns (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  source_task_ids TEXT NOT NULL,
  pattern_text TEXT NOT NULL,
  tags TEXT NOT NULL,
  applicable_task_types TEXT NOT NULL,
  confidence REAL NOT NULL,
  success_count INTEGER NOT NULL,
  last_used_at TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS understanding_checks (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  status TEXT NOT NULL,
  body_md TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_completions (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT 'human',
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS roadmap_commitments (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  title TEXT NOT NULL,
  intent TEXT NOT NULL,
  success_criteria TEXT NOT NULL,
  non_goals TEXT NOT NULL,
  autonomy_scope TEXT NOT NULL,
  review_gates TEXT NOT NULL,
  evidence_policy TEXT NOT NULL,
  status TEXT NOT NULL,
  accepted_by TEXT NOT NULL,
  accepted_at TEXT NOT NULL,
  closed_by TEXT NOT NULL DEFAULT '',
  closed_at TEXT NOT NULL DEFAULT '',
  closure_reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS roadmap_revisions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  proposed_commitment_id TEXT NOT NULL,
  status TEXT NOT NULL,
  body_md TEXT NOT NULL,
  source_path TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL,
  decided_by TEXT NOT NULL DEFAULT '',
  accepted_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS todos (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  title TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  acceptance_hint TEXT NOT NULL DEFAULT '',
  priority TEXT NOT NULL DEFAULT 'normal',
  source_type TEXT NOT NULL DEFAULT '',
  source_task_id TEXT NOT NULL DEFAULT '',
  roadmap_commitment_id TEXT NOT NULL DEFAULT '',
  roadmap_revision_id TEXT NOT NULL DEFAULT '',
  converted_task_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  triaged_at TEXT NOT NULL DEFAULT '',
  triage_reason TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS overdrive_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  roadmap_commitment_id TEXT NOT NULL DEFAULT '',
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  cursor_task_id TEXT NOT NULL DEFAULT '',
  max_failures INTEGER NOT NULL,
  failure_count INTEGER NOT NULL,
  summary_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS overdrive_events (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  task_id TEXT NOT NULL DEFAULT '',
  event_type TEXT NOT NULL,
  message TEXT NOT NULL,
  metadata TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

JSON_COLUMNS = {
    "tech_stack",
    "rules",
    "default_completion_criteria",
    "available_models",
    "fallback_models",
    "applied_rule_ids",
    "applied_failure_pattern_ids",
    "changed_files",
    "issues",
    "metadata",
    "source_failure_ids",
    "tags",
    "selection_score",
    "scope",
    "trigger_phrases",
    "required_behavior",
    "preflight_checks",
    "completion_evidence",
    "capabilities",
    "concerns",
    "scores",
    "required_scores",
    "source_task_ids",
    "applicable_task_types",
    "acceptance_criteria",
    "success_criteria",
    "non_goals",
    "autonomy_scope",
    "review_gates",
    "evidence_policy",
    "summary_json",
}

TABLE_JSON_COLUMNS = {
    "review_dispatches": {"args"},
}


def default_db_path() -> Path:
    env = os.environ.get("NILO_DB")
    if env:
        return Path(env)
    return Path.cwd() / ".nilo" / "nilo.db"


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def insert(self, table: str, row: dict[str, Any]) -> None:
        cols = list(row)
        placeholders = ", ".join("?" for _ in cols)
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
        self.conn.execute(sql, [self._encode(row[c]) for c in cols])
        self.conn.commit()

    def update(self, table: str, row_id: str, values: dict[str, Any]) -> None:
        parts = ", ".join(f"{key}=?" for key in values)
        args = [self._encode(value) for value in values.values()]
        args.append(row_id)
        self.conn.execute(f"UPDATE {table} SET {parts} WHERE id=?", args)
        self.conn.commit()

    def get(self, table: str, row_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
        return self._decode_row(row, table) if row else None

    def latest_for_task(self, table: str, task_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            f"SELECT * FROM {table} WHERE task_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return self._decode_row(row, table) if row else None

    def list_where(self, table: str, where: str = "1=1", args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        rows = self.conn.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY created_at DESC, rowid DESC", args).fetchall()
        return [self._decode_row(row, table) for row in rows]

    def latest_task_status_event(self, task_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT event_id, source, status, created_at FROM (
              SELECT id AS event_id, 'task' AS source, status AS status, created_at, rowid AS event_rowid, 10 AS priority FROM tasks WHERE id=?
              UNION ALL
              SELECT id AS event_id, 'understanding' AS source, status AS status, created_at, rowid AS event_rowid, 20 AS priority FROM understanding_checks WHERE task_id=?
              UNION ALL
              SELECT id AS event_id, 'instruction' AS source, 'instruction_generated' AS status, created_at, rowid AS event_rowid, 30 AS priority FROM instructions WHERE task_id=?
              UNION ALL
              SELECT id AS event_id, 'agent_report' AS source, 'agent_reported' AS status, created_at, rowid AS event_rowid, 40 AS priority FROM agent_reports WHERE task_id=?
              UNION ALL
              SELECT id AS event_id, 'review_request' AS source, CASE
                WHEN status='requested' THEN 'review_requested'
                WHEN status='reviewer_unavailable' THEN 'review_reviewer_unavailable'
                WHEN status='claimed' THEN 'review_claimed'
                WHEN status='in_progress' THEN 'review_in_progress'
                WHEN status='stale' THEN 'review_stale'
                ELSE 'review_requested'
              END AS status, updated_at AS created_at, rowid AS event_rowid, 45 AS priority FROM review_requests WHERE task_id=? AND status IN ('requested', 'reviewer_unavailable', 'claimed', 'in_progress', 'stale')
              UNION ALL
              SELECT id AS event_id, 'review_result' AS source, CASE WHEN verdict='approved' THEN 'review_approved' WHEN verdict='changes_requested' THEN 'review_changes_requested' ELSE 'review_commented' END AS status, created_at, rowid AS event_rowid, 65 AS priority FROM review_results WHERE task_id=?
              UNION ALL
              SELECT id AS event_id, 'review_finding_update' AS source, 'review_changes_requested' AS status, created_at, rowid AS event_rowid, 66 AS priority FROM review_finding_updates WHERE task_id=?
              UNION ALL
              SELECT id AS event_id, 'evidence_check' AS source, status AS status, created_at, rowid AS event_rowid, 50 AS priority FROM evidence_checks WHERE task_id=?
              UNION ALL
              SELECT id AS event_id, 'verification_run' AS source, CASE WHEN timed_out=1 THEN 'verification_timed_out' WHEN exit_code=0 THEN 'verification_passed' ELSE 'verification_failed' END AS status, created_at, rowid AS event_rowid, 55 AS priority FROM verification_runs WHERE task_id=?
              UNION ALL
              SELECT id AS event_id, 'outcome' AS source, decision AS status, created_at, rowid AS event_rowid, 60 AS priority FROM outcome_reviews WHERE task_id=?
              UNION ALL
              SELECT id AS event_id, 'completion' AS source, CASE WHEN actor='ai' THEN 'completed_by_ai' ELSE 'completed_by_user' END AS status, created_at, rowid AS event_rowid, 70 AS priority FROM task_completions WHERE task_id=?
            )
            ORDER BY created_at DESC, priority DESC, event_rowid DESC
            LIMIT 1
            """,
            (task_id, task_id, task_id, task_id, task_id, task_id, task_id, task_id, task_id, task_id, task_id),
        ).fetchone()
        return self._decode_row(row) if row else None

    def _migrate(self) -> None:
        self._ensure_column("tasks", "description", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("tasks", "acceptance_criteria", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column("tasks", "parent_task_id", "TEXT")
        self._ensure_column("tasks", "split_index", "INTEGER")
        self._ensure_column("tasks", "task_type", "TEXT NOT NULL DEFAULT 'implementation'")
        self._ensure_column("tasks", "risk_level", "TEXT NOT NULL DEFAULT 'medium'")
        self._ensure_column("tasks", "requires_understanding_check", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("tasks", "roadmap_commitment_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("tasks", "roadmap_item_id", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("tasks", "mode", "TEXT NOT NULL DEFAULT 'normal'")
        self._ensure_column("instructions", "applied_failure_pattern_ids", "TEXT NOT NULL DEFAULT '[]'")
        self._ensure_column("overdrive_runs", "summary_json", "TEXT NOT NULL DEFAULT '{}'")
        self._ensure_column("task_completions", "actor", "TEXT NOT NULL DEFAULT 'human'")
        self._ensure_column("roadmap_commitments", "closed_by", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("roadmap_commitments", "closed_at", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("roadmap_commitments", "closure_reason", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("roadmap_revisions", "source_path", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("roadmap_revisions", "decided_by", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("verification_runs", "source", "TEXT NOT NULL DEFAULT 'nilo_executed'")
        self._ensure_column("review_requests", "withdrawn_reason", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("review_requests", "withdrawn_actor", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("review_requests", "withdrawn_at", "TEXT NOT NULL DEFAULT ''")

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        if any(row["name"] == column for row in rows):
            return
        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self.conn.commit()

    @staticmethod
    def _encode(value: Any) -> Any:
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, bool):
            return int(value)
        return value

    @staticmethod
    def _decode_row(row: sqlite3.Row, table: str | None = None) -> dict[str, Any]:
        result = dict(row)
        json_columns = set(JSON_COLUMNS)
        if table:
            json_columns.update(TABLE_JSON_COLUMNS.get(table, set()))
        for key, value in list(result.items()):
            if key in json_columns and isinstance(value, str):
                try:
                    result[key] = json.loads(value)
                except json.JSONDecodeError:
                    pass
        return result
