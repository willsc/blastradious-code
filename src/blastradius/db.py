"""SQLite storage. One connection per thread, WAL mode, schema versioned via user_version."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .util import dumps, loads

MIGRATIONS: list[str] = [
    # --- v1 -----------------------------------------------------------------
    """
    CREATE TABLE messages (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        source_id TEXT NOT NULL,
        thread_id TEXT,
        rfc_message_id TEXT,
        received_at TEXT,
        sender TEXT,
        sender_name TEXT,
        original_sender TEXT,
        subject TEXT,
        message_type TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        error TEXT,
        body_text TEXT,
        calendar_text TEXT,
        raw BLOB,
        created_at TEXT NOT NULL,
        processed_at TEXT,
        UNIQUE (source, source_id)
    );
    CREATE INDEX messages_received ON messages (received_at);

    CREATE TABLE notices (
        id INTEGER PRIMARY KEY,
        key TEXT NOT NULL UNIQUE,
        message_type TEXT,
        kind TEXT,
        provider TEXT,
        account TEXT,
        maintenance_id TEXT,
        title TEXT,
        summary TEXT,
        status TEXT,
        impact TEXT,
        start_at TEXT,
        end_at TEXT,
        windows TEXT,
        circuits TEXT,
        identifiers TEXT,
        sequence INTEGER,
        emergency INTEGER NOT NULL DEFAULT 0,
        severity INTEGER NOT NULL DEFAULT 0,
        infra_severity INTEGER NOT NULL DEFAULT 0,
        mapped INTEGER NOT NULL DEFAULT 0,
        apps TEXT,
        blast TEXT,
        parser TEXT,
        confidence TEXT,
        first_seen TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_message_at TEXT,
        correlated_at TEXT,
        snapshot_id INTEGER,
        version INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX notices_window ON notices (start_at, end_at);

    CREATE TABLE notice_messages (
        notice_id INTEGER NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
        message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
        role TEXT,
        PRIMARY KEY (notice_id, message_id)
    );

    CREATE TABLE notice_events (
        id INTEGER PRIMARY KEY,
        notice_id INTEGER NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
        at TEXT NOT NULL,
        kind TEXT NOT NULL,
        detail TEXT
    );
    CREATE INDEX notice_events_notice ON notice_events (notice_id, at);

    CREATE TABLE impacts (
        notice_id INTEGER NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
        node_key TEXT NOT NULL,
        node_type TEXT NOT NULL,
        node_id TEXT NOT NULL,
        name TEXT,
        account TEXT,
        region TEXT,
        severity INTEGER NOT NULL,
        anchor INTEGER NOT NULL DEFAULT 0,
        reason TEXT,
        PRIMARY KEY (notice_id, node_key)
    );
    CREATE INDEX impacts_node ON impacts (node_key);

    CREATE TABLE notice_apps (
        notice_id INTEGER NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
        app TEXT NOT NULL,
        severity INTEGER NOT NULL,
        PRIMARY KEY (notice_id, app)
    );
    CREATE INDEX notice_apps_app ON notice_apps (app);

    CREATE TABLE snapshots (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        source TEXT NOT NULL,
        status TEXT,
        summary TEXT,
        aws_raw BLOB,
        graph BLOB NOT NULL,
        inventory_hash TEXT
    );

    CREATE TABLE subscriptions (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        owner TEXT,
        description TEXT,
        filter TEXT NOT NULL DEFAULT '{}',
        channels TEXT NOT NULL DEFAULT '[]',
        events TEXT NOT NULL DEFAULT '[]',
        token TEXT NOT NULL UNIQUE,
        managed_key TEXT UNIQUE,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE deliveries (
        id INTEGER PRIMARY KEY,
        subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
        notice_id INTEGER REFERENCES notices(id) ON DELETE CASCADE,
        collision_id TEXT,
        event TEXT NOT NULL,
        dedupe_key TEXT NOT NULL,
        channel_index INTEGER NOT NULL,
        channel_type TEXT NOT NULL,
        target TEXT,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        payload TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        next_attempt_at TEXT,
        UNIQUE (subscription_id, dedupe_key, channel_index)
    );
    CREATE INDEX deliveries_status ON deliveries (status, next_attempt_at);

    CREATE TABLE collisions (
        id TEXT PRIMARY KEY,
        notice_ids TEXT NOT NULL,
        start_at TEXT NOT NULL,
        end_at TEXT NOT NULL,
        severity INTEGER NOT NULL,
        data TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        first_seen TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE kv (
        k TEXT PRIMARY KEY,
        v TEXT
    );
    """,
]


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.migrate()

    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def migrate(self) -> None:
        conn = self.conn()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for index in range(version, len(MIGRATIONS)):
            conn.executescript("BEGIN;" + MIGRATIONS[index] + f";PRAGMA user_version={index + 1};COMMIT;")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Write transaction; nested use joins the outer transaction."""
        conn = self.conn()
        if conn.in_transaction:
            yield conn
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def query(self, sql: str, params: Any = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, params).fetchall()

    def one(self, sql: str, params: Any = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Any = (), default: Any = None) -> Any:
        row = self.conn().execute(sql, params).fetchone()
        return default if row is None or row[0] is None else row[0]

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        return self.conn().execute(sql, params)

    # -- key/value state ----------------------------------------------------

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT v FROM kv WHERE k = ?", (key,))
        return loads(row["v"], default) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT (k) DO UPDATE SET v = excluded.v",
            (key, dumps(value)),
        )
