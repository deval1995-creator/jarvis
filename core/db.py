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
    # v2: tokens written to the prompt cache also count against your limits
    """
    alter table usage add column cache_creation_tokens integer;
    """,
    # v3: long-term memory. Claude never writes memories directly: every
    # remember / update / forget is a proposal that Deval approves or rejects.
    """
    create table memories (
        id          integer primary key,
        kind        text not null check (kind in ('fact', 'preference', 'person', 'decision', 'journal')),
        category    text,                 -- trading, study, career, projects, home, health, finances...
        text        text not null,
        importance  integer not null default 3 check (importance between 1 and 5),
        confidence  real not null default 1.0,  -- below 1.0 = inferred, not stated
        source      text,                 -- typed | voice | routine | document | manual
        source_id   text,                 -- conversation it came from
        pinned      integer not null default 0,
        archived    integer not null default 0,
        replaces_id integer references memories(id),
        valid_from  text not null,
        valid_to    text,                 -- set when a newer version replaces it
        created_at  text not null,
        updated_at  text not null
    );
    create index memories_active on memories(archived, category);
    create table people (id integer primary key, name text not null unique collate nocase);
    create table memory_people (
        memory_id integer not null references memories(id),
        person_id integer not null references people(id),
        primary key (memory_id, person_id)
    );
    create virtual table memories_fts using fts5(text, content='memories', content_rowid='id');
    create trigger memories_ai after insert on memories begin
        insert into memories_fts(rowid, text) values (new.id, new.text);
    end;
    create trigger memories_ad after delete on memories begin
        insert into memories_fts(memories_fts, rowid, text) values ('delete', old.id, old.text);
    end;
    create trigger memories_au after update of text on memories begin
        insert into memories_fts(memories_fts, rowid, text) values ('delete', old.id, old.text);
        insert into memories_fts(rowid, text) values (new.id, new.text);
    end;
    create table memory_proposals (
        id              integer primary key,
        action          text not null check (action in ('remember', 'update', 'forget')),
        memory_id       integer references memories(id),  -- for update / forget
        kind            text,
        category        text,
        text            text,
        importance      integer,
        confidence      real,
        people          text,             -- JSON list of names
        reason          text,             -- why Claude proposed it
        source          text,
        conversation_id text,
        origin          text not null default 'chat',  -- chat | review
        status          text not null default 'pending' check (status in ('pending', 'approved', 'rejected')),
        result_memory_id integer,
        created_at      text not null,
        decided_at      text
    );
    create index memory_proposals_status on memory_proposals(status, created_at);
    create table settings (key text primary key, value text not null);
    insert into settings (key, value) values ('local_only_categories', '["health", "finances"]');
    """,
    # v4: auto-approve. Deval reviews only critical proposals; the rest save at once.
    """
    alter table memory_proposals add column review_reason text;  -- why it needs Deval, or null
    alter table memory_proposals add column decided_by text;     -- auto | deval
    insert into settings (key, value) values ('auto_approve', 'true');
    """,
    # v5: end-of-conversation memory review only looks at messages it hasn't seen.
    """
    alter table conversations add column reviewed_through integer not null default 0;  -- last message id reviewed
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
    # check_same_thread=False: the Core touches the db only from its event-loop thread,
    # but test clients and uvicorn may open it on a different thread than they use it.
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma journal_mode = wal")
    conn.execute("pragma foreign_keys = on")
    conn.execute("pragma busy_timeout = 5000")
    migrate(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("pragma user_version").fetchone()[0]


def _split_sql(sql: str) -> list[str]:
    """Split a migration into statements, keeping trigger bodies whole."""
    statements, buf = [], ""
    for part in sql.split(";"):
        buf += part + ";"
        if sqlite3.complete_statement(buf):
            if buf.strip().strip(";").strip():
                statements.append(buf.strip())
            buf = ""
    if buf.strip().strip(";").strip():
        statements.append(buf.strip())
    return statements


def migrate(conn: sqlite3.Connection) -> None:
    current = schema_version(conn)
    for version, sql in enumerate(MIGRATIONS[current:], start=current + 1):
        conn.execute("begin")
        try:
            for statement in _split_sql(sql):
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
        " cache_read_tokens, cache_creation_tokens, cost_usd, duration_ms, first_token_ms, created_at)"
        " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            claude_session_id,
            kind,
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            usage.get("cache_read_input_tokens"),
            usage.get("cache_creation_input_tokens"),
            cost_usd,
            duration_ms,
            first_token_ms,
            utc_now(),
        ),
    )
