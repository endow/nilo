from __future__ import annotations

import unittest

from nilo.review_coordinator import ErrorClass
from nilo.review_errors import classify_provider_error


class ReviewErrorClassificationTest(unittest.TestCase):
    def test_claude_usage_limit_is_rate_limited(self) -> None:
        result = classify_provider_error("claude-code", stderr="Claude usage limit reached. retry-after: 60", exit_code=1)
        self.assertEqual(result.error_class, ErrorClass.RATE_LIMITED)
        self.assertEqual(result.retry_after, "60")

    def test_codex_quota_json_is_quota_exhausted(self) -> None:
        result = classify_provider_error(
            "codex",
            stdout='{"error":{"type":"insufficient_quota","message":"Quota exceeded","retry_after":"2026-01-01T00:00:00Z"}}',
        )
        self.assertEqual(result.error_class, ErrorClass.QUOTA_EXHAUSTED)
        self.assertEqual(result.error_code, "insufficient_quota")
        self.assertEqual(result.retry_after, "2026-01-01T00:00:00Z")

    def test_grok_429_is_rate_limited(self) -> None:
        result = classify_provider_error("grok", stderr="xAI API HTTP 429 Too Many Requests", exit_code=1)
        self.assertEqual(result.error_class, ErrorClass.RATE_LIMITED)

    def test_authentication_is_not_rate_limit(self) -> None:
        result = classify_provider_error("grok", stderr="Unauthorized: invalid API key", exit_code=1)
        self.assertEqual(result.error_class, ErrorClass.AUTHENTICATION)

    def test_normal_review_output_is_not_an_error(self) -> None:
        result = classify_provider_error("claude-code", stdout="# ReviewResult\n\n## Verdict\napproved")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
