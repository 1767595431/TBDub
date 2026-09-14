from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS avatars (
    identifier TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    user_id TEXT,
    status TEXT NOT NULL,
    bake_status TEXT NOT NULL,
    bake_progress INTEGER DEFAULT 0,
    bake_message TEXT DEFAULT '',
    error TEXT,
    duration REAL,
    width INTEGER,
    height INTEGER,
    source_path TEXT,
    preview_path TEXT,
    poster_path TEXT,
    preprocess_path TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    task_name TEXT NOT NULL,
    user_id TEXT NOT NULL,
    avatar_identifier TEXT NOT NULL,
    status TEXT NOT NULL,
    progress REAL DEFAULT 0,
    progress_message TEXT DEFAULT '',
    error_message TEXT DEFAULT '',
    steps INTEGER NOT NULL,
    quality_label TEXT NOT NULL,
    mode TEXT NOT NULL,
    audio_name TEXT,
    audio_path TEXT,
    audio_duration REAL,
    result_path TEXT,
    preview_path TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    retain_from TEXT NOT NULL,
    pid INTEGER,
    eta_seconds REAL,
    avatar_type TEXT
);

CREATE TABLE IF NOT EXISTS uploads (
    upload_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    filename TEXT,
    filesize INTEGER,
    chunk_size INTEGER,
    total_chunks INTEGER,
    received TEXT,
    name TEXT,
    type TEXT,
    user_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=8000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        avatar_cols = {row["name"] for row in self.query("PRAGMA table_info(avatars)")}
        if "preprocess_path" not in avatar_cols:
            self.execute("ALTER TABLE avatars ADD COLUMN preprocess_path TEXT")
        task_cols = {row["name"] for row in self.query("PRAGMA table_info(tasks)")}
        if "eta_seconds" not in task_cols:
            self.execute("ALTER TABLE tasks ADD COLUMN eta_seconds REAL")
        if "avatar_type" not in task_cols:
            self.execute("ALTER TABLE tasks ADD COLUMN avatar_type TEXT")
            self.execute(
                "UPDATE tasks SET avatar_type=("
                "SELECT type FROM avatars WHERE avatars.identifier=tasks.avatar_identifier"
                ") WHERE avatar_type IS NULL OR avatar_type=''"
            )

    def execute(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur

    def query(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(row) for row in rows]

    def one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def insert(self, table: str, data: dict[str, Any]) -> None:
        keys = list(data.keys())
        placeholders = ",".join("?" * len(keys))
        sql = f"INSERT INTO {table} ({','.join(keys)}) VALUES ({placeholders})"
        self.execute(sql, tuple(data[k] for k in keys))

    def update(self, table: str, pk: str, pk_value: str, data: dict[str, Any]) -> None:
        if not data:
            return
        sets = ",".join(f"{k}=?" for k in data)
        sql = f"UPDATE {table} SET {sets} WHERE {pk}=?"
        self.execute(sql, tuple(data.values()) + (pk_value,))

    def delete(self, table: str, pk: str, pk_value: str) -> None:
        self.execute(f"DELETE FROM {table} WHERE {pk}=?", (pk_value,))

    def claim_wait_task(self, task_id: str, data: dict[str, Any]) -> bool:
        if not data:
            return False
        sets = ",".join(f"{k}=?" for k in data)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE tasks SET {sets} WHERE task_id=? AND status='wait'",
                tuple(data.values()) + (task_id,),
            )
            self._conn.commit()
            return cur.rowcount == 1

    @staticmethod
    def dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def loads(value: str | None, default: Any) -> Any:
        if not value:
            return default
        return json.loads(value)
