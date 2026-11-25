import sqlite3

import pytest

from core import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "jarvis.db")
    yield c
    c.close()


def test_migrates_to_latest_version(conn):
    assert db.schema_version(conn) == len(db.MIGRATIONS)


def test_wal_and_foreign_keys_on(conn):
    assert conn.execute("pragma journal_mode").fetchone()[0] == "wal"
    assert conn.execute("pragma foreign_keys").fetchone()[0] == 1


def test_reconnect_does_not_rerun_migrations(tmp_path):
    path = tmp_path / "jarvis.db"
    db.connect(path).close()
    c = db.connect(path)  # would fail with "table already exists" if re-run
    assert db.schema_version(c) == len(db.MIGRATIONS)
    c.close()


def test_message_requires_existing_conversation(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "insert into messages (conversation_id, role, source, content, created_at) values (?,?,?,?,?)",
            ("missing", "user", "typed", "hi", db.utc_now()),
        )


def test_record_usage(conn):
    db.record_usage(
        conn,
        kind="test",
        claude_session_id="s1",
        usage={"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100},
        cost_usd=0.001,
        duration_ms=1200,
        first_token_ms=800,
    )
    row = conn.execute("select * from usage").fetchone()
    assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"]) == (10, 5, 100)
    assert row["first_token_ms"] == 800
