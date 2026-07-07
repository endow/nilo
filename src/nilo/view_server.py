from __future__ import annotations

import json
import sqlite3
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import view_model
from .view_assets import APP_CSS, APP_HTML, APP_JS


class ViewRequestHandler(BaseHTTPRequestHandler):
    server: "ViewHTTPServer"

    def do_GET(self) -> None:
        try:
            self.route_get()
        except KeyError as exc:
            self.respond_json({"error": str(exc).strip("'")}, status=404)
        except SystemExit as exc:
            self.respond_json({"error": str(exc)}, status=404)
        except (sqlite3.OperationalError, ValueError) as exc:
            self.respond_json(
                {
                    "error": "database schema is not ready for nilo view",
                    "detail": str(exc),
                    "hint": "DB が古い可能性があります。一度通常の nilo コマンドを実行してマイグレーションしてから、nilo view を再実行してください。",
                },
                status=503,
            )

    def route_get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/":
            self.respond_text(APP_HTML, "text/html; charset=utf-8")
            return
        if path == "/assets/app.css":
            self.respond_text(APP_CSS, "text/css; charset=utf-8")
            return
        if path == "/assets/app.js":
            self.respond_text(APP_JS, "text/javascript; charset=utf-8")
            return
        if path == "/api/overview":
            self.respond_json(view_model.overview(self.server.db_path, self.server.project_id))
            return
        if path == "/api/analytics":
            self.respond_json(view_model.analytics(self.server.db_path, self.server.project_id))
            return
        if path == "/api/tasks":
            self.respond_json(
                view_model.tasks(
                    self.server.db_path,
                    self.server.project_id,
                    page=_int_query(query, "page", 1),
                    page_size=_int_query(query, "page_size", 50),
                    status=_str_query(query, "status"),
                    task_type=_str_query(query, "task_type"),
                    risk_level=_str_query(query, "risk_level"),
                    open_findings=_bool_query(query, "open_findings"),
                    open_failures=_bool_query(query, "open_failures"),
                    reservations=_bool_query(query, "reservations"),
                    roadmap=_str_query(query, "roadmap"),
                )
            )
            return
        if path == "/api/todos":
            self.respond_json(view_model.todos(self.server.db_path, self.server.project_id))
            return
        if path.startswith("/api/tasks/"):
            task_id = unquote(path.removeprefix("/api/tasks/"))
            self.respond_json(view_model.task_detail(self.server.db_path, self.server.project_id, task_id))
            return
        if path == "/api/timeline":
            self.respond_json(view_model.timeline(self.server.db_path, self.server.project_id))
            return
        self.respond_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        self.respond_json({"error": "read-only view"}, status=405)

    def do_PUT(self) -> None:
        self.respond_json({"error": "read-only view"}, status=405)

    def do_PATCH(self) -> None:
        self.respond_json({"error": "read-only view"}, status=405)

    def do_DELETE(self) -> None:
        self.respond_json({"error": "read-only view"}, status=405)

    def respond_json(self, data: object, *, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def respond_text(self, text: str, content_type: str) -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class ViewHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler], *, db_path: Path | None, project_id: str) -> None:
        super().__init__(server_address, handler_class)
        self.db_path = db_path
        self.project_id = project_id


def make_server(db_path: Path | None, project_id: str, host: str, port: int) -> ViewHTTPServer:
    return ViewHTTPServer((host, port), ViewRequestHandler, db_path=db_path, project_id=project_id)


def _int_query(query: dict[str, list[str]], key: str, default: int) -> int:
    try:
        return int(query.get(key, [str(default)])[0])
    except ValueError:
        return default


def _str_query(query: dict[str, list[str]], key: str) -> str:
    return query.get(key, [""])[0]


def _bool_query(query: dict[str, list[str]], key: str) -> bool:
    return _str_query(query, key).lower() in {"1", "true", "yes", "on"}


def run_view_server(*, db_path: Path | None, project_id: str, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    try:
        server = make_server(db_path, project_id, host, port)
    except OSError as exc:
        raise SystemExit(f"nilo view server could not bind to {host}:{port}: {exc}. Try --port with another port.") from exc
    url = f"http://{host}:{server.server_port}"
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print(f"Warning: Nilo view is binding to non-local host {host}.", flush=True)
    print(f"Nilo view: {url}", flush=True)
    print(f"Project: {project_id}", flush=True)
    print("Mode: read-only", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    if open_browser:
        threading.Timer(0.1, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
