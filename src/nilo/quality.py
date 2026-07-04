from __future__ import annotations

import re

from .markdown_parse import parse_labeled_value
from .report import parse_sections, section_value


SCORE_LINE_RE = re.compile(r"^\s*[-*]?\s*([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(\S+)\s*$")
INLINE_SCORE_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*([1-5])\b")


def parse_quality_review(markdown: str) -> tuple[str, list[str], dict[str, int]]:
    sections = parse_sections(markdown)
    summary = section_value(sections, ["Summary", "概要", "summary"]) or parse_labeled_value(
        markdown, ["summary", "概要", "要約"]
    )
    scores_body = section_value(sections, ["Scores", "スコア", "scores"])
    issues_body = section_value(sections, ["Issues", "指摘", "issues", "Findings", "問題"])
    issues = parse_bullets(issues_body)
    if not issues:
        issues = parse_labeled_list(markdown, ["issue", "issues", "finding", "concern", "指摘", "問題", "懸念"])
    scores = parse_scores_text(scores_body, strict_invalid=True) if scores_body else parse_scores_text(markdown, strict_invalid=False)
    if not summary:
        summary = markdown.strip()
    return summary.strip(), issues, scores


def parse_bullets(text: str) -> list[str]:
    values: list[str] = []
    for line in text.splitlines():
        value = line.strip().lstrip("-*").strip()
        if value:
            values.append(value)
    return values


def parse_scores_text(text: str, strict_invalid: bool = True) -> dict[str, int]:
    scores: dict[str, int] = {}
    for line in text.splitlines():
        match = SCORE_LINE_RE.match(line)
        if match:
            raw_score = match.group(2).strip()
            if raw_score.isdigit():
                score = int(raw_score)
                if 1 <= score <= 5:
                    scores[match.group(1)] = score
                else:
                    if strict_invalid:
                        raise ValueError(f"score must be 1-5: {match.group(1)}={raw_score}")
                continue
        for inline in INLINE_SCORE_RE.finditer(line):
            scores[inline.group(1)] = int(inline.group(2))
    return scores


def parse_labeled_list(markdown: str, labels: list[str]) -> list[str]:
    label_pattern = "|".join(re.escape(label) for label in labels)
    pattern = re.compile(rf"^\s*(?:[-*]\s*)?(?:{label_pattern})\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
    values: list[str] = []
    for line in markdown.splitlines():
        match = pattern.match(line)
        if match:
            value = match.group(1).strip()
            if value:
                values.append(value)
    return values
