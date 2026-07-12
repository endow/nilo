from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .review_coordinator import ErrorClass


@dataclass(frozen=True)
class ClassifiedProviderError:
    error_class: ErrorClass
    error_code: str
    retry_after: str
    message: str


RATE_LIMIT_PATTERNS = (
    r"\brate[ _-]?limit(?:ed)?\b",
    r"\btoo many requests\b",
    r"\busage limit\b",
    r"\bstatus(?: code)?[:= ]+429\b",
    r"\bhttp[/ ]?429\b",
)
QUOTA_PATTERNS = (
    r"\binsufficient[_ -]?quota\b",
    r"\bquota (?:is )?(?:exceeded|exhausted)\b",
    r"\bcredit balance\b",
    r"\bcredits? (?:exhausted|depleted)\b",
)
AUTH_PATTERNS = (
    r"\binvalid api key\b",
    r"\bunauthori[sz]ed\b",
    r"\bauthentication (?:failed|error)\b",
    r"\bstatus(?: code)?[:= ]+40[13]\b",
)
RETRY_AFTER_PATTERN = re.compile(r"retry[-_ ]after[\s:=\"]+([^\s,\"}]+)", re.IGNORECASE)


def _flatten_payload(value: str) -> tuple[str, str, str]:
    stripped = value.strip()
    if not stripped:
        return "", "", ""
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped, "", ""
    if not isinstance(payload, dict):
        return stripped, "", ""
    error = payload.get("error", payload)
    if isinstance(error, dict):
        code = str(error.get("code") or error.get("type") or payload.get("code") or "")
        message = str(error.get("message") or error)
        retry_after = str(error.get("retry_after") or payload.get("retry_after") or "")
        return " ".join((code, message, retry_after)), code, retry_after
    return str(error), str(payload.get("code") or ""), str(payload.get("retry_after") or "")


def _matches(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def classify_provider_error(
    reviewer: str,
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int | None = None,
) -> ClassifiedProviderError | None:
    stdout_text, stdout_code, stdout_retry_after = _flatten_payload(stdout)
    stderr_text, stderr_code, stderr_retry_after = _flatten_payload(stderr)
    combined = " ".join((reviewer, stdout_text, stderr_text)).strip()
    error_code = stdout_code or stderr_code or (str(exit_code) if exit_code not in {None, 0} else "")
    retry_match = RETRY_AFTER_PATTERN.search(combined)
    retry_after = stdout_retry_after or stderr_retry_after or (retry_match.group(1) if retry_match else "")
    if _matches(QUOTA_PATTERNS, combined):
        return ClassifiedProviderError(ErrorClass.QUOTA_EXHAUSTED, error_code, retry_after, "provider quota exhausted")
    if _matches(RATE_LIMIT_PATTERNS, combined):
        return ClassifiedProviderError(ErrorClass.RATE_LIMITED, error_code, retry_after, "provider rate limited")
    if _matches(AUTH_PATTERNS, combined):
        return ClassifiedProviderError(ErrorClass.AUTHENTICATION, error_code, retry_after, "provider authentication failed")
    return None
