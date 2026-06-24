from __future__ import annotations

import sqlite3
from pathlib import Path


def make_sqlite_db(path: Path, *, body: str = "committed", keep_open: bool = False) -> sqlite3.Connection | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS notes (body TEXT NOT NULL)")
    conn.execute("INSERT INTO notes (body) VALUES (?)", (body,))
    conn.commit()
    if keep_open:
        return conn
    conn.close()
    return None
