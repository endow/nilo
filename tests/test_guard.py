from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nilo.guard import evaluate_evidence
from nilo.report import claimed_status, declares_no_changed_files, extract_changed_files, validate_report_shape


VALID_REPORT = """# 完了報告

## 1. 実施内容
Evidence Guardを実装した。

## 2. 変更ファイル一覧
- src/nilo/guard.py

## 3. 実行した検証
### テストコマンド
python -m pytest
### テスト結果
2 passed
### 型チェック
未実行。型チェック環境が未定義のため。
### lint
未実行。lint環境が未定義のため。

## 4. 未実行の検証（理由を記載）
型チェックとlintはプロジェクト設定がないため未実行。

## 5. 既知の問題 / 仕様から外れた判断
なし。仕様から外れた判断はない。

## 6. 人間に確認してほしい点
追加確認は不要。
"""


class GuardTests(unittest.TestCase):
    def test_extract_changed_files(self) -> None:
        self.assertEqual(extract_changed_files(VALID_REPORT), ["src/nilo/guard.py"])

    def test_validate_report_shape_flags_placeholder(self) -> None:
        report = VALID_REPORT.replace("2 passed", "TODO")
        issues = validate_report_shape(report)
        self.assertTrue(any("placeholder" in issue for issue in issues))

    def test_validate_report_shape_allows_bracketed_logs(self) -> None:
        report = VALID_REPORT.replace("2 passed", "[100%] 2 passed")
        issues = validate_report_shape(report)
        self.assertFalse(any("placeholder" in issue for issue in issues))

    def test_claimed_status_ignores_completion_report_heading(self) -> None:
        self.assertEqual(claimed_status(VALID_REPORT), "reported")

    def test_claimed_status_reads_explicit_status_line(self) -> None:
        report = VALID_REPORT.replace("Evidence Guardを実装した。", "status: completed\nEvidence Guardを実装した。")
        self.assertEqual(claimed_status(report), "completed")

    def test_extract_changed_files_ignores_prose(self) -> None:
        report = VALID_REPORT.replace("- src/nilo/guard.py", "- guard.py を修正\n- src/nilo/guard.py")
        self.assertEqual(extract_changed_files(report), ["src/nilo/guard.py"])

    def test_extract_changed_files_accepts_extensionless_paths(self) -> None:
        report = VALID_REPORT.replace("- src/nilo/guard.py", "- src/nilo/Makefile")
        self.assertEqual(extract_changed_files(report), ["src/nilo/Makefile"])

    def test_extract_changed_files_accepts_root_extensionless_files(self) -> None:
        for path in ("LICENSE", "Makefile", "Dockerfile", "NOTICE", "README"):
            with self.subTest(path=path):
                report = VALID_REPORT.replace("- src/nilo/guard.py", f"- {path}")
                self.assertEqual(extract_changed_files(report), [path])

    def test_extract_changed_files_normalizes_leading_dot_slash(self) -> None:
        report = VALID_REPORT.replace("- src/nilo/guard.py", "- ./LICENSE")
        self.assertEqual(extract_changed_files(report), ["LICENSE"])

    def test_extract_changed_files_rejects_non_local_paths(self) -> None:
        for path in ("/tmp/LICENSE", "../LICENSE", "docs/../../LICENSE", "C:/repo/LICENSE"):
            with self.subTest(path=path):
                report = VALID_REPORT.replace("- src/nilo/guard.py", f"- {path}")
                self.assertEqual(extract_changed_files(report), [])

    def test_extract_changed_files_accepts_dotfiles(self) -> None:
        report = VALID_REPORT.replace("- src/nilo/guard.py", "- .gitignore")
        self.assertEqual(extract_changed_files(report), [".gitignore"])

    def test_extract_changed_files_accepts_paths_with_spaces(self) -> None:
        report = VALID_REPORT.replace("- src/nilo/guard.py", "- src/nilo/My Report.md")
        self.assertEqual(extract_changed_files(report), ["src/nilo/My Report.md"])

    def test_extract_changed_files_accepts_extensionless_paths_below_directories_with_spaces(self) -> None:
        report = VALID_REPORT.replace("- src/nilo/guard.py", "- legal notices/NOTICE")
        self.assertEqual(extract_changed_files(report), ["legal notices/NOTICE"])

    def test_extract_changed_files_ignores_prose_with_path_and_spaces(self) -> None:
        report = VALID_REPORT.replace("- src/nilo/guard.py", "- src/nilo/My Report.md を修正")
        self.assertEqual(extract_changed_files(report), [])

    def test_extract_changed_files_accepts_no_changed_files_claim(self) -> None:
        report = VALID_REPORT.replace("- src/nilo/guard.py", "- 変更ファイルなし")
        self.assertEqual(extract_changed_files(report), [])
        self.assertTrue(declares_no_changed_files("- 変更ファイルなし"))

    def test_evaluate_evidence_accepts_no_changed_files_when_git_diff_is_empty(self) -> None:
        report = VALID_REPORT.replace("- src/nilo/guard.py", "- 変更ファイルなし")
        with patch("nilo.guard.changed_files_since", return_value=(set(), [])):
            status, issues, metadata = evaluate_evidence(report, [], "abc123", Path.cwd())

        self.assertEqual(status, "evidence_submitted")
        self.assertEqual(issues, [])
        self.assertEqual(metadata["reported_changed_files"], [])

    def test_evaluate_evidence_without_git_needs_review(self) -> None:
        with TemporaryDirectory() as directory:
            status, issues, metadata = evaluate_evidence(VALID_REPORT, ["src/nilo/guard.py"], None, Path(directory))

        self.assertEqual(status, "needs_human_review")
        self.assertTrue(issues)
        self.assertEqual(metadata["reported_changed_files"], ["src/nilo/guard.py"])

    def test_changed_files_mismatch_explains_existing_dirty_files(self) -> None:
        with patch("nilo.guard.changed_files_since", return_value=({"data/input.json"}, [])):
            status, issues, metadata = evaluate_evidence(VALID_REPORT, ["src/nilo/guard.py"], "abc123", Path.cwd())

        self.assertEqual(status, "needs_human_review")
        self.assertEqual(metadata["actual_changed_files"], ["data/input.json"])
        self.assertTrue(any("既存 dirty files" in issue for issue in issues))
        self.assertTrue(any(issue.startswith("changed_files missing actual files: data/input.json") for issue in issues))


if __name__ == "__main__":
    unittest.main()
