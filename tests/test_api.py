import pytest
from fastapi.testclient import TestClient

from core import db
from core.api import create_app
from tests.fakes import FakeBackend

TOKEN = "test-token"


async def no_claude_review(system_prompt, prompt):
    return {"memories": [], "summary": "test"}, None
AUTH = {"X-Jarvis-Token": TOKEN}
ORIGIN = {"origin": "http://127.0.0.1:8765"}
WS = "ws://127.0.0.1:8765/ws"  # absolute: a relative URL sends Host: testserver


@pytest.fixture
def client(tmp_path):
    conn = db.connect(tmp_path / "jarvis.db")
    app = create_app(conn=conn, backend_factory=FakeBackend, token=TOKEN, review_runner=no_claude_review)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        yield c
    conn.close()


def test_api_requires_token(client):
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"X-Jarvis-Token": "wrong"}).status_code == 401
    assert client.get("/api/status", headers=AUTH).status_code == 200


def test_rejects_foreign_host_header(client):
    # DNS rebinding: evil.example resolving to 127.0.0.1
    r = client.get("/api/status", headers={**AUTH, "host": "evil.example"})
    assert r.status_code == 400


def test_page_carries_token(client):
    r = client.get("/")
    assert r.status_code == 200 and TOKEN in r.text


def test_post_message_returns_reply_and_saves(client):
    r = client.post("/api/message", json={"text": "hello", "source": "routine"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["type"] == "done" and r.json()["text"] == "Hello, Deval."
    conv = client.get("/api/conversations/current", headers=AUTH).json()
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant"]
    assert client.get(f"/api/conversations/{conv['id']}", headers=AUTH).status_code == 200
    assert client.get("/api/conversations/nope", headers=AUTH).status_code == 404


def test_post_message_rejects_bad_source(client):
    r = client.post("/api/message", json={"text": "hi", "source": "telepathy"}, headers=AUTH)
    assert r.status_code == 422


def test_websocket_streams_a_turn(client):
    with client.websocket_connect(WS, headers=ORIGIN) as ws:
        ws.send_json({"type": "auth", "token": TOKEN})
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "message", "text": "hi"})
        types = []
        while True:
            ev = ws.receive_json()
            types.append(ev["type"])
            if ev["type"] in ("done", "interrupted", "error"):
                break
    assert types == ["start", "delta", "delta", "done"]


def test_websocket_rejects_bad_token(client):
    from starlette.websockets import WebSocketDisconnect

    for hello in [{"type": "auth", "token": "wrong"}, {"type": "message", "text": "hi"}]:
        with client.websocket_connect(WS, headers=ORIGIN) as ws:
            ws.send_json(hello)
            with pytest.raises(WebSocketDisconnect) as e:
                ws.receive_json()
            assert e.value.code == 4401


def test_websocket_rejects_foreign_origin(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(WS, headers={"origin": "https://evil.example"}):
            pass
    assert e.value.code == 4403


def test_new_conversation(client):
    client.post("/api/message", json={"text": "hello"}, headers=AUTH)
    old = client.get("/api/status", headers=AUTH).json()["conversation_id"]
    new = client.post("/api/conversations/new", headers=AUTH).json()["conversation_id"]
    assert new != old
    assert len(client.get("/api/conversations", headers=AUTH).json()) == 2


def test_idle_conversation_gets_reviewed(tmp_path):
    import time

    calls = []

    async def runner(system_prompt, prompt):
        calls.append(prompt)
        return {"memories": [{"text": "Deval's sister Priya lives in Pune.", "kind": "fact", "importance": 3,
                              "confidence": 1.0, "reason": "stated"}], "summary": "Family news."}, None

    conn = db.connect(tmp_path / "jarvis.db")
    app = create_app(conn=conn, backend_factory=FakeBackend, token=TOKEN, review_runner=runner,
                     idle_review_seconds=0, idle_check_seconds=0.05)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        c.post("/api/message", json={"text": "My sister Priya just moved to Pune for a new job."}, headers=AUTH)
        deadline = time.time() + 5
        while not calls and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.2)
        assert len(calls) == 1  # reviewed once, not again on later checks
        saved = c.get("/api/memory", headers=AUTH).json()
        assert [m["text"] for m in saved] == ["Deval's sister Priya lives in Pune."]
    conn.close()
