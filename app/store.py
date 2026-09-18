"""SQLite persistence (WAL) for setups, events, the Telegram outbox, OAuth and the engine lease.

A state transition and its outbox row are always written inside ONE transaction, so a restart can
never produce a second ENTER and never lose one (architecture §6, failure mode E1).
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS setups (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    model TEXT NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    computed_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at REAL NOT NULL,
    armed_at REAL,
    tap_at REAL,
    triggered_at REAL,
    closed_at REAL,
    expires_at REAL,
    last_processed_at REAL,
    entry_price REAL,
    score INTEGER,
    outcome TEXT,
    shadow_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_setups_state ON setups(state);
CREATE INDEX IF NOT EXISTS idx_setups_symbol ON setups(symbol, state);
CREATE INDEX IF NOT EXISTS idx_setups_fingerprint ON setups(fingerprint, created_at);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    setup_id TEXT NOT NULL,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_setup ON events(setup_id, id);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    chat_id TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at REAL NOT NULL,
    sent_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(sent_at, id);

CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY,
    data_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_pending (
    tx TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    params_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_codes (
    code_hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    data_json TEXT NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL,
    family TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    client_id TEXT NOT NULL,
    family TEXT NOT NULL,
    scopes TEXT NOT NULL,
    resource TEXT,
    expires_at REAL,
    revoked_at REAL,
    grace_until REAL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tokens_family ON oauth_tokens(family);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS lease (
    name TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    heartbeat REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS login_attempts (
    scope TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    first_failure REAL,
    locked_until REAL
);
"""


class Store:
    """Thin synchronous SQLite wrapper. All writes go through `transaction()`."""

    def __init__(self, path: str) -> None:
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    # ------------------------------------------------------------------ core
    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        current = self.get_kv("schema_version")
        if current != str(SCHEMA_VERSION):
            self.set_kv("schema_version", str(SCHEMA_VERSION))

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self.transaction() as conn:
            conn.execute(sql, params)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -------------------------------------------------------------------- kv
    def get_kv(self, key: str) -> str | None:
        row = self.query_one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_kv(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, time.time()),
        )

    def delete_kv(self, key: str) -> None:
        self.execute("DELETE FROM kv WHERE key = ?", (key,))

    def get_json(self, key: str, default: Any = None) -> Any:
        raw = self.get_kv(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return default

    def set_json(self, key: str, value: Any) -> None:
        self.set_kv(key, json.dumps(value, separators=(",", ":")))

    def secret_key(self) -> bytes:
        """A stable per-deployment secret (CSRF tokens). Generated once, then persisted."""
        existing = self.get_kv("secret_key")
        if existing:
            return bytes.fromhex(existing)
        generated = secrets.token_bytes(32)
        self.set_kv("secret_key", generated.hex())
        return generated

    # ----------------------------------------------------------------- lease
    def acquire_lease(self, name: str, holder: str, ttl_s: float = 30.0, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self.transaction() as conn:
            row = conn.execute("SELECT holder, heartbeat FROM lease WHERE name = ?", (name,)).fetchone()
            if row is None:
                conn.execute("INSERT INTO lease(name, holder, heartbeat) VALUES(?,?,?)", (name, holder, now))
                return True
            if row["holder"] == holder or now - row["heartbeat"] > ttl_s:
                conn.execute("UPDATE lease SET holder = ?, heartbeat = ? WHERE name = ?", (holder, now, name))
                return True
            return False

    def release_lease(self, name: str, holder: str) -> None:
        self.execute("DELETE FROM lease WHERE name = ? AND holder = ?", (name, holder))

    def lease_holder(self, name: str) -> tuple[str, float] | None:
        row = self.query_one("SELECT holder, heartbeat FROM lease WHERE name = ?", (name,))
        return (row["holder"], row["heartbeat"]) if row else None

    # ---------------------------------------------------------------- outbox
    def enqueue(self, conn: sqlite3.Connection, dedupe_key: str, chat_id: str, text: str, now: float) -> None:
        """Queue a Telegram message. Must run inside the same transaction as the state change."""
        conn.execute(
            "INSERT OR IGNORE INTO outbox(dedupe_key, chat_id, text, created_at) VALUES(?,?,?,?)",
            (dedupe_key, chat_id, text, now),
        )

    def queue_message(self, dedupe_key: str, chat_id: str, text: str, now: float | None = None) -> None:
        with self.transaction() as conn:
            self.enqueue(conn, dedupe_key, chat_id, text, time.time() if now is None else now)

    def pending_messages(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM outbox WHERE sent_at IS NULL ORDER BY id LIMIT ?", (limit,))

    def mark_sent(self, outbox_id: int, now: float | None = None) -> None:
        self.execute("UPDATE outbox SET sent_at = ? WHERE id = ?", (time.time() if now is None else now, outbox_id))

    def outbox_health(self) -> dict[str, Any]:
        """Pending count, the age of the oldest and why it last failed — a stuck queue is silent."""
        row = self.query_one(
            "SELECT COUNT(*) AS pending, MIN(created_at) AS oldest, MAX(attempts) AS attempts "
            "FROM outbox WHERE sent_at IS NULL"
        )
        error = self.query_one(
            "SELECT last_error FROM outbox WHERE sent_at IS NULL AND last_error IS NOT NULL "
            "ORDER BY attempts DESC LIMIT 1"
        )
        return {
            "pending": row["pending"] if row else 0,
            "oldest": row["oldest"] if row else None,
            "attempts": (row["attempts"] if row else 0) or 0,
            "last_error": (error["last_error"] if error else "") or "",
        }

    def mark_failed(self, outbox_id: int, error: str) -> None:
        self.execute(
            "UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?",
            (error[:400], outbox_id),
        )

    # ---------------------------------------------------------------- setups
    def insert_setup(self, conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        conn.execute(f"INSERT INTO setups({columns}) VALUES({placeholders})", tuple(row.values()))

    def add_event(self, conn: sqlite3.Connection, setup_id: str, ts: float, kind: str, data: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO events(setup_id, ts, type, data_json) VALUES(?,?,?,?)",
            (setup_id, ts, kind, json.dumps(data, separators=(",", ":"), default=str)),
        )

    def get_setup(self, setup_id: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM setups WHERE id = ?", (setup_id,))

    def setups_in_state(self, states: tuple[str, ...], symbol: str | None = None) -> list[sqlite3.Row]:
        marks = ",".join("?" for _ in states)
        sql = f"SELECT * FROM setups WHERE state IN ({marks})"
        params: tuple = states
        if symbol:
            sql += " AND symbol = ?"
            params = (*states, symbol.upper())
        return self.query(sql + " ORDER BY created_at", params)

    def recent_setups(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM setups ORDER BY created_at DESC LIMIT ?", (limit,))

    def events_for(self, setup_id: str, limit: int = 20) -> list[sqlite3.Row]:
        rows = self.query("SELECT * FROM events WHERE setup_id = ? ORDER BY id DESC LIMIT ?", (setup_id, limit))
        return list(reversed(rows))

    def find_duplicate(self, fingerprint: str, since: float) -> sqlite3.Row | None:
        return self.query_one(
            "SELECT * FROM setups WHERE fingerprint = ? AND created_at >= ? "
            "AND state NOT IN ('REJECTED','REPLACED','CANCELLED') ORDER BY created_at DESC LIMIT 1",
            (fingerprint, since),
        )


def json_loads(raw: Any, default: Any = None) -> Any:
    if raw in (None, ""):
        return {} if default is None else default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {} if default is None else default
