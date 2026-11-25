"""Conversations and messages: the Core's record of everything said."""

from __future__ import annotations

import sqlite3
import uuid

from core.db import utc_now


def create_conversation(conn: sqlite3.Connection, title: str | None = None) -> str:
    conversation_id = uuid.uuid4().hex
    conn.execute(
        "insert into conversations (id, title, started_at) values (?, ?, ?)",
        (conversation_id, title, utc_now()),
    )
    return conversation_id


def active_conversation(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The most recent conversation that hasn't been ended."""
    return conn.execute(
        "select * from conversations where ended_at is null order by started_at desc limit 1"
    ).fetchone()


def end_conversation(conn: sqlite3.Connection, conversation_id: str, summary: str | None = None) -> None:
    conn.execute(
        "update conversations set ended_at = ?, summary = coalesce(?, summary) where id = ?",
        (utc_now(), summary, conversation_id),
    )


def set_claude_session_id(conn: sqlite3.Connection, conversation_id: str, session_id: str) -> None:
    conn.execute(
        "update conversations set claude_session_id = ? where id = ?",
        (session_id, conversation_id),
    )


def add_message(conn: sqlite3.Connection, conversation_id: str, role: str, source: str, content: str) -> int:
    cur = conn.execute(
        "insert into messages (conversation_id, role, source, content, created_at) values (?, ?, ?, ?, ?)",
        (conversation_id, role, source, content, utc_now()),
    )
    # Title a conversation after its first user message.
    if role == "user":
        conn.execute(
            "update conversations set title = ? where id = ? and title is null",
            (content[:80], conversation_id),
        )
    return cur.lastrowid


def get_conversation(conn: sqlite3.Connection, conversation_id: str) -> dict | None:
    row = conn.execute("select * from conversations where id = ?", (conversation_id,)).fetchone()
    if row is None:
        return None
    messages = conn.execute(
        "select id, role, source, content, created_at from messages"
        " where conversation_id = ? order by created_at, id",
        (conversation_id,),
    ).fetchall()
    return {**dict(row), "messages": [dict(m) for m in messages]}


def list_conversations(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "select c.id, c.title, c.started_at, c.ended_at,"
        " (select count(*) from messages m where m.conversation_id = c.id) as message_count"
        " from conversations c order by c.started_at desc limit ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def usage_since(conn: sqlite3.Connection, since_iso: str) -> dict:
    row = conn.execute(
        "select count(*) as turns, coalesce(sum(input_tokens), 0) as input_tokens,"
        " coalesce(sum(output_tokens), 0) as output_tokens,"
        " coalesce(sum(cache_read_tokens), 0) as cache_read_tokens,"
        " coalesce(sum(cache_creation_tokens), 0) as cache_creation_tokens"
        " from usage where created_at >= ?",
        (since_iso,),
    ).fetchone()
    return dict(row)
