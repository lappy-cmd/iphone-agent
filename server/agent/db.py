"""Storage: reminders, pending confirmation, helper command queue, key/value state.

Two backends with the same SQL (SQLite dialect):
- a local SQLite file (default, for a VM or laptop), or
- a hosted Turso/libSQL database over its HTTP API (for free hosts whose disk is wiped on restart).

All timestamps are stored as UTC ISO-8601 strings.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
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
CREATE TABLE IF NOT EXISTS seen_emails (
    id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,  -- user | model
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""
KEEP_MESSAGES = 200


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class DBError(Exception):
    pass


@dataclass
class Result:
    rows: list[dict] = field(default_factory=list)
    rowcount: int = 0
    lastrowid: int | None = None


def _statements(script: str) -> list[str]:
    return [st.strip() for st in script.split(";") if st.strip()]


class SQLiteConn:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def execute(self, sql: str, args: tuple = ()) -> Result:
        cur = self._conn.execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()]
        self._conn.commit()
        return Result(rows, cur.rowcount, cur.lastrowid)

    def executescript(self, script: str) -> None:
        self._conn.executescript(script)
        self._conn.commit()


def _encode(v: Any) -> dict:
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    return {"type": "text", "value": str(v)}


def _decode(v: dict) -> Any:
    t = v.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(v["value"])
    if t == "float":
        return float(v["value"])
    return v.get("value")


class TursoConn:
    """Minimal client for Turso's HTTP API (POST /v2/pipeline). Each call is its own request."""

    def __init__(self, url: str, token: str, client=None):
        import httpx

        if url.startswith("libsql://"):
            url = "https://" + url[len("libsql://"):]
        self._url = url.rstrip("/") + "/v2/pipeline"
        self._headers = {"Authorization": f"Bearer {token}"}
        self._client = client or httpx.Client(timeout=15)

    def _pipeline(self, stmts: list[tuple[str, tuple]]) -> list[dict]:
        reqs = [
            {"type": "execute", "stmt": {"sql": sql, "args": [_encode(a) for a in args]}}
            for sql, args in stmts
        ]
        reqs.append({"type": "close"})
        try:
            resp = self._client.post(self._url, json={"requests": reqs}, headers=self._headers)
        except Exception as exc:
            raise DBError(f"database unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise DBError(f"database HTTP {resp.status_code}: {resp.text[:200]}")
        out = []
        for res in resp.json()["results"][: len(stmts)]:
            if res.get("type") != "ok":
                raise DBError(res.get("error", {}).get("message", "database error"))
            out.append(res["response"]["result"])
        return out

    def execute(self, sql: str, args: tuple = ()) -> Result:
        r = self._pipeline([(sql, args)])[0]
        names = [c.get("name") for c in r.get("cols", [])]
        rows = [dict(zip(names, (_decode(v) for v in row))) for row in r.get("rows", [])]
        last = r.get("last_insert_rowid")
        return Result(rows, int(r.get("affected_row_count") or 0), int(last) if last is not None else None)

    def executescript(self, script: str) -> None:
        self._pipeline([(st, ()) for st in _statements(script)])


class DB:
    def __init__(self, path: str = ":memory:", *, turso_url: str = "", turso_token: str = "", http_client=None):
        if turso_url:
            self._conn = TursoConn(turso_url, turso_token, http_client)
        else:
            self._conn = SQLiteConn(path)
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def _exec(self, sql: str, args: tuple = ()) -> Result:
        with self._lock:
            return self._conn.execute(sql, args)

    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        return self._exec(sql, args).rows

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
    def _reminder(r: dict) -> dict:
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

    def peek_pending(self, now: datetime) -> dict | None:
        """The pending action without clearing it, or None if absent/expired."""
        rows = self._all("SELECT * FROM pending WHERE slot = 1")
        if not rows or _dt(rows[0]["expires_at"]) < now:
            return None
        return {"kind": rows[0]["kind"], "payload": json.loads(rows[0]["payload"])}

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
            rows = self._all(
                "SELECT * FROM helper_commands WHERE status = 'queued' ORDER BY id LIMIT 1"
            )
            if not rows:
                return None
            row = rows[0]
            self._exec("UPDATE helper_commands SET status = 'sent' WHERE id = ?", (row["id"],))
        return {"id": row["id"], "action": row["action"], "payload": json.loads(row["payload"])}

    def helper_command_created(self, cid: int) -> datetime:
        rows = self._all("SELECT created_at FROM helper_commands WHERE id = ?", (cid,))
        return _dt(rows[0]["created_at"])

    def get_helper_command(self, cid: int) -> dict | None:
        rows = self._all("SELECT status, detail FROM helper_commands WHERE id = ?", (cid,))
        return {"status": rows[0]["status"], "detail": rows[0]["detail"] or ""} if rows else None

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

    # ---- processed Google Voice emails ----
    def mark_seen(self, email_id: str) -> bool:
        """Record an email as handled. True if it was new."""
        return self._exec("INSERT OR IGNORE INTO seen_emails(id) VALUES(?)", (email_id,)).rowcount > 0

    # ---- conversation memory ----
    def add_message(self, role: str, text: str, now: datetime) -> None:
        cur = self._exec(
            "INSERT INTO messages(role, text, created_at) VALUES(?, ?, ?)", (role, text, _iso(now))
        )
        if cur.lastrowid and cur.lastrowid % 50 == 0:
            self._exec("DELETE FROM messages WHERE id <= ?", (cur.lastrowid - KEEP_MESSAGES,))

    def recent_messages(self, limit: int, role: str | None = None) -> list[dict]:
        where, args = ("WHERE role = ? ", (role, limit)) if role else ("", (limit,))
        rows = self._all(
            f"SELECT role, text FROM messages {where}ORDER BY id DESC LIMIT ?", args
        )
        return [{"role": r["role"], "text": r["text"]} for r in reversed(rows)]
