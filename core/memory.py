"""Long-term memory: proposals, approval, recall.

Claude can only *propose* (remember / update / forget). A change reaches the
memories table only when Deval approves it, so nothing is stored silently and
a prompt-injected email can't plant a memory on its own.

Memories in local-only categories (health, finances by default) are never
returned to Claude by recall or get_profile.
"""

from __future__ import annotations

import json
import re
import sqlite3

from core.db import utc_now

KINDS = ("fact", "preference", "person", "decision", "journal")


class MemoryError(ValueError):
    pass


# ---------- settings ----------


def local_only_categories(conn: sqlite3.Connection) -> list[str]:
    row = conn.execute("select value from settings where key = 'local_only_categories'").fetchone()
    return json.loads(row[0]) if row else []


def set_local_only_categories(conn: sqlite3.Connection, categories: list[str]) -> None:
    value = json.dumps(sorted({c.strip().lower() for c in categories if c.strip()}))
    conn.execute(
        "insert into settings (key, value) values ('local_only_categories', ?)"
        " on conflict(key) do update set value = excluded.value",
        (value,),
    )


def _visible_clause(conn: sqlite3.Connection) -> tuple[str, list]:
    local = local_only_categories(conn)
    if not local:
        return "", []
    marks = ",".join("?" * len(local))
    return f" and (m.category is null or lower(m.category) not in ({marks}))", local


# ---------- proposals ----------


def _norm_category(category: str | None) -> str | None:
    return category.strip().lower() if category and category.strip() else None


def _get_memory(conn: sqlite3.Connection, memory_id: int) -> sqlite3.Row:
    row = conn.execute("select * from memories where id = ?", (memory_id,)).fetchone()
    if row is None or row["archived"]:
        raise MemoryError(f"no active memory with id {memory_id}")
    return row


def propose(
    conn: sqlite3.Connection,
    action: str,
    *,
    text: str | None = None,
    memory_id: int | None = None,
    kind: str | None = None,
    category: str | None = None,
    importance: int | None = None,
    confidence: float | None = None,
    people: list[str] | None = None,
    reason: str | None = None,
    source: str | None = None,
    conversation_id: str | None = None,
    origin: str = "chat",
) -> dict:
    """Record a pending change. Returns the proposal as a dict."""
    if action == "remember":
        if not text or not text.strip():
            raise MemoryError("remember needs text")
        kind = kind or "fact"
        if kind not in KINDS:
            raise MemoryError(f"kind must be one of {KINDS}")
    elif action == "update":
        if memory_id is None or not text or not text.strip():
            raise MemoryError("update needs memory_id and the new text")
        old = _get_memory(conn, memory_id)
        kind = kind or old["kind"]
        category = category if category is not None else old["category"]
        importance = importance if importance is not None else old["importance"]
    elif action == "forget":
        if memory_id is None:
            raise MemoryError("forget needs memory_id")
        old = _get_memory(conn, memory_id)
        text = old["text"]  # shown on the card so Deval sees what goes
    else:
        raise MemoryError(f"unknown action {action!r}")

    if importance is not None and not 1 <= int(importance) <= 5:
        raise MemoryError("importance must be 1..5")
    if confidence is not None and not 0.0 < float(confidence) <= 1.0:
        raise MemoryError("confidence must be in (0, 1]")

    # Don't stack duplicate pending proposals for the same thing.
    dup = conn.execute(
        "select id from memory_proposals where status = 'pending' and action = ?"
        " and coalesce(memory_id, -1) = coalesce(?, -1) and lower(coalesce(text, '')) = lower(?)",
        (action, memory_id, (text or "").strip()),
    ).fetchone()
    if dup:
        return get_proposal(conn, dup[0])

    cur = conn.execute(
        "insert into memory_proposals (action, memory_id, kind, category, text, importance, confidence,"
        " people, reason, source, conversation_id, origin, created_at)"
        " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            action,
            memory_id,
            kind,
            _norm_category(category),
            text.strip() if text else None,
            importance,
            confidence,
            json.dumps(people or []),
            reason,
            source,
            conversation_id,
            origin,
            utc_now(),
        ),
    )
    return get_proposal(conn, cur.lastrowid)


def get_proposal(conn: sqlite3.Connection, proposal_id: int) -> dict:
    row = conn.execute("select * from memory_proposals where id = ?", (proposal_id,)).fetchone()
    if row is None:
        raise MemoryError(f"no proposal {proposal_id}")
    d = dict(row)
    d["people"] = json.loads(d["people"] or "[]")
    return d


def list_proposals(conn: sqlite3.Connection, status: str = "pending") -> list[dict]:
    rows = conn.execute(
        "select id from memory_proposals where status = ? order by created_at, id", (status,)
    ).fetchall()
    return [get_proposal(conn, r[0]) for r in rows]


def _link_people(conn: sqlite3.Connection, memory_id: int, names: list[str]) -> None:
    for name in {n.strip() for n in names if n and n.strip()}:
        conn.execute("insert or ignore into people (name) values (?)", (name,))
        person_id = conn.execute("select id from people where name = ?", (name,)).fetchone()[0]
        conn.execute(
            "insert or ignore into memory_people (memory_id, person_id) values (?, ?)", (memory_id, person_id)
        )


def _insert_memory(conn: sqlite3.Connection, p: dict, replaces_id: int | None = None) -> int:
    now = utc_now()
    cur = conn.execute(
        "insert into memories (kind, category, text, importance, confidence, source, source_id,"
        " replaces_id, valid_from, created_at, updated_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            p["kind"] or "fact",
            p["category"],
            p["text"],
            p["importance"] or 3,
            p["confidence"] or 1.0,
            p["source"],
            p["conversation_id"],
            replaces_id,
            now,
            now,
            now,
        ),
    )
    memory_id = cur.lastrowid
    _link_people(conn, memory_id, p["people"])
    return memory_id


def _archive(conn: sqlite3.Connection, memory_id: int) -> None:
    now = utc_now()
    conn.execute(
        "update memories set archived = 1, valid_to = ?, updated_at = ? where id = ?", (now, now, memory_id)
    )


def approve(conn: sqlite3.Connection, proposal_id: int, edited_text: str | None = None) -> dict:
    """Apply a pending proposal. Deval may tweak the text before approving."""
    p = get_proposal(conn, proposal_id)
    if p["status"] != "pending":
        raise MemoryError(f"proposal {proposal_id} is already {p['status']}")
    if edited_text is not None and edited_text.strip():
        p["text"] = edited_text.strip()
        p["confidence"] = 1.0  # Deval wrote it, so it's stated, not inferred

    conn.execute("begin")
    try:
        if p["action"] == "remember":
            result = _insert_memory(conn, p)
        elif p["action"] == "update":
            _get_memory(conn, p["memory_id"])  # still active?
            _archive(conn, p["memory_id"])
            old_people = [
                r[0]
                for r in conn.execute(
                    "select name from people join memory_people on person_id = people.id where memory_id = ?",
                    (p["memory_id"],),
                )
            ]
            p["people"] = p["people"] or old_people
            result = _insert_memory(conn, p, replaces_id=p["memory_id"])
        else:  # forget
            _get_memory(conn, p["memory_id"])
            _archive(conn, p["memory_id"])
            result = p["memory_id"]
        conn.execute(
            "update memory_proposals set status = 'approved', decided_at = ?, result_memory_id = ?, text = ?"
            " where id = ?",
            (utc_now(), result, p["text"], proposal_id),
        )
        conn.execute("commit")
    except Exception:
        conn.execute("rollback")
        raise
    return get_proposal(conn, proposal_id)


def reject(conn: sqlite3.Connection, proposal_id: int) -> dict:
    p = get_proposal(conn, proposal_id)
    if p["status"] != "pending":
        raise MemoryError(f"proposal {proposal_id} is already {p['status']}")
    conn.execute(
        "update memory_proposals set status = 'rejected', decided_at = ? where id = ?", (utc_now(), proposal_id)
    )
    return get_proposal(conn, proposal_id)


# ---------- reading ----------


def _row_out(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    d = {k: row[k] for k in ("id", "kind", "category", "text", "importance", "confidence", "pinned", "created_at")}
    d["people"] = [
        r[0]
        for r in conn.execute(
            "select name from people join memory_people on person_id = people.id where memory_id = ?", (row["id"],)
        )
    ]
    return d


def _fts_query(query: str) -> str | None:
    words = re.findall(r"\w+", query.lower())
    stop = {"the", "a", "an", "is", "my", "me", "i", "what", "who", "where", "when", "do", "does", "of", "to", "and"}
    words = [w for w in words if w not in stop] or words
    if not words:
        return None
    return " OR ".join(f'"{w}"*' for w in words[:12])


def recall(
    conn: sqlite3.Connection,
    query: str,
    *,
    category: str | None = None,
    limit: int = 8,
    include_local_only: bool = False,
) -> list[dict]:
    """Full-text search over active memories, best match first."""
    match = _fts_query(query)
    if match is None:
        return []
    where, params = ("", []) if include_local_only else _visible_clause(conn)
    if category:
        where += " and lower(m.category) = ?"
        params.append(category.strip().lower())
    rows = conn.execute(
        "select m.* from memories_fts f join memories m on m.id = f.rowid"
        f" where memories_fts match ? and m.archived = 0{where}"
        " order by bm25(memories_fts) - m.importance * 0.5 - m.pinned * 2 limit ?",
        [match, *params, limit],
    ).fetchall()
    # Names in the query should find memories linked to that person, even if
    # the memory text doesn't repeat the name.
    seen = {r["id"] for r in rows}
    person_rows = conn.execute(
        "select m.* from memories m join memory_people mp on mp.memory_id = m.id join people p on p.id = mp.person_id"
        f" where m.archived = 0 and instr(lower(?), lower(p.name)) > 0{where} limit ?",
        [query, *params, limit],
    ).fetchall()
    rows = list(rows) + [r for r in person_rows if r["id"] not in seen]
    return [_row_out(conn, r) for r in rows[:limit]]


def get_profile(conn: sqlite3.Connection, limit: int = 30) -> dict:
    """The essentials: pinned and important memories, plus known people."""
    where, params = _visible_clause(conn)
    rows = conn.execute(
        f"select m.* from memories m where m.archived = 0 and (m.pinned = 1 or m.importance >= 4){where}"
        " order by m.pinned desc, m.importance desc, m.created_at desc limit ?",
        [*params, limit],
    ).fetchall()
    people = [r[0] for r in conn.execute("select name from people order by name")]
    return {"memories": [_row_out(conn, r) for r in rows], "people": people}


def list_memories(conn: sqlite3.Connection, query: str | None = None, include_archived: bool = False) -> list[dict]:
    """For Deval's memory screen: everything, local-only included."""
    if query:
        return recall(conn, query, limit=100, include_local_only=True)
    rows = conn.execute(
        "select * from memories m where (? or m.archived = 0) order by m.created_at desc limit 500",
        (1 if include_archived else 0,),
    ).fetchall()
    return [_row_out(conn, r) for r in rows]


def edit_memory(conn: sqlite3.Connection, memory_id: int, **fields) -> dict:
    """Deval's own direct edit (no proposal needed: Deval is the reviewer)."""
    _get_memory(conn, memory_id)
    allowed = {"text", "category", "importance", "pinned", "kind"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "category" in sets:
        sets["category"] = _norm_category(sets["category"])
    if "kind" in sets and sets["kind"] not in KINDS:
        raise MemoryError(f"kind must be one of {KINDS}")
    if sets:
        assignments = ", ".join(f"{k} = ?" for k in sets)
        conn.execute(
            f"update memories set {assignments}, confidence = 1.0, updated_at = ? where id = ?",
            [*sets.values(), utc_now(), memory_id],
        )
    return _row_out(conn, _get_memory(conn, memory_id))


def archive_memory(conn: sqlite3.Connection, memory_id: int) -> None:
    _get_memory(conn, memory_id)
    _archive(conn, memory_id)
