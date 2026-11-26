import asyncio

import pytest
from claude_agent_sdk import PermissionResultDeny

from core import connectors, db, memory, memory_review, store
from core.backend import AGENT_HOME, session_options
from core.sessions import SessionManager
from tests.fakes import FakeBackend


def test_tool_names_match_claude_code_naming():
    assert "mcp__claude_ai_Gmail__search_threads" in connectors.ALLOWED_TOOLS
    assert "mcp__claude_ai_Google_Calendar__list_events" in connectors.ALLOWED_TOOLS
    assert "mcp__claude_ai_Gmail__send_message" in connectors.DISALLOWED_TOOLS
    assert "mcp__claude_ai_Google_Calendar__create_event" in connectors.DISALLOWED_TOOLS


def test_no_tool_is_both_allowed_and_denied():
    assert not set(connectors.ALLOWED_TOOLS) & set(connectors.DISALLOWED_TOOLS)


def test_unlisted_tools_are_refused():
    result = asyncio.run(connectors.deny_unlisted("mcp__claude_ai_Slack__slack_send_message", {}, None))
    assert isinstance(result, PermissionResultDeny)


def test_outside_content_detection():
    assert connectors.reads_outside_content("mcp__claude_ai_Gmail__get_thread")
    assert connectors.reads_outside_content("mcp__claude_ai_Google_Calendar__list_events")
    assert not connectors.reads_outside_content("mcp__memory__recall")


def test_session_options_with_connectors():
    o = session_options(allowed_tools=["mcp__memory__recall"], connectors=True)
    assert o.cwd == str(AGENT_HOME)
    assert (AGENT_HOME / "CLAUDE.md").exists()
    from core.backend import JARVIS_DIR

    assert JARVIS_DIR not in AGENT_HOME.parents  # outside the repo
    assert not o.strict_mcp_config
    assert "mcp__memory__recall" in o.allowed_tools and "mcp__claude_ai_Gmail__get_thread" in o.allowed_tools
    assert "mcp__claude_ai_Gmail__send_message" in o.disallowed_tools
    assert o.can_use_tool is connectors.deny_unlisted
    assert o.tools == []  # no files, shell or web


def test_session_options_without_connectors_stays_slim():
    o = session_options(connectors=False)
    assert o.strict_mcp_config and o.env == {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}


def test_before_tool_hook_runs_with_tool_name():
    seen = []

    async def before(name):
        seen.append(name)

    o = session_options(connectors=True, before_tool=before)
    hook = o.hooks["PreToolUse"][0].hooks[0]
    assert asyncio.run(hook({"tool_name": "mcp__claude_ai_Gmail__get_thread"}, "t1", None)) == {}
    assert seen == ["mcp__claude_ai_Gmail__get_thread"]


class FakeClient:
    def __init__(self, servers):
        self.servers = {s: st for s, st in servers}
        self.toggles = []

    async def get_mcp_status(self):
        return {"mcpServers": [{"name": n, "status": s} for n, s in self.servers.items()]}

    async def toggle_mcp_server(self, name, enabled):
        self.toggles.append((name, enabled))
        self.servers[name] = "connected" if enabled else "disabled"


def test_select_servers_keeps_only_gmail_calendar_and_memory():
    client = FakeClient([
        ("claude.ai Gmail", "connected"),
        ("claude.ai Google Calendar", "disabled"),
        ("claude.ai Slack", "connected"),
        ("claude.ai ElevenLabs", "connected"),
        ("claude.ai Google Drive", "needs-auth"),
        ("memory", "connected"),
    ])
    status = asyncio.run(connectors.select_servers(client))
    assert status["claude.ai Slack"] == "disabled" and status["claude.ai ElevenLabs"] == "disabled"
    assert status["claude.ai Google Calendar"] == "connected"
    assert status["memory"] == "connected" and status["claude.ai Google Drive"] == "needs-auth"


# ----- untrusted conversations -----


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "jarvis.db")
    yield c
    c.close()


def test_untrusted_flag_is_per_conversation(conn):
    m = SessionManager(conn, FakeBackend)
    assert not m.untrusted
    m.mark_untrusted()
    assert m.untrusted and m.status()["untrusted"]
    asyncio.run(m.new_conversation())
    assert not m.untrusted


def test_review_of_untrusted_conversation_needs_approval(conn):
    cid = store.create_conversation(conn)
    conn.execute("update conversations set untrusted = 1 where id = ?", (cid,))
    store.add_message(conn, cid, "user", "typed", "Summarise the email from my landlord about the lease renewal.")
    store.add_message(conn, cid, "assistant", "typed", "The landlord says the lease renews on March 1.")

    async def runner(system_prompt, prompt):
        return {"memories": [{"text": "Deval's lease renews on March 1.", "kind": "fact", "importance": 3,
                              "confidence": 1.0, "reason": "from email"}], "summary": "Lease email."}, None

    r = asyncio.run(memory_review.review_conversation(conn, cid, runner))
    assert r.saved == [] and len(r.pending) == 1
    assert "email" in memory.list_proposals(conn)[0]["review_reason"]
