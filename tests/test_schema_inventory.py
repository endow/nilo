from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from nilo.schema_catalog import JSON_FIELD_SCHEMAS, SCHEMA_CATALOG
from nilo.store import CORE_STATE_TABLES, Store


REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FIELDS = {
    "concept",
    "classification",
    "recommendation",
    "source_of_truth",
    "derived_from",
    "retention",
    "deletion",
    "migration_risk",
    "usage",
}


class SchemaInventoryTests(unittest.TestCase):
    def test_catalog_exactly_matches_actual_schema_tables(self) -> None:
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "nilo.db")
            try:
                actual = {
                    str(row[0])
                    for row in store.conn.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
            finally:
                store.close()
        self.assertEqual(actual, set(SCHEMA_CATALOG))
        self.assertLessEqual(CORE_STATE_TABLES, set(SCHEMA_CATALOG))

    def test_every_catalog_entry_has_required_inventory_fields(self) -> None:
        incomplete = {
            table: sorted(REQUIRED_FIELDS - set(entry))
            for table, entry in SCHEMA_CATALOG.items()
            if REQUIRED_FIELDS - set(entry)
        }
        self.assertEqual(incomplete, {})

    def test_document_and_usage_report_cover_every_table(self) -> None:
        inventory = (REPO_ROOT / "docs" / "schema-inventory.md").read_text(encoding="utf-8")
        usage = (REPO_ROOT / "docs" / "schema-usage-report.md").read_text(encoding="utf-8")
        missing_inventory = sorted(table for table in SCHEMA_CATALOG if f"`{table}`" not in inventory)
        missing_usage = sorted(table for table in SCHEMA_CATALOG if f"## `{table}`" not in usage)
        self.assertEqual(missing_inventory, [])
        self.assertEqual(missing_usage, [])

    def test_checked_in_usage_report_is_current(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/schema_usage_report.py"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        expected = (REPO_ROOT / "docs" / "schema-usage-report.md").read_text(encoding="utf-8")
        self.assertEqual(result.stdout, expected)

    def test_json_catalog_references_real_tables_and_columns(self) -> None:
        with TemporaryDirectory() as directory:
            store = Store(Path(directory) / "nilo.db")
            try:
                invalid = []
                for table, columns in JSON_FIELD_SCHEMAS.items():
                    actual_columns = {
                        str(row[1])
                        for row in store.conn.execute(f"PRAGMA table_info({table})")
                    }
                    for column in columns:
                        if column not in actual_columns:
                            invalid.append(f"{table}.{column}")
            finally:
                store.close()
        self.assertEqual(invalid, [])


if __name__ == "__main__":
    unittest.main()
