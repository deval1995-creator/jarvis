import asyncio
from types import SimpleNamespace

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from core import approvals, audit, db
from core.api import create_app
from tests.fakes import FakeBackend

SEND = "mcp__claude_ai_Gmail__send_message"
ARGS = {"to": ["sam@example.com"], "subject": "Lunch", "body": "Friday at noon?"}


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "jarvis.db")
    yield c
    c.close()


async def ask_then(broker, decide_with):
    """Start a request, let the card appear, then decide it."""
    task = asyncio.create_task(broker.request(SEND, ARGS, conversation_id="c1", untrusted=False))
    while not broker.list():
        await asyncio.sleep(0.01)
    [card] = broker.list()
    await decide_with(card)
    return card, await task


def test_approve_runs_exactly_the_stored_arguments(conn):
    events = []

    async def on_event(e):
        events.append(e["type"])

    broker = approvals.ApprovalBroker(conn, on_event=on_event)
    card, result = asyncio.run(ask_then(broker, lambda c: broker.decide(c["id"], True)))
    assert isinstance(result, PermissionResultAllow) and result.updated_input == ARGS
    assert card["arguments_hash"] == approvals.arguments_hash(ARGS)
    assert broker.get(card["id"])["status"] == "approved"
    assert events == ["approval_request", "approval_decided"]


def test_reject_denies(conn):
    broker = approvals.ApprovalBroker(conn)
    _, result = asyncio.run(ask_then(broker, lambda c: broker.decide(c["id"], False)))
    assert isinstance(result, PermissionResultDeny) and "rejected" in result.message


def test_no_answer_means_no(conn):
    broker = approvals.ApprovalBroker(conn, timeout=0.05)
    result = asyncio.run(broker.request(SEND, ARGS, conversation_id="c1", untrusted=False))
    assert isinstance(result, PermissionResultDeny)
    assert broker.list("expired")


def test_cancel_all_on_interrupt(conn):
    broker = approvals.ApprovalBroker(conn)

    async def cancel(card):
        assert await broker.cancel_all() == 1

    card, result = asyncio.run(ask_then(broker, cancel))
    assert isinstance(result, PermissionResultDeny)
    assert broker.get(card["id"])["status"] == "cancelled"


def test_tampered_record_is_refused(conn):
    broker = approvals.ApprovalBroker(conn)
    evil = '{"to": ["attacker@example.com"], "subject": "x", "body": "y"}'

    async def tamper_then_approve(card):
        conn.execute("update approvals set arguments = ? where id = ?", (evil, card["id"]))
        await broker.decide(card["id"], True)

    _, result = asyncio.run(ask_then(broker, tamper_then_approve))
    assert isinstance(result, PermissionResultDeny)


def test_decide_twice_refused(conn):
    broker = approvals.ApprovalBroker(conn)
    card, _ = asyncio.run(ask_then(broker, lambda c: broker.decide(c["id"], False)))
    with pytest.raises(approvals.ApprovalError):
        asyncio.run(broker.decide(card["id"], True))


def test_startup_expires_orphans(conn):
    conn.execute(
        "insert into approvals (id, tool, arguments, arguments_hash, created_at) values ('old', 't', '{}', 'h', 'x')"
    )
    assert approvals.ApprovalBroker(conn).expire_orphans() == 1


# ----- the gate and audit, as wired in the Core -----


@pytest.fixture
def app(conn):
    async def no_review(*args):
        return {"memories": [], "summary": ""}, None

    return create_app(conn=conn, backend_factory=FakeBackend, token="t", review_runner=no_review, connectors=True)


def run_tool_call(app, tool, args, decide=None):
    hooks, broker = app.state.tool_hooks, app.state.broker

    async def scenario():
        await hooks["before"](tool, args, "use-1", "sess-1")
        gate = asyncio.create_task(hooks["gate"](tool, args, SimpleNamespace(tool_use_id="use-1")))
        if decide is not None:
            while not broker.list():
                await asyncio.sleep(0.01)
            await broker.decide(broker.list()[0]["id"], decide)
        result = await gate
        if result.behavior == "allow":
            await hooks["after"]("use-1", "sent", None)
        return result

    return asyncio.run(scenario())


def test_gate_approval_flow_is_audited(app, conn):
    assert run_tool_call(app, SEND, ARGS, decide=True).behavior == "allow"
    [row] = audit.recent(conn)
    assert (row["tool"], row["status"], row["result_summary"]) == (SEND, "done", "sent")
    assert row["approval_id"] == app.state.broker.list("approved")[0]["id"]


def test_gate_rejection_is_audited(app, conn):
    assert run_tool_call(app, SEND, ARGS, decide=False).behavior == "deny"
    assert audit.recent(conn)[0]["status"] == "denied"


def test_gate_refuses_unlisted_tools_without_a_card(app, conn):
    result = run_tool_call(app, "mcp__claude_ai_Slack__slack_send_message", {"text": "hi"})
    assert result.behavior == "deny" and app.state.broker.list() == []
    assert audit.recent(conn)[0]["status"] == "denied"


def test_acting_alone_does_not_mark_untrusted(app, conn):
    run_tool_call(app, SEND, ARGS, decide=False)
    assert not app.state.manager.untrusted
    assert app.state.broker.list("rejected")[0]["untrusted"] == 0


def test_reading_email_marks_conversation_untrusted_and_cards_show_it(app, conn):
    asyncio.run(app.state.tool_hooks["before"]("mcp__claude_ai_Gmail__get_thread", {"id": "x"}, "u0", "s"))
    assert app.state.manager.untrusted
    run_tool_call(app, SEND, ARGS, decide=False)
    assert app.state.broker.list("rejected")[0]["untrusted"] == 1


def test_audit_prune(conn):
    conn.execute(
        "insert into tool_audit (tool, status, created_at) values ('t', 'done', '2000-01-01T00:00:00+00:00')"
    )
    audit.started(conn, tool="t2", arguments={}, tool_use_id="u", session_id=None, conversation_id=None)
    assert audit.prune(conn) == 1 and len(audit.recent(conn)) == 1
