"""SQLite storage for Jarvis Core.

The live database lives in %LOCALAPPDATA%\\Jarvis, never in a synced folder:
OneDrive can corrupt a live WAL database or fork it into conflict copies.
Schema changes are numbered migrations tracked with PRAGMA user_version.
All timestamps are UTC ISO-8601 strings; convert only for display.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Each entry moves the schema from version N to N+1. Never edit a shipped
# migration; append a new one instead.
MIGRATIONS: list[str] = [
    # v1: conversations, messages and usage tracking
    """
    create table conversations (
        id                text primary key,
        title             text,
        started_at        text not null,
        ended_at          text,
        summary           text,
        claude_session_id text
    );
    create table messages (
        id              integer primary key,
        conversation_id text not null references conversations(id),
        role            text not null check (role in ('user', 'assistant', 'system')),
        source          text not null check (source in ('typed', 'voice', 'routine', 'discord')),
        content         text not null,
        created_at      text not null
    );
    create index messages_conversation_created on messages(conversation_id, created_at);
    create table usage (
        id                integer primary key,
        claude_session_id text,
        kind              text not null,   -- chat | routine | background | test
        input_tokens      integer,
        output_tokens     integer,
        cache_read_tokens integer,
        cost_usd          real,            -- API-equivalent cost, for comparison only on Pro
        duration_ms       integer,
        first_token_ms    integer,
        created_at        text not null
    );
    """,
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def default_db_path() -> Path:
    base = os.environ.get("JARVIS_DATA_DIR") or os.path.join(
        os.environ.get("LOCALAPPDATA", str(Path.home())), "Jarvis"
    )
    return Path(base) / "jarvis.db"


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open the database, apply pragmas and run any pending migrations."""
    path = Path(path) if path else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit; explicit transactions below
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode = wal")
    conn.execute("pragma foreign_keys = on")
    conn.execute("pragma busy_timeout = 5000")
    migrate(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("pragma user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> None:
    current = schema_version(conn)
    for version, sql in enumerate(MIGRATIONS[current:], start=current + 1):
        conn.execute("begin")
        try:
            for statement in sql.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(f"pragma user_version = {version}")
            conn.execute("commit")
        except Exception:
            conn.execute("rollback")
            raise


def record_usage(
    conn: sqlite3.Connection,
    *,
    kind: str,
    claude_session_id: str | None,
    usage: dict | None,
    cost_usd: float | None,
    duration_ms: int | None,
    first_token_ms: int | None,
) -> None:
    usage = usage or {}
    conn.execute(
        "insert into usage (claude_session_id, kind, input_tokens, output_tokens,"
        " cache_read_tokens, cost_usd, duration_ms, first_token_ms, created_at)"
        " values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            claude_session_id,
            kind,
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            usage.get("cache_read_input_tokens"),
            cost_usd,
            duration_ms,
            first_token_ms,
            utc_now(),
        ),
    )
