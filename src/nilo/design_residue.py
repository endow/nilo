from __future__ import annotations

import hashlib
import re
from pathlib import Path


IMPLEMENTED_FOLLOWUPS = {
    "quality quick summary/issue manual recording is implemented": ("quality_quick_summary_issue", "resolved"),
    "quality quick optional --score key=value structured score storage is implemented": ("quality_quick_score_storage", "resolved"),
    "quality quick required score schema is implemented": ("quality_quick_required_score_schema", "resolved"),
    "quality quick interactive review is implemented": ("quality_quick_interactive_review", "resolved"),
    "quality quick review AI integration is implemented": ("quality_quick_review_ai_integration", "resolved"),
    "quality autoscore prepare/import agent workflow is implemented": ("quality_autoscore_agent_workflow", "resolved"),
    "review prepare review-only prompt and review import QualityReview storage are implemented": ("review_prepare_import_storage", "resolved"),
    "review import parses Summary / Issues / Scores headings into QualityReview": ("review_import_structured_parse", "resolved"),
    "review import advanced natural language parsing is implemented": ("review_import_advanced_natural_language_parse", "resolved"),
    "review import score validity evaluation is implemented": ("review_import_score_validity_evaluation", "resolved"),
    "rules derive prepare/import agent workflow is implemented": ("rules_derive_agent_workflow", "resolved"),
    "project status の最小実装を追加する": ("project_status_minimal", "resolved"),
    "project summary のテキスト出力を追加する": ("project_summary_text", "resolved"),
    "project summary --format json を追加する": ("project_summary_json", "resolved"),
    "task / verification / report / outcome の履歴を recent_history に統合する": ("recent_history_stream", "resolved"),
    "task.base_commit と git log から commit_mapping を生成する": ("commit_mapping_git_log", "resolved"),
    "design_residue の初期ヒューリスティックを実装する": ("design_residue_heuristic", "resolved"),
    "project export-handson で handson 互換出力を生成する": ("export_handson_handoff", "resolved"),
    "AGENTS.md / CLAUDE.md の管理ブロックを project status 起点へ更新する": ("agent_install_project_status", "resolved"),
}

OPEN_MARKERS = ("未実装", "未確定", "後続で扱う")
RESIDUE_REWRITES = {
    "Phase 1.5 の最小実装では、`quality quick` により summary と issue の手動記録を先に実装する。スコア入力、対話レビュー、レビューAI連携は後続で扱う。": [
        ("quality quick summary/issue manual recording is implemented", "resolved"),
        ("quality quick interactive review is implemented", "resolved"),
        ("quality quick review AI integration is implemented", "resolved"),
    ],
    "`quality quick` は任意で `--score key=value` を受け取り、1〜5の範囲で構造化スコアを保存できる。`quality schema set` によりプロジェクト単位の必須スコア項目を保存でき、`quality quick --strict-scores` はその schema を使って不足スコアを検出する。": [
        ("quality quick optional --score key=value structured score storage is implemented", "resolved"),
        ("quality quick required score schema is implemented", "resolved"),
    ],
    "後続実装では、たとえば `nilo quality autoscore prepare --task task_001` で Codex / Claude Code に渡す採点指示を生成し、AI が返した `Summary` / `Scores` / `Rationale` を `nilo quality autoscore import --task task_001 --file autoscore.md` で取り込む。import 時には、score が 1〜5 の範囲にあること、required score schema を満たすこと、不明な score key を扱う方針に反していないことを検証する。quality autoscore prepare/import agent workflow は後続で扱う。": [
        ("quality quick optional --score key=value structured score storage is implemented", "resolved"),
        ("quality quick required score schema is implemented", "resolved"),
        ("quality autoscore prepare/import agent workflow is implemented", "resolved"),
    ],
    "Phase 1.5 の最小実装では、`review prepare` はコード変更禁止のレビュー指示書を出力し、`review import` はレビュー結果を `QualityReview` として保存する。スコア抽出や構造化パースは後続で扱う。": [
        ("review prepare review-only prompt and review import QualityReview storage are implemented", "resolved"),
    ],
    "Phase 1.5 の最小実装では、`review import` が `Summary` / `Issues` / `Scores` の見出しを簡易パースし、`QualityReview.summary`、`QualityReview.issues`、`QualityReview.scores` に保存する。高度な自然言語解析やスコア妥当性評価は後続で扱う。": [
        ("review import parses Summary / Issues / Scores headings into QualityReview", "resolved"),
        ("review import advanced natural language parsing is implemented", "resolved"),
        ("review import score validity evaluation is implemented", "resolved"),
    ],
    "FailureLog → DerivedRule 変換は、現行MVPでは `fallback_structured` のキーワードベース変換に加え、AIエージェントを使った `rules derive prepare/import` を実装済みとする。これは以前は後続で扱う項目だったものを解消したものであり、Nilo 本体がAI API呼び出しやAPI key管理を中核責務として持たない原則は維持する。失敗履歴自体は保存し、再変換可能な形を維持する。": [
        ("rules derive prepare/import agent workflow is implemented", "resolved"),
    ],
}


def stable_key(text: str) -> str:
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    return f"design_residue_{digest}"


def display_path(path: Path) -> str:
    try:
        return path.relative_to(Path.cwd()).as_posix()
    except ValueError:
        return path.as_posix()


def source_from_heading(path: Path, heading: str | None, line_number: int) -> str:
    shown_path = display_path(path)
    if heading:
        match = re.match(r"#+\s+(\d+(?:\.\d+)*)\b", heading)
        if match:
            return f"{shown_path} {match.group(1)}"
    return f"{shown_path}:{line_number}"


def suggested_task_type(text: str) -> str:
    if "設計" in text or "未確定" in text:
        return "design"
    if "レビュー" in text or "review" in text.lower():
        return "review"
    if "検証" in text or "verification" in text.lower():
        return "verification"
    return "implementation"


def residue_item(path: Path, heading: str | None, line_number: int, text: str, status: str = "open") -> dict:
    key, mapped_status = IMPLEMENTED_FOLLOWUPS.get(text, (stable_key(text), status))
    return {
        "source": source_from_heading(path, heading, line_number),
        "key": key,
        "status": mapped_status,
        "summary": text,
        "suggested_task_type": suggested_task_type(text),
    }


def normalize_bullet(line: str) -> str | None:
    match = re.match(r"^\s*[-*]\s+(.+?)\s*$", line)
    if not match:
        return None
    return match.group(1).strip()


def parse_design_residue(path: Path) -> list[dict]:
    if not path.exists():
        return []

    items: list[dict] = []
    seen: set[str] = set()
    heading: str | None = None
    in_code_block = False
    in_followup_candidates = False

    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped
            in_followup_candidates = False
            continue
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if stripped in ("後続実装タスク候補：", "後続実装タスク候補:"):
            in_followup_candidates = True
            continue

        bullet = normalize_bullet(line)
        candidate: str | None = None
        if in_followup_candidates and bullet:
            candidate = bullet
        elif (
            not in_code_block
            and not stripped.startswith("`design_residue` は")
            and any(marker in stripped for marker in OPEN_MARKERS)
        ):
            candidate = bullet or stripped

        if not candidate:
            continue
        rewritten = RESIDUE_REWRITES.get(candidate, [(candidate, "open")])
        for summary, status in rewritten:
            if summary in seen:
                continue
            seen.add(summary)
            items.append(residue_item(path, heading, line_number, summary, status))

    return items
