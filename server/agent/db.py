"""SQLite storage: reminders, pending confirmation, helper command queue, key/value state.

All timestamps are stored as UTC ISO-8601 strings.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    due_at TEXT NOT NULL,
    heads_up_at TEXT,
    heads_up_sent INTEGER NOT NULL DEFAULT 0,
    sent INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pending (
    slot INTEGER PRIMARY KEY CHECK (slot = 1),
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS helper_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',  -- queued | sent | done | failed
    detail TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class DB:
    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # ---- key/value ----
    def get(self, key: str, default: Any = None) -> Any:
        rows = self._all("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    # ---- reminders ----
    def add_reminder(self, text: str, due_at: datetime, heads_up_at: datetime | None) -> int:
        cur = self._exec(
            "INSERT INTO reminders(text, due_at, heads_up_at) VALUES(?, ?, ?)",
            (text, _iso(due_at), _iso(heads_up_at) if heads_up_at else None),
        )
        return int(cur.lastrowid)

    def upcoming_reminders(self) -> list[dict]:
        rows = self._all("SELECT * FROM reminders WHERE sent = 0 ORDER BY due_at")
        return [self._reminder(r) for r in rows]

    def delete_reminder(self, rid: int) -> bool:
        return self._exec("DELETE FROM reminders WHERE id = ? AND sent = 0", (rid,)).rowcount > 0

    def due_reminders(self, now: datetime) -> list[dict]:
        rows = self._all("SELECT * FROM reminders WHERE sent = 0 AND due_at <= ?", (_iso(now),))
        return [self._reminder(r) for r in rows]

    def due_heads_ups(self, now: datetime) -> list[dict]:
        rows = self._all(
            "SELECT * FROM reminders WHERE sent = 0 AND heads_up_sent = 0 "
            "AND heads_up_at IS NOT NULL AND heads_up_at <= ?",
            (_iso(now),),
        )
        return [self._reminder(r) for r in rows]

    def mark_reminder_sent(self, rid: int) -> None:
        self._exec("UPDATE reminders SET sent = 1, heads_up_sent = 1 WHERE id = ?", (rid,))

    def mark_heads_up_sent(self, rid: int) -> None:
        self._exec("UPDATE reminders SET heads_up_sent = 1 WHERE id = ?", (rid,))

    @staticmethod
    def _reminder(r: sqlite3.Row) -> dict:
        return {
            "id": r["id"],
            "text": r["text"],
            "due_at": _dt(r["due_at"]),
            "heads_up_at": _dt(r["heads_up_at"]),
        }

    # ---- pending confirmation (single slot) ----
    def set_pending(self, kind: str, payload: dict, expires_at: datetime) -> None:
        self._exec(
            "INSERT INTO pending(slot, kind, payload, expires_at) VALUES(1, ?, ?, ?) "
            "ON CONFLICT(slot) DO UPDATE SET kind = excluded.kind, "
            "payload = excluded.payload, expires_at = excluded.expires_at",
            (kind, json.dumps(payload), _iso(expires_at)),
        )

    def pop_pending(self, now: datetime) -> dict | None:
        """Return and clear the pending action, or None if absent/expired."""
        rows = self._all("SELECT * FROM pending WHERE slot = 1")
        self._exec("DELETE FROM pending")
        if not rows:
            return None
        r = rows[0]
        if _dt(r["expires_at"]) < now:
            return None
        return {"kind": r["kind"], "payload": json.loads(r["payload"])}

    # ---- helper command queue ----
    def queue_helper_command(self, action: str, payload: dict, now: datetime) -> int:
        cur = self._exec(
            "INSERT INTO helper_commands(action, payload, created_at) VALUES(?, ?, ?)",
            (action, json.dumps(payload), _iso(now)),
        )
        return int(cur.lastrowid)

    def next_helper_command(self) -> dict | None:
        """Hand out the oldest queued command and mark it sent."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM helper_commands WHERE status = 'queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if not row:
                return None
            self._conn.execute(
                "UPDATE helper_commands SET status = 'sent' WHERE id = ?", (row["id"],)
            )
            self._conn.commit()
        return {"id": row["id"], "action": row["action"], "payload": json.loads(row["payload"])}

    def helper_command_created(self, cid: int) -> datetime:
        rows = self._all("SELECT created_at FROM helper_commands WHERE id = ?", (cid,))
        return _dt(rows[0]["created_at"])

    def finish_helper_command(self, cid: int, ok: bool, detail: str) -> dict | None:
        rows = self._all("SELECT * FROM helper_commands WHERE id = ?", (cid,))
        if not rows:
            return None
        self._exec(
            "UPDATE helper_commands SET status = ?, detail = ? WHERE id = ?",
            ("done" if ok else "failed", detail, cid),
        )
        r = rows[0]
        return {"id": r["id"], "action": r["action"], "payload": json.loads(r["payload"])}
