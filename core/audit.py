"""Audit log: one row per tool call, from request to result.

Rows hold full arguments and a result summary (which can include email text),
so they are pruned after RETENTION_DAYS.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from core.db import utc_now

RETENTION_DAYS = 30
SUMMARY_CHARS = 500


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def started(conn: sqlite3.Connection, *, tool: str, arguments: dict, tool_use_id: str | None,
            session_id: str | None, conversation_id: str | None) -> int:
    cur = conn.execute(
        "insert into tool_audit (conversation_id, session_id, tool_use_id, tool, arguments, status, created_at)"
        " values (?, ?, ?, ?, ?, 'requested', ?)",
        (conversation_id, session_id, tool_use_id, tool, _dump(arguments), utc_now()),
    )
    return cur.lastrowid


def set_status(conn: sqlite3.Connection, tool_use_id: str | None, status: str, approval_id: str | None = None) -> None:
    if not tool_use_id:
        return
    conn.execute(
        "update tool_audit set status = ?, approval_id = coalesce(?, approval_id) where tool_use_id = ?",
        (status, approval_id, tool_use_id),
    )


def finished(conn: sqlite3.Connection, tool_use_id: str | None, *, result=None, error: str | None = None) -> None:
    if not tool_use_id:
        return
    text = error if error is not None else (result if isinstance(result, str) else _dump(result))
    conn.execute(
        "update tool_audit set status = ?, result_summary = ?, finished_at = ? where tool_use_id = ?",
        ("failed" if error is not None else "done", (text or "")[:SUMMARY_CHARS], utc_now(), tool_use_id),
    )


def recent(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    rows = conn.execute("select * from tool_audit order by id desc limit ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def prune(conn: sqlite3.Connection, days: int = RETENTION_DAYS) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="milliseconds")
    return conn.execute("delete from tool_audit where created_at < ?", (cutoff,)).rowcount
