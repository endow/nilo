from __future__ import annotations

import re


PLACEHOLDER_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"^\[ここに記述\]$",
        r"^\[未記入\]$",
        r"^\[TODO\]$",
        r"^TODO$",
        r"^TBD$",
        r"^N/A$",
        r"^未記入$",
        r"^ここに記述$",
    ]
]

REQUIRED_SECTIONS = [
    ("1. 実施内容", ["実施内容"]),
    ("2. 変更ファイル一覧", ["変更ファイル一覧"]),
    ("3. テストコマンド", ["テストコマンド"]),
    ("3. テスト結果", ["テスト結果"]),
    ("3. 型チェック", ["型チェック"]),
    ("3. lint", ["lint"]),
    ("4. 未実行の検証", ["未実行の検証"]),
    ("5. 既知の問題", ["既知の問題", "仕様から外れた判断"]),
    ("6. 人間に確認してほしい点", ["人間に確認してほしい点"]),
]

EXTENSIONLESS_FILE_NAMES = {"Makefile", "Dockerfile", "Containerfile", "LICENSE", "NOTICE", "README"}


def parse_sections(markdown: str) -> dict[str, str]:
    matches = list(re.finditer(r"^#{2,3}\s+(.+?)\s*$", markdown, flags=re.MULTILINE))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        sections[title] = markdown[start:end].strip()
    return sections


def section_value(sections: dict[str, str], candidates: list[str]) -> str:
    for title, body in sections.items():
        if any(candidate.lower() in title.lower() for candidate in candidates):
            return body.strip()
    return ""


def is_placeholder(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return True
    meaningful_lines = [
        line.strip().lstrip("-*").strip()
        for line in stripped.splitlines()
        if line.strip()
    ]
    return bool(meaningful_lines) and all(
        any(pattern.match(line) for pattern in PLACEHOLDER_PATTERNS)
        for line in meaningful_lines
    )


def is_probable_path(value: str) -> bool:
    normalized = normalize_reported_path(value)
    if normalized is None:
        return False
    return _is_probable_normalized_path(normalized)


def normalize_reported_path(value: str) -> str | None:
    normalized = value.strip().strip("\"'").replace("\\", "/")
    if not normalized or re.search(r"[\r\n]", normalized):
        return None
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:/", normalized)
        or any(part in ("", ".", "..") for part in normalized.split("/"))
    ):
        return None
    return normalized


def _is_probable_normalized_path(normalized: str) -> bool:
    has_whitespace = bool(re.search(r"\s", normalized))
    if "/" in normalized:
        return _has_pathlike_leaf(normalized) if has_whitespace else True
    if re.match(r"^\.[A-Za-z0-9][A-Za-z0-9_.-]*$", normalized):
        return True
    if normalized in EXTENSIONLESS_FILE_NAMES:
        return True
    return bool(re.match(r"^[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+$", normalized))


def _has_pathlike_leaf(value: str) -> bool:
    leaf = value.rsplit("/", 1)[-1]
    return bool(
        re.search(r"\.[A-Za-z0-9_.-]+$", leaf)
        or leaf in EXTENSIONLESS_FILE_NAMES
    )


def extract_changed_files(markdown: str) -> list[str]:
    sections = parse_sections(markdown)
    body = section_value(sections, ["変更ファイル一覧"])
    if declares_no_changed_files(body):
        return []
    files: list[str] = []
    for line in body.splitlines():
        value = line.strip().lstrip("-*").strip()
        value = value.strip("`").strip("\"'")
        if not value or is_placeholder(value):
            continue
        normalized = normalize_reported_path(value)
        if normalized is not None and _is_probable_normalized_path(normalized):
            files.append(normalized)
    return sorted(set(files))


def declares_no_changed_files(value: str) -> bool:
    normalized_lines = [
        line.strip().lstrip("-*").strip().strip("`").strip("\"'")
        for line in value.splitlines()
        if line.strip()
    ]
    return any(line in {"変更ファイルなし", "なし", "none", "no changed files"} for line in normalized_lines)


def claimed_status(markdown: str) -> str:
    for line in markdown.splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = re.match(r"^\s*(?:claimed_status|status|ステータス|状態)\s*[:：]\s*(.+?)\s*$", line, flags=re.IGNORECASE)
        if not match:
            continue
        value = match.group(1).lower()
        if re.search(r"\b(completed|done)\b|完了", value, flags=re.IGNORECASE):
            return "completed"
    return "reported"


def validate_report_shape(markdown: str) -> list[str]:
    sections = parse_sections(markdown)
    issues: list[str] = []
    for label, candidates in REQUIRED_SECTIONS:
        value = section_value(sections, candidates)
        if not value:
            issues.append(f"missing section body: {label}")
        elif is_placeholder(value):
            issues.append(f"placeholder section body: {label}")
    return issues
