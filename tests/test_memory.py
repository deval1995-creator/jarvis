import asyncio
import json

import pytest

from core import db, memory
from core.memory_mcp import build_memory_server


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "jarvis.db")
    yield c
    c.close()


def remember(conn, text, **kw):
    p = memory.propose(conn, "remember", text=text, **kw)
    return memory.approve(conn, p["id"])["result_memory_id"]


def test_proposal_is_not_a_memory_until_approved(conn):
    p = memory.propose(conn, "remember", text="Deval's wife is Asha.", kind="person", people=["Asha"])
    assert p["status"] == "pending"
    assert memory.recall(conn, "wife") == []

    memory.approve(conn, p["id"])
    [m] = memory.recall(conn, "wife")
    assert m["text"] == "Deval's wife is Asha." and m["people"] == ["Asha"]


def test_rejected_proposal_never_stored(conn):
    p = memory.propose(conn, "remember", text="Deval likes jazz.")
    memory.reject(conn, p["id"])
    assert memory.recall(conn, "jazz") == []
    with pytest.raises(memory.MemoryError):
        memory.approve(conn, p["id"])


def test_approve_with_edit_uses_deval_text_at_full_confidence(conn):
    p = memory.propose(conn, "remember", text="Deval probably lives in Shelbyville.", confidence=0.6)
    memory.approve(conn, p["id"], edited_text="Deval lives in Springfield.")
    [m] = memory.recall(conn, "lives")
    assert m["text"] == "Deval lives in Springfield." and m["confidence"] == 1.0


def test_update_versions_the_memory(conn):
    old_id = remember(conn, "Deval trades the 9 EMA setup on 5-minute charts.", category="trading")
    p = memory.propose(conn, "update", memory_id=old_id, text="Deval trades the 9 EMA setup on 2-minute charts.")
    memory.approve(conn, p["id"])
    [m] = memory.recall(conn, "EMA setup")
    assert "2-minute" in m["text"] and m["category"] == "trading"
    old = conn.execute("select archived, valid_to from memories where id = ?", (old_id,)).fetchone()
    assert old["archived"] == 1 and old["valid_to"] is not None


def test_forget_archives(conn):
    mid = remember(conn, "Deval's gym is Goodlife.")
    memory.approve(conn, memory.propose(conn, "forget", memory_id=mid)["id"])
    assert memory.recall(conn, "gym") == []


def test_forget_unknown_memory_refused(conn):
    with pytest.raises(memory.MemoryError):
        memory.propose(conn, "forget", memory_id=999)


def test_duplicate_pending_proposals_collapse(conn):
    a = memory.propose(conn, "remember", text="Deval's wife is Asha.")
    b = memory.propose(conn, "remember", text="deval's wife is asha.")
    assert a["id"] == b["id"]
    assert len(memory.list_proposals(conn)) == 1


def test_local_only_categories_hidden_from_claude(conn):
    remember(conn, "Deval's blood pressure medication is X.", category="health", importance=5)
    remember(conn, "Deval's favourite stock is NVDA.", category="trading", importance=5)
    assert memory.recall(conn, "medication") == []
    assert [m["category"] for m in memory.get_profile(conn)["memories"]] == ["trading"]
    # Deval's own screen still sees it
    assert len(memory.list_memories(conn, "medication")) == 1

    memory.set_local_only_categories(conn, [])
    assert len(memory.recall(conn, "medication")) == 1


def test_recall_finds_by_person_name(conn):
    remember(conn, "Anniversary is March 3.", people=["Asha"])
    assert [m["text"] for m in memory.recall(conn, "when is Asha's anniversary")] == ["Anniversary is March 3."]


def test_recall_ranks_and_handles_odd_input(conn):
    remember(conn, "Deval studies for the CFA exam.", importance=2)
    remember(conn, "Deval's CFA exam is in August.", importance=5)
    assert memory.recall(conn, "CFA exam")[0]["text"] == "Deval's CFA exam is in August."
    assert memory.recall(conn, '"); drop table memories; --') == []
    assert memory.recall(conn, "   ") == []


def test_profile_includes_pinned_and_important(conn):
    remember(conn, "Low importance thing.", importance=1)
    mid = remember(conn, "Pinned thing.", importance=1)
    memory.edit_memory(conn, mid, pinned=1)
    remember(conn, "Deval's wife is Asha.", importance=5, people=["Asha"])
    profile = memory.get_profile(conn)
    assert [m["text"] for m in profile["memories"]] == ["Pinned thing.", "Deval's wife is Asha."]
    assert profile["people"] == ["Asha"]


# ----- the MCP tools Claude calls -----


def call_tool(server, name, args):
    tool = next(t for t in server["_jarvis_tools"] if t.name == name)
    result = asyncio.run(tool.handler(args))
    return result, json.loads(result["content"][0]["text"]) if not result.get("is_error") else None


@pytest.fixture
def server(conn, monkeypatch):
    """The real memory server, with its tool objects kept so tests can call them."""
    import core.memory_mcp as mm

    real = mm.create_sdk_mcp_server

    def keep_tools(name, version="1.0.0", tools=None):
        return {**real(name=name, version=version, tools=tools), "_jarvis_tools": tools}

    monkeypatch.setattr(mm, "create_sdk_mcp_server", keep_tools)
    proposals = []

    async def on_proposal(p):
        proposals.append(p)

    srv = build_memory_server(conn, context=lambda: {"conversation_id": "c1", "source": "voice"}, on_proposal=on_proposal)
    srv["_proposals"] = proposals
    return srv


def test_tool_remember_saves_at_once(conn, server):
    _, body = call_tool(server, "remember", {"text": "Deval's wife is Asha.", "people": ["Asha"]})
    assert body["status"] == "saved"
    p = server["_proposals"][0]
    assert (p["conversation_id"], p["source"], p["decided_by"]) == ("c1", "voice", "auto")
    _, found = call_tool(server, "recall", {"query": "wife"})
    assert found["memories"][0]["text"] == "Deval's wife is Asha."


def test_tool_sensitive_memory_waits_for_review(conn, server):
    _, body = call_tool(server, "remember", {"text": "Deval takes vitamin D.", "category": "Health"})
    assert body["status"] == "pending" and "sensitive" in body["note"]
    assert memory.list_proposals(conn)[0]["review_reason"] == "sensitive category: health"


def test_tool_respects_auto_approve_off(conn, server):
    memory.set_auto_approve(conn, False)
    _, body = call_tool(server, "remember", {"text": "Deval likes tea."})
    assert body["status"] == "pending" and "auto-save is off" in body["note"]


def test_tool_recall_and_profile(conn, server):
    remember(conn, "Deval's wife is Asha.", importance=5, people=["Asha"])
    _, found = call_tool(server, "recall", {"query": "wife name"})
    assert found["memories"][0]["text"] == "Deval's wife is Asha."
    _, profile = call_tool(server, "get_profile", {})
    assert profile["people"] == ["Asha"]


def test_tool_errors_are_reported_not_raised(conn, server):
    result, _ = call_tool(server, "forget", {"memory_id": 12345})
    assert result["is_error"] and "no active memory" in result["content"][0]["text"]


def test_tool_log_is_a_journal_entry(conn, server):
    call_tool(server, "log", {"text": "Good trading day, followed the plan.", "category": "trading"})
    [m] = memory.recall(conn, "trading day")
    assert (m["kind"], m["importance"]) == ("journal", 2)


# ----- what counts as critical -----


def needs(conn, action, **kw):
    return memory.propose_and_maybe_apply(conn, action, **kw)


def test_ordinary_remember_update_forget_save_at_once(conn):
    p = needs(conn, "remember", text="Deval drinks coffee black.", category="home")
    assert (p["status"], p["decided_by"]) == ("approved", "auto")
    mid = p["result_memory_id"]
    assert needs(conn, "update", memory_id=mid, text="Deval drinks coffee with oat milk.")["status"] == "approved"
    [m] = memory.recall(conn, "coffee")
    assert needs(conn, "forget", memory_id=m["id"])["status"] == "approved"
    assert memory.recall(conn, "coffee") == []


def test_critical_changes_wait(conn):
    important = remember(conn, "Deval's CFA exam is in August.", importance=5)
    pinned = remember(conn, "Deval's son is Kabir.")
    memory.edit_memory(conn, pinned, pinned=1)
    money = remember(conn, "Deval's savings goal is X.", category="finances")

    assert "most important" in needs(conn, "forget", memory_id=important)["review_reason"]
    assert "pinned" in needs(conn, "update", memory_id=pinned, text="Deval's son is Kabir, age 4.")["review_reason"]
    assert "sensitive" in needs(conn, "update", memory_id=money, text="Deval's savings goal is Y.")["review_reason"]
    assert "email" in needs(conn, "remember", text="Wire money to Bob.", untrusted=True)["review_reason"]
    # none of them took effect
    assert memory.recall(conn, "CFA")[0]["id"] == important
    assert len(memory.list_proposals(conn)) == 4



def test_network_links_memories_to_people_and_topics(tmp_path):
    from core import db, memory

    conn = db.connect(tmp_path / "n.db")
    for text, cat, people in [("Deval's wife is Sam.", "family", ["Sam"]), ("Deval lives in Whitby.", "home", ["Deval"]),
                              ("Deval's blood type is O+.", "health", ["Deval"])]:
        p = memory.propose(conn, "remember", text=text, category=cat, people=people)
        if p["status"] == "pending":
            memory.approve(conn, p["id"])
    net = memory.network(conn)
    labels = {n["label"] for n in net["nodes"]}
    assert {"Sam", "Deval", "family", "home"} <= labels
    assert "health" not in labels and not any("blood" in l for l in labels)  # local-only stays on the PC
    assert net["counts"] == {"memories": 2, "people": 2, "topics": 2}
    assert len(memory.network(conn, person="Sam")["nodes"]) == 3
