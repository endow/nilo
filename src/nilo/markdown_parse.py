from __future__ import annotations

import re


def parse_labeled_value(markdown: str, labels: list[str]) -> str:
    label_pattern = "|".join(re.escape(label) for label in labels)
    pattern = re.compile(rf"^\s*(?:{label_pattern})\s*[:：]\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
    match = pattern.search(markdown)
    return match.group(1).strip() if match else ""
