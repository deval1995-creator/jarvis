import pytest
from fastapi.testclient import TestClient

from core import db
from core.api import create_app
from tests.fakes import FakeBackend

TOKEN = "test-token"
AUTH = {"X-Jarvis-Token": TOKEN}
ORIGIN = {"origin": "http://127.0.0.1:8765"}
WS = "ws://127.0.0.1:8765/ws"  # absolute: a relative URL sends Host: testserver


@pytest.fixture
def client(tmp_path):
    conn = db.connect(tmp_path / "jarvis.db")
    app = create_app(conn=conn, backend_factory=FakeBackend, token=TOKEN)
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
    with client.websocket_connect(f"{WS}?token={TOKEN}", headers=ORIGIN) as ws:
        ws.send_json({"type": "message", "text": "hi"})
        types = []
        while True:
            ev = ws.receive_json()
            types.append(ev["type"])
            if ev["type"] in ("done", "interrupted", "error"):
                break
    assert types == ["start", "delta", "delta", "done"]


def test_websocket_rejects_bad_token_and_foreign_origin(client):
    from starlette.websockets import WebSocketDisconnect

    for url, headers, code in [
        (f"{WS}?token=wrong", ORIGIN, 4401),
        (f"{WS}?token={TOKEN}", {"origin": "https://evil.example"}, 4403),
    ]:
        with pytest.raises(WebSocketDisconnect) as e:
            with client.websocket_connect(url, headers=headers):
                pass
        assert e.value.code == code


def test_new_conversation(client):
    client.post("/api/message", json={"text": "hello"}, headers=AUTH)
    old = client.get("/api/status", headers=AUTH).json()["conversation_id"]
    new = client.post("/api/conversations/new", headers=AUTH).json()["conversation_id"]
    assert new != old
    assert len(client.get("/api/conversations", headers=AUTH).json()) == 2
