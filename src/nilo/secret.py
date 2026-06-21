from __future__ import annotations

import re


SECRET_PATTERNS = [
    ("openai_api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----")),
    ("env_secret_assignment", re.compile(r"(?im)^\s*(?:[A-Z0-9_]*SECRET|[A-Z0-9_]*TOKEN|[A-Z0-9_]*PASSWORD|DATABASE_URL)\s*=\s*.+$")),
]


def detect_secret_issues(text: str) -> list[str]:
    issues: list[str] = []
    for name, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            issues.append(f"secret detected: {name}")
    return issues


def mask_secrets(text: str) -> str:
    masked = text
    for name, pattern in SECRET_PATTERNS:
        masked = pattern.sub(f"[MASKED:{name}]", masked)
    return masked
