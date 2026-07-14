from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from nilo.review import build_review_context, looks_like_review_result, parse_review_result


class ReviewContextTests(unittest.TestCase):
    def test_parse_review_result_ignores_no_findings_sentinel(self) -> None:
        body = """# ReviewResult

## Verdict
approved

## Summary
Looks good.

## Findings
No findings.
"""

        verdict, summary, findings = parse_review_result(body)

        self.assertEqual(verdict, "approved")
        self.assertEqual(summary, "Looks good.")
        self.assertEqual(findings, [])

    def test_parse_review_result_accepts_fenced_markdown_with_preface(self) -> None:
        body = """Here is the review.

```markdown
# ReviewResult

## Verdict
approved

## Summary
OK.

## Findings
なし
```
"""

        verdict, summary, findings = parse_review_result(body)

        self.assertEqual(verdict, "approved")
        self.assertEqual(summary, "OK.")
        self.assertEqual(findings, [])

    def test_parse_review_result_accepts_json_output(self) -> None:
        body = '{"verdict":"changes_requested","summary":"Fix one issue.","findings":[{"title":"Bug","severity":"high","file":"a.py","line":7,"blocking":true,"description":"broken"}]}'

        verdict, summary, findings = parse_review_result(body)

        self.assertEqual(verdict, "changes_requested")
        self.assertEqual(summary, "Fix one issue.")
        self.assertEqual(findings[0]["title"], "Bug")
        self.assertTrue(findings[0]["blocking"])

    def test_parse_review_result_rejects_unknown_finding_field(self) -> None:
        body = """# ReviewResult

## Verdict
approved

## Summary
Looks good.

## Findings
### F1
severity: low
mblocking: false

Typo in field name.
"""

        with self.assertRaisesRegex(ValueError, "unknown ReviewResult finding field: mblocking"):
            parse_review_result(body)

    def test_parse_review_result_allows_colons_in_description_and_code(self) -> None:
        body = """# ReviewResult

## Verdict
commented

## Summary
One note.

## Findings
### F1
severity: low
blocking: false

Reproduction: open https://example.com/path
```yaml
custom: value
```
"""

        _verdict, _summary, findings = parse_review_result(body)

        self.assertIn("Reproduction: open https://example.com/path", findings[0]["description"])
        self.assertIn("custom: value", findings[0]["description"])

    def test_parse_review_result_combines_description_field_and_free_text(self) -> None:
        body = """# ReviewResult

## Findings
### F1
severity: low
description: Short summary.

Additional detail.
"""

        _verdict, _summary, findings = parse_review_result(body)

        self.assertEqual(findings[0]["description"], "Short summary.\nAdditional detail.")

    def test_looks_like_review_result_rejects_unrecognized_json(self) -> None:
        self.assertFalse(looks_like_review_result("{}"))
        self.assertFalse(looks_like_review_result('{"error":"rate limited"}'))

    def test_build_review_context_uses_task_diff_when_working_tree_is_clean(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)
            path = root / "example.txt"
            path.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "example.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            base = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            ).stdout.strip()
            path.write_text("after\n", encoding="utf-8")
            subprocess.run(["git", "add", "example.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "change"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            ).stdout.strip()

            body = build_review_context(
                {
                    "id": "task_test",
                    "title": "Review committed diff",
                    "task_type": "implementation",
                    "risk_level": "medium",
                    "description": "",
                    "acceptance_criteria": [],
                    "base_commit": base,
                },
                {
                    "id": "review_test",
                    "requester": "codex",
                    "reviewer": "claude-code",
                    "status": "requested",
                    "reason": "test",
                },
                None,
                None,
                {
                    "command": "python -m unittest",
                    "exit_code": 0,
                    "timed_out": False,
                    "stdout": "",
                    "stderr": "",
                    "git_head": head,
                },
                root,
            )

        self.assertIn("diff --git a/example.txt b/example.txt", body)
        self.assertIn("-before", body)
        self.assertIn("+after", body)
        self.assertNotIn("# no working tree diff", body)

    def test_build_review_context_includes_untracked_text_preview(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            (root / "new file.txt").write_text("hello from untracked\n", encoding="utf-8")

            body = build_review_context(
                {
                    "id": "task_test",
                    "title": "Review dirty tree",
                    "task_type": "review",
                    "risk_level": "medium",
                    "description": "",
                    "acceptance_criteria": [],
                    "base_commit": None,
                },
                {
                    "id": "review_test",
                    "requester": "codex",
                    "reviewer": "claude-code",
                    "status": "requested",
                    "reason": "test",
                },
                None,
                None,
                None,
                root,
            )

        self.assertIn("## Untracked File Preview", body)
        self.assertIn("### new file.txt", body)
        self.assertIn("hello from untracked", body)


if __name__ == "__main__":
    unittest.main()
