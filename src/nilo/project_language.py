from __future__ import annotations

import locale
import re
from pathlib import Path


PRIMARY_LANGUAGE_RULE_PREFIX = "primary_language:"
SUPPORTED_PRIMARY_LANGUAGES = {"ja", "en"}
JAPANESE_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
TECHNICAL_TOKEN_RE = re.compile(
    r"(`[^`]+`|[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+|[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+|--?[A-Za-z0-9_-]+)"
)
FOCUSED_EVIDENCE_POLICY = (
    "Record targeted verification for the changed module or focused test group first; "
    "use full verification only for release, broad-risk, or shared-core changes; "
    "if full verification is skipped, document the scope reason instead of treating the skip as a failure."
)
FOCUSED_EVIDENCE_POLICY_JA = (
    "まず変更モジュールまたは focused test group の targeted verification を記録する。"
    "release、広範囲 risk、shared-core 変更の場合だけ full verification を使う。"
    "full verification を省略する場合は、失敗扱いにせず scope reason を記録する。"
)


def normalize_primary_language(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    if normalized.startswith("ja"):
        return "ja"
    if normalized.startswith("en"):
        return "en"
    return normalized


def primary_language_rule(language: str) -> str:
    normalized = normalize_primary_language(language)
    if normalized not in SUPPORTED_PRIMARY_LANGUAGES:
        raise ValueError(f"unsupported primary_language: {language}")
    return f"{PRIMARY_LANGUAGE_RULE_PREFIX} {normalized}"


def rule_primary_language(rules: list[str]) -> str:
    for rule in rules:
        key, separator, value = rule.partition(":")
        if separator and key.strip().lower().replace("-", "_") == "primary_language":
            language = normalize_primary_language(value)
            if language in SUPPORTED_PRIMARY_LANGUAGES:
                return language
    return ""


def infer_primary_language_from_text(text: str) -> str:
    return "ja" if JAPANESE_RE.search(text) else "en"


def infer_primary_language_from_files(root: Path | None = None) -> str:
    root = root or Path.cwd()
    for name in ("README.md", "AGENTS.md", "CLAUDE.md"):
        path = root / name
        if path.exists() and path.is_file():
            try:
                body = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if body.strip():
                return infer_primary_language_from_text(body)
    system_language = (locale.getlocale()[0] or "").replace("-", "_").lower()
    return "ja" if system_language.startswith("ja") or "japanese" in system_language else "en"


def project_primary_language(project: dict, root: Path | None = None) -> str:
    language = rule_primary_language(project.get("rules") or [])
    if language:
        return language
    haystack = "\n".join([project.get("name") or "", *(project.get("rules") or [])])
    if JAPANESE_RE.search(haystack):
        return "ja"
    return infer_primary_language_from_files(root)


def ensure_primary_language_rule(rules: list[str], language: str) -> list[str]:
    if rule_primary_language(rules):
        return list(rules)
    return [*rules, primary_language_rule(language)]


def human_readable_language_policy(project: dict, root: Path | None = None) -> str:
    language = project_primary_language(project, root)
    if language == "ja":
        return (
            "Niloへ保存する人間可読フィールドは project primary_language=ja で書く。"
            "CLI から渡された文面は自動翻訳しない。command、path、file name、identifier、"
            "status、enum、JSON field name は元の技術表記を維持する。"
        )
    return (
        f"Human-readable fields saved to Nilo must use project primary_language={language}. "
        "Do not translate CLI-provided text automatically; keep commands, paths, file names, identifiers, "
        "status, enum, and JSON field names in their original technical form."
    )


def human_gate_texts(project: dict, root: Path | None = None) -> dict[str, str]:
    if project_primary_language(project, root) == "ja":
        return {
            "public_operation_required": "公開操作が必要な場合",
            "destructive_change_required": "破壊的変更が必要な場合",
            "verification_fails": "検証が失敗した場合",
            "human_acceptance_required": "人間による承認が必要な場合",
            "high_risk_completion_required": "高リスクのタスクを完了するには、人間による明示的な判断が必要です",
            "human_only": "この操作は人間の判断を記録するため、AIは実行できません",
            "human_confirm_required": "人間の判断には human_confirm=True が必要です",
            "decision_note_required": "人間の判断には判断理由が必要です",
            "decision_source_required": "人間の判断には decision_source=human_interactive または human_explicit が必要です",
            "ai_human_decision_forbidden": "AIは人間の判断を記録できません",
            "human_completion_note_required": "人間による完了には、人間の承認内容を --decision-note で指定する必要があります",
            "accepted_risk_human_required": "人間の判断が必要です: accepted-risk は人間が記録する必要があります",
            "accepted_risk_confirmation_required": "人間による確認が必要です: accepted-risk には --human-confirm と判断理由が必要です",
            "next": "次の操作:",
            "have_human_run": "人間が実行してください",
            "todo_close_decision_required": "Todoを終了するには、人間による確認または後続項目へのリンクが必要です",
        }
    return {
        "public_operation_required": "public operation is required",
        "destructive_change_required": "destructive change is required",
        "verification_fails": "verification fails",
        "human_acceptance_required": "human acceptance is required",
        "high_risk_completion_required": "high-risk task completion requires an explicit human decision",
        "human_only": "this transition records a human decision and cannot be performed by AI",
        "human_confirm_required": "human decision requires human_confirm=True",
        "decision_note_required": "human decision requires a decision note",
        "decision_source_required": "human decision requires decision_source=human_interactive or human_explicit",
        "ai_human_decision_forbidden": "AI cannot create a human decision",
        "human_completion_note_required": "human completion requires --decision-note with the human acceptance note",
        "accepted_risk_human_required": "human decision required: accepted-risk must be recorded by a human",
        "accepted_risk_confirmation_required": "human confirmation required: accepted-risk needs --human-confirm and a decision note",
        "next": "next:",
        "have_human_run": "have a human run",
        "todo_close_decision_required": "closing a todo requires human confirmation or a linked successor",
    }


def _nontechnical_text(value: str) -> str:
    return TECHNICAL_TOKEN_RE.sub(" ", value)


def human_readable_language_issues(language: str, fields: dict[str, str | list[str]]) -> list[str]:
    normalized = normalize_primary_language(language)
    issues: list[str] = []
    for name, raw_value in fields.items():
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        for value in values:
            text = str(value or "").strip()
            if not text:
                continue
            human_text = _nontechnical_text(text)
            if not human_text.strip():
                continue
            if normalized == "ja" and not JAPANESE_RE.search(human_text):
                issues.append(f"{name} must use project primary_language=ja")
            if normalized == "en" and JAPANESE_RE.search(human_text):
                issues.append(f"{name} must use project primary_language=en")
    return issues


def roadmap_proposal_texts(language: str) -> dict[str, str]:
    normalized = normalize_primary_language(language)
    if normalized == "ja":
        return {
            "default_success": "自律実行前に、人間が成功条件を定義している。",
            "non_goal": "この提案だけでは roadmap commitment を承認または close しない。",
            "autonomy_scope": "この提案が承認された後にだけ、具体的な task を作成する。",
            "review_gate": "implementation task を作成する前に人間の acceptance が必要。",
            "evidence_policy": FOCUSED_EVIDENCE_POLICY_JA,
        }
    return {
        "default_success": "Human-defined success criteria are required before autonomous execution.",
        "non_goal": "This proposal does not accept or close the roadmap commitment.",
        "autonomy_scope": "Create concrete tasks only after this proposal is accepted.",
        "review_gate": "Human acceptance is required before implementation tasks are created.",
        "evidence_policy": FOCUSED_EVIDENCE_POLICY,
    }


def render_roadmap_proposal_from_todo(title: str, description: str, acceptance_hint: str, language: str) -> str:
    texts = roadmap_proposal_texts(language)
    acceptance = acceptance_hint or texts["default_success"]
    return "\n".join(
        [
            f"# {title}",
            "",
            "## Intent",
            description or title,
            "",
            "## Success Criteria",
            f"- {acceptance}",
            "",
            "## Non Goals",
            f"- {texts['non_goal']}",
            "",
            "## Autonomy Scope",
            f"- {texts['autonomy_scope']}",
            "",
            "## Review Gates",
            f"- {texts['review_gate']}",
            "",
            "## Evidence Policy",
            f"- {texts['evidence_policy']}",
            "",
        ]
    )
