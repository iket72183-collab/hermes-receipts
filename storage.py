"""Profile-scoped SQLite receipts. Paths come from plugin_data_dir on every open."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import PLUGIN_NAME, host_context_complete, valid_id

_LOCK = threading.Lock()
_EPOCH: str | None = None
_EPOCH_PID: int | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    host_task_id TEXT,
    host_session_id TEXT,
    criteria_digest TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    sealed_at TEXT,
    state TEXT NOT NULL,
    receipt_json TEXT,
    identity_conflict INTEGER NOT NULL DEFAULT 0,
    capture_epoch TEXT
);
CREATE TABLE IF NOT EXISTS armed (
    attempt_id TEXT NOT NULL,
    check_id TEXT NOT NULL,
    command TEXT NOT NULL,
    workdir TEXT NOT NULL,
    expected_exit INTEGER NOT NULL,
    contains TEXT,
    timeout_seconds INTEGER NOT NULL,
    criteria_digest TEXT NOT NULL,
    host_task_id TEXT,
    host_session_id TEXT,
    armed_at TEXT NOT NULL,
    epoch TEXT NOT NULL,
    expired INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (attempt_id, check_id)
);
CREATE TABLE IF NOT EXISTS pre_events (
    tool_call_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL,
    check_id TEXT NOT NULL,
    command TEXT NOT NULL,
    workdir TEXT,
    host_task_id TEXT,
    host_session_id TEXT,
    started_at TEXT NOT NULL,
    epoch TEXT NOT NULL,
    expired INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS observations (
    tool_call_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL,
    check_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def current_epoch() -> str:
    global _EPOCH, _EPOCH_PID
    pid = os.getpid()
    if _EPOCH is None or _EPOCH_PID != pid:
        _EPOCH = secrets.token_hex(8)
        _EPOCH_PID = pid
    return _EPOCH


def reset_epoch_for_tests() -> str:
    """Test-only: simulate a process restart's new capture epoch."""
    global _EPOCH
    _EPOCH = None
    return current_epoch()


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open receipt storage. Production resolves the active profile on every call."""
    if db_path is None:
        from plugins.plugin_storage import plugin_data_dir, plugin_db

        root = plugin_data_dir(PLUGIN_NAME)
        os.chmod(root, 0o700)
        conn = plugin_db(PLUGIN_NAME)
        db_file = root / "data.db"
        if db_file.exists():
            os.chmod(db_file, 0o600)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(db_path.parent, 0o700)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = None
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    # A different epoch may belong to another live process. Opening storage
    # must not invalidate its capture. Hook queries require our own epoch,
    # so restarted (or forked) processes still cannot resume old captures.
    conn.commit()
    return conn


def new_attempt_id() -> str:
    attempt_id = "att_" + secrets.token_hex(12)
    if not valid_id(attempt_id):
        raise RuntimeError("generated attempt id failed validation")
    return attempt_id


def insert_attempt(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    task_id: str,
    host_task_id: str | None,
    host_session_id: str | None,
    digest: str,
    spec: dict[str, Any],
    started_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO attempts (
            attempt_id, task_id, host_task_id, host_session_id, criteria_digest,
            spec_json, started_at, state, capture_epoch
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)
        """,
        (attempt_id, task_id, host_task_id, host_session_id, digest, json.dumps(spec), started_at, current_epoch()),
    )
    if host_context_complete(host_task_id, host_session_id):
        for check in spec["checks"]:
            if check["kind"] != "test":
                continue
            conn.execute(
                """
                INSERT INTO armed (
                    attempt_id, check_id, command, workdir, expected_exit, contains,
                    timeout_seconds, criteria_digest, host_task_id, host_session_id,
                    armed_at, epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id, check["id"], check["command"], check["workdir"],
                    check["expected_exit"], check.get("contains"), check["timeout"],
                    digest, host_task_id, host_session_id, started_at, current_epoch(),
                ),
            )


def open_attempt_for_host(conn: sqlite3.Connection, host_task_id: str, host_session_id: str) -> str | None:
    row = conn.execute(
        """
        SELECT attempt_id FROM attempts
        WHERE host_task_id = ? AND host_session_id = ? AND state = 'open' AND capture_epoch = ?
        LIMIT 1
        """,
        (host_task_id, host_session_id, current_epoch()),
    ).fetchone()
    return None if row is None else str(row["attempt_id"])


def get_attempt(conn: sqlite3.Connection, attempt_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()


def list_attempts(conn: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM attempts WHERE task_id = ? ORDER BY started_at ASC, attempt_id ASC",
        (task_id,),
    ))


def seal_attempt(conn: sqlite3.Connection, attempt_id: str, receipt: dict[str, Any], sealed_at: str) -> None:
    conn.execute(
        """
        UPDATE attempts
        SET state = 'sealed', sealed_at = ?, receipt_json = ?
        WHERE attempt_id = ? AND state = 'open'
        """,
        (sealed_at, json.dumps(receipt), attempt_id),
    )
    conn.execute("UPDATE armed SET expired = 1 WHERE attempt_id = ?", (attempt_id,))


def mark_identity_conflict(conn: sqlite3.Connection, attempt_id: str) -> None:
    conn.execute("UPDATE attempts SET identity_conflict = 1 WHERE attempt_id = ?", (attempt_id,))


def bind_host_if_missing(
    conn: sqlite3.Connection, attempt_id: str, host_task_id: str | None, host_session_id: str | None,
) -> None:
    row = get_attempt(conn, attempt_id)
    if row is None:
        return
    if row["host_task_id"] or row["host_session_id"]:
        if (host_task_id and row["host_task_id"] and host_task_id != row["host_task_id"]) or (
            host_session_id and row["host_session_id"] and host_session_id != row["host_session_id"]
        ):
            mark_identity_conflict(conn, attempt_id)
        return
    if host_task_id or host_session_id:
        conn.execute(
            "UPDATE attempts SET host_task_id = ?, host_session_id = ? WHERE attempt_id = ?",
            (host_task_id, host_session_id, attempt_id),
        )


def match_armed(
    conn: sqlite3.Connection, *, command: str, workdir: str, host_task_id: str | None, host_session_id: str | None,
) -> list[sqlite3.Row]:
    if not host_context_complete(host_task_id, host_session_id):
        return []
    return list(conn.execute(
        """
        SELECT * FROM armed
        WHERE expired = 0 AND epoch = ? AND command = ? AND workdir = ?
          AND host_task_id = ? AND host_session_id = ?
        """,
        (current_epoch(), command, workdir, host_task_id, host_session_id),
    ))


def insert_pre_event(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO pre_events (
            tool_call_id, attempt_id, check_id, command, workdir, host_task_id,
            host_session_id, started_at, epoch
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["tool_call_id"], row["attempt_id"], row["check_id"], row["command"],
            row["workdir"], row["host_task_id"], row["host_session_id"], row["started_at"],
            current_epoch(),
        ),
    )


def get_pre_event(conn: sqlite3.Connection, tool_call_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM pre_events WHERE tool_call_id = ? AND expired = 0 AND epoch = ?",
        (tool_call_id, current_epoch()),
    ).fetchone()


def insert_observation(conn: sqlite3.Connection, tool_call_id: str, attempt_id: str, check_id: str, payload: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO observations (tool_call_id, attempt_id, check_id, payload_json, observed_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (tool_call_id, attempt_id, check_id, json.dumps(payload), utc_now()),
    )


def observation_for(conn: sqlite3.Connection, attempt_id: str, check_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT payload_json FROM observations
        WHERE attempt_id = ? AND check_id = ?
        ORDER BY observed_at ASC LIMIT 1
        """,
        (attempt_id, check_id),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["payload_json"])


def transaction(db_path: Path | None = None):
    class _Txn:
        def __enter__(self) -> sqlite3.Connection:
            _LOCK.acquire()
            try:
                self.conn = connect(db_path)
                self.conn.execute("BEGIN IMMEDIATE")
                return self.conn
            except Exception:
                _LOCK.release()
                raise

        def __exit__(self, exc_type, exc, tb) -> None:
            try:
                if exc_type is None:
                    self.conn.commit()
                else:
                    self.conn.rollback()
            finally:
                self.conn.close()
                _LOCK.release()

    return _Txn()
