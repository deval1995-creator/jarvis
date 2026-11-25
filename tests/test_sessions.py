import asyncio

import pytest

from core import db, store
from core.sessions import SessionManager
from tests.fakes import FailingBackend, FakeBackend


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "jarvis.db")
    yield c
    c.close()


async def collect(gen):
    return [e async for e in gen]


def test_turn_streams_and_saves_both_messages(conn):
    m = SessionManager(conn, FakeBackend)
    events = asyncio.run(collect(m.process_message("hi there", "voice")))

    assert [e["type"] for e in events] == ["start", "delta", "delta", "done"]
    assert events[-1]["text"] == "Hello, Deval."
    msgs = store.get_conversation(conn, m.conversation_id)["messages"]
    assert [(x["role"], x["source"], x["content"]) for x in msgs] == [
        ("user", "voice", "hi there"),
        ("assistant", "voice", "Hello, Deval."),
    ]


def test_usage_and_session_id_recorded(conn):
    m = SessionManager(conn, FakeBackend)
    asyncio.run(collect(m.process_message("hi")))
    row = conn.execute("select * from usage").fetchone()
    assert (row["kind"], row["input_tokens"], row["output_tokens"]) == ("chat", 3, 7)
    assert row["first_token_ms"] is not None
    assert m.status()["claude_session_id"] == m.backend.session_id


def test_restart_resumes_stored_claude_session(conn):
    first = SessionManager(conn, FakeBackend)
    asyncio.run(collect(first.process_message("remember the number 42")))
    stored = first.backend.session_id

    second = SessionManager(conn, FakeBackend)  # simulates restarting the Core
    assert second.conversation_id == first.conversation_id
    assert second.backend.resume == stored


def test_new_input_interrupts_reply_in_progress(conn):
    def slow(resume):
        return FakeBackend(resume, chunks=("one ", "two ", "three ", "four"), delay=0.05)

    m = SessionManager(conn, slow)

    async def scenario():
        first = asyncio.create_task(collect(m.process_message("count slowly")))
        await asyncio.sleep(0.08)  # let it get a word or so out
        second = await collect(m.process_message("stop, new question"))
        return await first, second

    first, second = asyncio.run(scenario())
    assert first[-1]["type"] == "interrupted"
    assert first[-1]["text"] != "one two three four"
    assert second[-1]["type"] == "done"
    roles = [x["role"] for x in store.get_conversation(conn, m.conversation_id)["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]  # partial reply kept


def test_backend_crash_reports_error_and_keeps_partial(conn):
    m = SessionManager(conn, FailingBackend)
    events = asyncio.run(collect(m.process_message("hi")))
    assert events[-1]["type"] == "error"
    assert "CLI process exited" in events[-1]["error"]
    assert store.get_conversation(conn, m.conversation_id)["messages"][-1]["content"] == "Partial"
    assert not m.busy  # lock released, next turn can run


def test_new_conversation_starts_fresh_session(conn):
    m = SessionManager(conn, FakeBackend)
    asyncio.run(collect(m.process_message("hi")))
    old_id, old_backend = m.conversation_id, m.backend
    asyncio.run(m.new_conversation())
    assert m.conversation_id != old_id
    assert old_backend.closed
    assert m.backend.resume is None
    assert store.get_conversation(conn, old_id)["ended_at"] is not None


def test_rejects_bad_input(conn):
    m = SessionManager(conn, FakeBackend)
    with pytest.raises(ValueError):
        asyncio.run(collect(m.process_message("   ")))
    with pytest.raises(ValueError):
        asyncio.run(collect(m.process_message("hi", "telepathy")))
