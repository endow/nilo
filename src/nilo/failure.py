from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .timeutil import now_iso


SEVERITY_WEIGHT = {"low": 1, "medium": 2, "high": 3}
COOLING_DOWN_SUCCESS_THRESHOLD = 5
VALID_SEVERITIES = {"low", "medium", "high"}
PATTERN_SEVERITIES = {"blocking", "warning"}


SEED_FAILURE_PATTERNS = [
    {
        "id": "failure_mcp_review_cli_fallback",
        "title": "Claude review was attempted through CLI fallback",
        "severity": "blocking",
        "scope": {
            "tools": ["claude-code", "mcp", "review"],
            "intents": ["request_review", "ask_claude_to_review"],
            "files": ["mcp_server.py", "review_dispatcher.py", "review_requests"],
        },
        "trigger_phrases": [
            "Claudeでレビュー",
            "Claude Code にレビュー",
            "review with claude",
            "ask claude to review",
            "claude review",
        ],
        "failure_summary": "Agent attempted to use Claude CLI fallback instead of Nilo MCP review request.",
        "required_behavior": [
            "Use only Nilo MCP callable review tools for Claude review requests.",
            "Do not invoke claude, claude -p, or any Claude CLI fallback as a substitute.",
            "If the callable MCP review tool is unavailable, stop and report the unavailability.",
            "Keep review request creation separate from reviewer dispatch availability.",
        ],
        "preflight_checks": [
            "Confirm the callable MCP review tool is available.",
            "Confirm which code path creates the review request.",
            "Confirm no CLI fallback path will be used.",
        ],
        "completion_evidence": [
            "Created review_request id, or a clear callable-tool-unavailable result.",
            "Reviewer target resolution result.",
            "Explicit statement that no Claude CLI fallback was used.",
        ],
    },
    {
        "id": "failure_integration_before_connectivity_check",
        "title": "Integration was implemented before proving basic connectivity",
        "severity": "blocking",
        "scope": {
            "tools": ["mcp", "external-tool", "agent-bridge", "reviewer"],
            "intents": ["integrate", "connect", "dispatch", "request_review"],
            "files": ["mcp_server.py", "review_dispatcher.py", "reviewer_registry.py"],
        },
        "trigger_phrases": [
            "MCP",
            "連携",
            "疎通",
            "Claude Code",
            "reviewer",
            "dispatch",
        ],
        "failure_summary": "Agent implemented outer integration logic before proving that the minimal communication path worked.",
        "required_behavior": [
            "Prove the smallest possible connectivity path before building integration layers.",
            "Do not claim integration is complete without executable evidence.",
        ],
        "preflight_checks": [
            "Identify the minimal connectivity check.",
            "Run or document the exact check before implementation completion.",
        ],
        "completion_evidence": [
            "Exact command or callable tool used for connectivity check.",
            "Result of the check.",
            "If failed, explicit stop reason and no completion claim.",
        ],
    },
]


def failure_to_rule_text(category: str, message: str) -> tuple[str, list[str], str]:
    lowered = f"{category} {message}".lower()
    if "changed_files" in lowered or "change" in lowered or "git" in lowered:
        return "完了報告の変更ファイル一覧は、作業開始時点からのGit差分と一致させる", ["#git", "#evidence"], "high"
    if "test" in lowered or "テスト" in lowered:
        return "完了報告には実行したテストコマンドと結果ログを必ず含める", ["#testing"], "medium"
    if "lint" in lowered:
        return "完了報告にはlintの実行結果、または未実行理由を明記する", ["#lint"], "medium"
    if "type" in lowered or "型" in lowered:
        return "完了報告には型チェックの実行結果、または未実行理由を明記する", ["#typecheck"], "medium"
    return "完了報告では不足証跡と未実行項目を明示する", ["#evidence"], "medium"


def deterministic_id(prefix: str, parts: list[str]) -> str:
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def score_rule(rule: dict[str, Any], task_title: str) -> dict[str, Any]:
    text = f"{rule['rule_text']} {' '.join(rule['tags'])}".lower()
    task = task_title.lower()
    relevance = 0.5
    for token in ["test", "lint", "type", "git", "diff", "テスト", "型", "変更"]:
        if token in text and token in task:
            relevance += 0.2
    relevance = min(relevance, 1.0)
    recency = recency_score(rule.get("last_seen_at", ""))
    return {
        "relevance": relevance,
        "recency": recency,
        "severity": rule["severity"],
        "recurrence_count": rule["recurrence_count"],
        "confidence": rule["confidence"],
    }


def recency_score(value: str) -> float:
    try:
        last_seen = datetime.fromisoformat(value)
    except ValueError:
        return 0.2
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    age_days = max((datetime.now(timezone.utc) - last_seen.astimezone(timezone.utc)).days, 0)
    if age_days <= 7:
        return 1.0
    if age_days <= 30:
        return 0.7
    if age_days <= 90:
        return 0.4
    return 0.2


def score_value(score: dict[str, Any]) -> float:
    return (
        SEVERITY_WEIGHT.get(score["severity"], 1) * 10
        + float(score["relevance"]) * 8
        + int(score["recurrence_count"]) * 2
        + float(score["recency"]) * 3
        + float(score["confidence"]) * 5
    )


def select_rules(rules: list[dict[str, Any]], task_title: str, degraded: bool) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    limit = 2 if degraded else 5
    per_tag_limit = 2 if degraded else 3
    candidates: list[tuple[dict[str, Any], dict[str, Any], float]] = []
    for rule in rules:
        if rule["manually_disabled"] or rule["state"] not in ("new", "active"):
            continue
        score = score_rule(rule, task_title)
        candidates.append((rule, score, score_value(score)))

    candidates.sort(key=lambda item: item[2], reverse=True)
    selected: list[tuple[dict[str, Any], dict[str, Any]]] = []
    tag_counts: defaultdict[str, int] = defaultdict(int)
    for rule, score, _ in candidates:
        tags = rule["tags"] or ["#general"]
        if any(tag_counts[tag] >= per_tag_limit for tag in tags):
            continue
        selected.append((rule, score))
        for tag in tags:
            tag_counts[tag] += 1
        if len(selected) >= limit:
            break
    return selected


def derived_rule_from_failure(project_id: str, failure: dict[str, Any]) -> dict[str, Any]:
    text, tags, severity = failure_to_rule_text(failure["category"], failure["message"])
    created_at = now_iso()
    return {
        "id": deterministic_id("rule", [project_id, text]),
        "project_id": project_id,
        "source_failure_ids": [failure["id"]],
        "source": "fallback_structured",
        "auto_activated": True,
        "manually_disabled": False,
        "rule_text": text,
        "tags": tags,
        "severity": severity,
        "confidence": 0.4,
        "recurrence_count": 1,
        "success_count": 0,
        "last_seen_at": created_at,
        "state": "active",
        "created_at": created_at,
    }


def parse_agent_derived_rules(markdown: str) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    current: dict[str, str] | None = None
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.lower() == "## rule":
            if current is not None:
                rules.append(current)
            current = {}
            continue
        if current is None or ":" not in line:
            continue
        key, value = line.split(":", 1)
        current[key.strip().lower()] = value.strip()
    if current is not None:
        rules.append(current)
    if not rules:
        raise ValueError("no Rule blocks found")
    return [normalize_agent_rule(rule) for rule in rules]


def normalize_agent_rule(rule: dict[str, str]) -> dict[str, Any]:
    source_failures = split_csv(rule.get("source_failures", ""))
    rule_text = rule.get("rule", "").strip()
    tags = split_csv(rule.get("tags", ""))
    severity = rule.get("severity", "").strip().lower()
    raw_confidence = rule.get("confidence", "").strip()
    if not source_failures:
        raise ValueError("source_failures is required")
    if not rule_text:
        raise ValueError("rule is required")
    if len(rule_text) > 200:
        raise ValueError("rule must be 200 characters or fewer")
    if not tags:
        raise ValueError("tags is required")
    invalid_tags = [tag for tag in tags if not tag.startswith("#")]
    if invalid_tags:
        raise ValueError(f"tags must start with #: {', '.join(invalid_tags)}")
    if severity not in VALID_SEVERITIES:
        raise ValueError("severity must be low, medium, or high")
    try:
        confidence = float(raw_confidence)
    except ValueError:
        raise ValueError("confidence must be a number") from None
    if confidence < 0.1 or confidence > 1.0:
        raise ValueError("confidence must be between 0.1 and 1.0")
    return {
        "source_failure_ids": source_failures,
        "rule_text": rule_text,
        "tags": tags,
        "severity": severity,
        "confidence": confidence,
    }


def split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def seed_failure_patterns(store) -> None:
    created_at = now_iso()
    for pattern in SEED_FAILURE_PATTERNS:
        if store.get("failure_patterns", pattern["id"]):
            continue
        store.insert("failure_patterns", {**pattern, "created_at": created_at})


def active_failure_patterns(store) -> list[dict[str, Any]]:
    seed_failure_patterns(store)
    return list(reversed(store.list_where("failure_patterns")))


def task_search_text(task: dict[str, Any]) -> str:
    acceptance = " ".join(task.get("acceptance_criteria") or [])
    return " ".join(
        [
            task.get("title", ""),
            task.get("description", ""),
            acceptance,
            task.get("task_type", ""),
        ]
    )


def match_failure_patterns(patterns: list[dict[str, Any]], task: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    text = task_search_text(task)
    folded = text.casefold()
    matches: list[tuple[dict[str, Any], str]] = []
    for pattern in patterns:
        trigger = first_matching_trigger(pattern, text, folded)
        if trigger:
            matches.append((pattern, f"trigger phrase: {trigger}"))
            continue
        heuristic = heuristic_match_reason(pattern["id"], folded)
        if heuristic:
            matches.append((pattern, heuristic))
    return matches


def first_matching_trigger(pattern: dict[str, Any], text: str, folded: str) -> str:
    for phrase in pattern.get("trigger_phrases", []):
        if not phrase:
            continue
        if phrase in text or phrase.casefold() in folded:
            return phrase
    return ""


def heuristic_match_reason(pattern_id: str, folded: str) -> str:
    if pattern_id == "failure_mcp_review_cli_fallback":
        wants_claude = "claude" in folded or "claude code" in folded
        wants_review = "review" in folded or "レビュー" in folded
        mentions_mcp = "mcp" in folded
        if wants_claude and wants_review and mentions_mcp:
            return "heuristic: Claude MCP review request"
        if wants_claude and wants_review:
            return "heuristic: Claude review request"
    if pattern_id == "failure_integration_before_connectivity_check":
        integration_terms = ["mcp", "連携", "connect", "integrate", "integration", "dispatch", "reviewer", "external"]
        if any(term in folded for term in integration_terms):
            return "heuristic: external integration or connectivity work"
    return ""


def refresh_task_failure_pattern_matches(store, task: dict[str, Any]) -> list[dict[str, Any]]:
    seed_failure_patterns(store)
    patterns = active_failure_patterns(store)
    matches = match_failure_patterns(patterns, task)
    existing = {
        row["failure_pattern_id"]: row
        for row in store.list_where("task_failure_pattern_matches", "task_id=?", (task["id"],))
    }
    created_at = now_iso()
    rows: list[dict[str, Any]] = []
    for pattern, reason in matches:
        row = existing.get(pattern["id"])
        if row:
            rows.append({**pattern, "match_reason": row["match_reason"]})
            continue
        store.insert(
            "task_failure_pattern_matches",
            {
                "id": deterministic_id("failure_match", [task["id"], pattern["id"]]),
                "task_id": task["id"],
                "failure_pattern_id": pattern["id"],
                "match_reason": reason,
                "created_at": created_at,
            },
        )
        rows.append({**pattern, "match_reason": reason})
    return rows


def matched_failure_patterns_for_task(store, task_id: str) -> list[dict[str, Any]]:
    seed_failure_patterns(store)
    rows = store.list_where("task_failure_pattern_matches", "task_id=?", (task_id,))
    patterns: list[dict[str, Any]] = []
    for row in reversed(rows):
        pattern = store.get("failure_patterns", row["failure_pattern_id"])
        if pattern:
            patterns.append({**pattern, "match_reason": row["match_reason"]})
    return patterns


def render_recurrence_prevention(patterns: list[dict[str, Any]]) -> str:
    if not patterns:
        return ""
    lines = ["## Recurrence prevention", ""]
    for pattern in patterns:
        lines.append(f"This task matches previous failure pattern: {pattern['id']}.")
        lines.append("")
        lines.append(f"Severity: {pattern['severity']}")
        lines.append(f"Previous failure: {pattern['failure_summary']}")
        lines.append("")
        lines.append("Required behavior:")
        lines.extend(f"- {item}" for item in pattern["required_behavior"])
        lines.append("")
        lines.append("Preflight checks:")
        lines.extend(f"- {item}" for item in pattern["preflight_checks"])
        lines.append("")
        lines.append("Completion evidence required:")
        lines.extend(f"- {item}" for item in pattern["completion_evidence"])
        lines.append("")
    return "\n".join(lines).rstrip()


def recurrence_prevention_summary_lines(patterns: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for pattern in patterns:
        first_check = pattern["preflight_checks"][0] if pattern["preflight_checks"] else "follow required behavior"
        lines.append(f"- {pattern['id']}: {pattern['severity']}")
        lines.append(f"  Previous failure: {pattern['failure_summary']}")
        lines.append(f"  Required next check: {first_check}")
    return lines


def recurrence_evidence_issues(markdown: str, patterns: list[dict[str, Any]]) -> list[str]:
    folded = markdown.casefold()
    issues: list[str] = []
    for pattern in patterns:
        if pattern["severity"] != "blocking":
            continue
        missing = missing_evidence_for_pattern(pattern["id"], folded)
        for item in missing:
            issues.append(f"recurrence prevention missing evidence ({pattern['id']}): {item}")
    return issues


def unresolved_recurrence_completion_issues(store, task_id: str) -> list[str]:
    patterns = [pattern for pattern in matched_failure_patterns_for_task(store, task_id) if pattern["severity"] == "blocking"]
    if not patterns:
        return []
    check = store.latest_for_task("evidence_checks", task_id)
    if check and check["status"] == "evidence_submitted":
        return []
    if check:
        issues = [
            issue
            for issue in check["issues"]
            if issue.startswith("recurrence prevention missing evidence")
        ]
        if issues:
            return issues
    pattern_ids = ", ".join(pattern["id"] for pattern in patterns)
    return [f"recurrence prevention evidence_check=evidence_submitted required for blocking patterns: {pattern_ids}"]


def missing_evidence_for_pattern(pattern_id: str, folded_markdown: str) -> list[str]:
    if pattern_id == "failure_mcp_review_cli_fallback":
        missing = []
        has_request_or_unavailable = (
            re.search(r"\breview_request\b.*\b(id|review_[a-z0-9_]+)", folded_markdown, re.DOTALL) is not None
            or "callable-tool-unavailable" in folded_markdown
            or "callable tool unavailable" in folded_markdown
            or "mcp callable tool unavailable" in folded_markdown
            or "callable mcp tool unavailable" in folded_markdown
            or "利用不可" in folded_markdown
        )
        if not has_request_or_unavailable:
            missing.append("review_request id or callable-tool-unavailable result")
        if "reviewer target resolution" not in folded_markdown and "target resolution" not in folded_markdown:
            missing.append("reviewer target resolution result")
        if (
            "no claude cli fallback was used" not in folded_markdown
            and "no cli fallback was used" not in folded_markdown
            and "cli fallback 未使用" not in folded_markdown
            and "claude cli fallback 未使用" not in folded_markdown
        ):
            missing.append("explicit confirmation that no Claude CLI fallback was used")
        return missing
    if pattern_id == "failure_integration_before_connectivity_check":
        missing = []
        check_terms = [
            "connectivity check",
            "疎通確認",
            "minimal communication",
            "callable tool used",
        ]
        has_check = (
            "connectivity check" in folded_markdown
            or "疎通確認" in folded_markdown
            or "minimal communication" in folded_markdown
            or "callable tool used" in folded_markdown
        )
        if not has_check:
            missing.append("exact command or callable tool used for connectivity check")
        if not connectivity_check_has_result(folded_markdown, check_terms):
            missing.append("result of the connectivity check")
        return missing
    return []


def connectivity_check_has_result(folded_markdown: str, check_terms: list[str]) -> bool:
    result_terms = ["result", "結果", "exit_code", "passed", "failed", "成功", "失敗"]
    for check_term in check_terms:
        if check_term not in folded_markdown:
            continue
        for result_term in result_terms:
            forward = rf"{re.escape(check_term)}.{{0,240}}{re.escape(result_term)}"
            backward = rf"{re.escape(result_term)}.{{0,240}}{re.escape(check_term)}"
            if re.search(forward, folded_markdown, re.DOTALL) or re.search(backward, folded_markdown, re.DOTALL):
                return True
    return False


def derived_rule_from_agent(project_id: str, candidate: dict[str, Any], created_at: str | None = None) -> dict[str, Any]:
    timestamp = created_at or now_iso()
    return {
        "id": deterministic_id("rule", [project_id, candidate["rule_text"]]),
        "project_id": project_id,
        "source_failure_ids": candidate["source_failure_ids"],
        "source": "agent_import",
        "auto_activated": True,
        "manually_disabled": False,
        "rule_text": candidate["rule_text"],
        "tags": candidate["tags"],
        "severity": candidate["severity"],
        "confidence": candidate["confidence"],
        "recurrence_count": len(candidate["source_failure_ids"]),
        "success_count": 0,
        "last_seen_at": timestamp,
        "state": "active",
        "created_at": timestamp,
    }


def rule_success_update(rule: dict[str, Any]) -> dict[str, Any]:
    success_count = int(rule["success_count"]) + 1
    confidence = max(float(rule["confidence"]) - 0.08, 0.1)
    state = "cooling_down" if success_count >= COOLING_DOWN_SUCCESS_THRESHOLD else rule["state"]
    return {
        "success_count": success_count,
        "confidence": confidence,
        "state": state,
    }
