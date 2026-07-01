from __future__ import annotations

import json
import sqlite3
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from nilo.store import Store
from nilo.timeutil import now_iso
from nilo.view_server import make_server, run_view_server


class ViewServerTests(unittest.TestCase):
    def test_view_server_json_routes_and_read_only_methods(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            self.seed_minimal_db(db)
            server = make_server(db, "project_test", "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                overview = self.get_json(f"{base}/api/overview")
                self.assertEqual(overview["project"]["id"], "project_test")
                tasks = self.get_json(f"{base}/api/tasks?page=1&page_size=1")
                self.assertIn("tasks", tasks)
                self.assertEqual(tasks["pagination"]["page_size"], 1)
                self.assertEqual(self.get_json(f"{base}/api/tasks/task_one")["task"]["id"], "task_one")
                self.assertIn("todos", self.get_json(f"{base}/api/todos"))
                self.assertIn("events", self.get_json(f"{base}/api/timeline"))
                self.assertIn("summary", self.get_json(f"{base}/api/analytics"))

                request = Request(f"{base}/api/tasks", method="POST")
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request, timeout=5)
                self.assertEqual(raised.exception.code, 405)
                raised.exception.close()

                with self.assertRaises(HTTPError) as not_found:
                    urlopen(f"{base}/api/tasks/missing_task", timeout=5)
                self.assertEqual(not_found.exception.code, 404)
                not_found.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_view_server_reports_old_schema_as_json_error(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            self.seed_minimal_db(db)
            conn = sqlite3.connect(db)
            try:
                conn.execute("DROP TABLE overdrive_events")
                conn.commit()
            finally:
                conn.close()
            server = make_server(db, "project_test", "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with self.assertRaises(HTTPError) as raised:
                    urlopen(f"{base}/api/timeline", timeout=5)
                self.assertEqual(raised.exception.code, 503)
                body = json.loads(raised.exception.read().decode("utf-8"))
                self.assertIn("database schema is not ready", body["error"])
                self.assertIn("マイグレーション", body["hint"])
                raised.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_view_server_reports_port_conflict_without_traceback(self) -> None:
        with TemporaryDirectory() as directory:
            db = Path(directory) / "nilo.db"
            self.seed_minimal_db(db)
            server = make_server(db, "project_test", "127.0.0.1", 0)
            try:
                with self.assertRaises(SystemExit) as raised:
                    run_view_server(
                        db_path=db,
                        project_id="project_test",
                        host="127.0.0.1",
                        port=server.server_port,
                        open_browser=False,
                    )
                self.assertIn("could not bind", str(raised.exception))
                self.assertIn("--port", str(raised.exception))
            finally:
                server.server_close()

    def get_json(self, url: str) -> dict:
        with urlopen(url, timeout=5) as response:
            self.assertEqual(response.headers["Content-Type"], "application/json; charset=utf-8")
            return json.loads(response.read().decode("utf-8"))

    def seed_minimal_db(self, db: Path) -> None:
        now = now_iso()
        store = Store(db)
        try:
            store.insert(
                "projects",
                {
                    "id": "project_test",
                    "name": "Project Test",
                    "tech_stack": [],
                    "rules": [],
                    "default_completion_criteria": [],
                    "available_models": [],
                    "fallback_models": [],
                    "requires_local_execution": 0,
                    "created_at": now,
                },
            )
            store.insert(
                "tasks",
                {
                    "id": "task_one",
                    "project_id": "project_test",
                    "title": "Task one",
                    "description": "",
                    "acceptance_criteria": [],
                    "parent_task_id": None,
                    "split_index": None,
                    "task_type": "implementation",
                    "risk_level": "medium",
                    "requires_understanding_check": 0,
                    "roadmap_commitment_id": "",
                    "roadmap_item_id": "",
                    "status": "planned",
                    "assigned_model_profile": "",
                    "degradation_mode": "normal",
                    "mode": "normal",
                    "base_commit": None,
                    "created_at": now,
                },
            )
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
