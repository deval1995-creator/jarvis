import asyncio

import pytest

from core import db, memory, memory_review, store


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "jarvis.db")
    yield c
    c.close()


class FakeRunner:
    def __init__(self, memories=(), summary="Talked about family."):
        self.memories = list(memories)
        self.summary = summary
        self.prompts: list[str] = []

    async def __call__(self, system_prompt, prompt):
        self.prompts.append(prompt)
        usage = {"input_tokens": 50, "output_tokens": 20, "session_id": "bg-1", "cost_usd": 0.0}
        return {"memories": self.memories, "summary": self.summary}, usage


def chat(conn, conversation_id, *pairs):
    for user, assistant in pairs:
        store.add_message(conn, conversation_id, "user", "typed", user)
        store.add_message(conn, conversation_id, "assistant", "typed", assistant)


def cand(text, **kw):
    return {"text": text, "kind": "fact", "importance": 3, "confidence": 1.0, "reason": "stated", **kw}


def review(conn, cid, runner):
    return asyncio.run(memory_review.review_conversation(conn, cid, runner))


def test_saves_missed_memories_and_summary(conn):
    cid = store.create_conversation(conn)
    chat(conn, cid, ("My sister Priya just moved to Pune for a new job.", "That's exciting!"))
    runner = FakeRunner([cand("Deval's sister Priya lives in Pune.", category="family", people=["Priya"])])
    r = review(conn, cid, runner)

    assert [p["text"] for p in r.saved] == ["Deval's sister Priya lives in Pune."]
    [m] = memory.recall(conn, "Priya")
    assert m["people"] == ["Priya"]
    assert store.get_conversation(conn, cid)["summary"] == "Talked about family."
    assert conn.execute("select kind from usage").fetchone()[0] == "background"
    assert "Priya just moved to Pune" in runner.prompts[0]


def test_only_reviews_new_messages(conn):
    cid = store.create_conversation(conn)
    chat(conn, cid, ("My sister Priya just moved to Pune for a new job.", "Nice."))
    runner = FakeRunner()
    review(conn, cid, runner)
    assert review(conn, cid, runner).skipped_reason == "nothing new"

    chat(conn, cid, ("Also I started learning the guitar this week, loving it.", "Great!"))
    review(conn, cid, runner)
    assert "guitar" in runner.prompts[-1] and "Priya" not in runner.prompts[-1]


def test_short_chat_skipped_without_calling_claude(conn):
    cid = store.create_conversation(conn)
    chat(conn, cid, ("hi", "Hello!"))
    runner = FakeRunner()
    r = review(conn, cid, runner)
    assert r.skipped_reason == "too short" and runner.prompts == []
    assert memory_review.pending_review(conn, cid) == []


def test_skips_duplicates_of_saved_and_pending(conn):
    memory.approve(conn, memory.propose(conn, "remember", text="Deval's wife is Asha.")["id"])
    memory.propose(conn, "remember", text="Deval takes vitamin D.", category="health")  # pending
    cid = store.create_conversation(conn)
    chat(conn, cid, ("Asha and I are planning a trip to Goa in December.", "Sounds fun."))
    runner = FakeRunner([
        cand("Deval's wife is Asha"),
        cand("Deval takes Vitamin D"),
        cand("Deval and Asha plan a trip to Goa in December.", kind="decision"),
    ])
    r = review(conn, cid, runner)
    assert len(r.skipped_duplicates) == 2
    assert [p["text"] for p in r.saved] == ["Deval and Asha plan a trip to Goa in December."]


def test_sensitive_candidates_wait_for_approval(conn):
    cid = store.create_conversation(conn)
    chat(conn, cid, ("My doctor says my cholesterol is a bit high, cutting back on butter.", "Good plan."))
    r = review(conn, cid, FakeRunner([cand("Deval has high cholesterol.", category="health")]))
    assert r.saved == [] and len(r.pending) == 1
    assert memory.list_proposals(conn)[0]["origin"] == "review"


def test_local_only_memories_never_sent_to_claude(conn):
    memory.approve(conn, memory.propose(conn, "remember", text="Deval's salary is X.", category="finances")["id"])
    memory.approve(conn, memory.propose(conn, "remember", text="Deval likes chess.", category="hobbies")["id"])
    cid = store.create_conversation(conn)
    chat(conn, cid, ("Played a great game of chess with my dad last night.", "Nice!"))
    runner = FakeRunner()
    review(conn, cid, runner)
    assert "Deval likes chess." in runner.prompts[0]
    assert "salary" not in runner.prompts[0]


def test_reviewer_survives_runner_failure(conn):
    async def broken(system_prompt, prompt):
        raise RuntimeError("CLI crashed")

    cid = store.create_conversation(conn)
    chat(conn, cid, ("My sister Priya just moved to Pune for a new job.", "Nice."))
    reviewer = memory_review.Reviewer(conn, broken)
    assert asyncio.run(reviewer.review(cid)) is None
    assert memory_review.pending_review(conn, cid) != []  # will be retried later


def test_low_confidence_guesses_dropped(conn):
    cid = store.create_conversation(conn)
    chat(conn, cid, ("It's freezing out here today, minus twenty at the bus stop.", "Stay warm!"))
    r = review(conn, cid, FakeRunner([cand("Deval probably lives in Canada.", confidence=0.5)]))
    assert r.saved == [] and r.skipped_low_confidence == ["Deval probably lives in Canada."]


def test_prompt_carries_today(conn):
    from datetime import date

    cid = store.create_conversation(conn)
    chat(conn, cid, ("My sister Priya just moved to Pune for a new job.", "Nice."))
    runner = FakeRunner()
    review(conn, cid, runner)
    assert runner.prompts[0].startswith(f"TODAY: {date.today().isoformat()}")
