"""Jarvis's browser, driven for real (headless Chromium) against a tiny local site."""

import asyncio
import http.server
import socketserver
import threading
from types import SimpleNamespace

import pytest

from core import browser as br

PAGES = {
    "/": b"""<html><head><title>Home</title></head><body><h1>Cafe Test</h1><p>Open daily 8-6.</p>
             <a href="/menu">Menu</a> <form action="/search"><input name="q" placeholder="Search menu">
             <button type="submit">Search</button></form><button id="book">Book a table</button></body></html>""",
    "/menu": b"<html><head><title>Menu</title></head><body><p>Latte 4.50</p></body></html>",
}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        body = PAGES.get(path, b"<html><head><title>Results</title></head><body>Results for: "
                         + self.path.encode() + b"</body></html>")
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def site():
    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def run(coro):
    return asyncio.run(coro)


def test_local_and_odd_addresses_are_blocked(monkeypatch):
    fake_dns = {"example.com": "93.184.215.14", "sneaky.example": "10.0.0.5"}
    monkeypatch.setattr(br.socket, "getaddrinfo", lambda host, port: [(0, 0, 0, "", (fake_dns.get(host, host), 0))])
    br._host_problem.cache_clear()
    with pytest.raises(br.BrowserError, match="private"):
        br.check_url("http://sneaky.example/")  # a public name pointing at a private address
    for bad in ["file:///C:/Windows/win.ini", "http://localhost:8765/", "http://127.0.0.1:8765/api/status",
                "http://192.168.1.1/", "javascript:alert(1)", "ftp://example.com"]:
        with pytest.raises(br.BrowserError):
            br.check_url(bad)
    assert br.check_url("example.com") == "https://example.com"
    br._host_problem.cache_clear()


def test_open_read_follow_and_back(site):
    async def go():
        b = br.Browser(allow_local=True)
        try:
            page = await b.open(site + "/")
            assert page["title"] == "Home" and "Open daily 8-6" in page["text"]
            kinds = {e["text"]: e["kind"] for e in page["elements"]}
            assert kinds["Menu"] == "link" and kinds["Book a table"] == "button" and kinds["Search menu"] == "field"
            menu_ref = next(e["ref"] for e in page["elements"] if e["text"] == "Menu")
            menu = await b.follow(menu_ref)
            assert menu["title"] == "Menu" and "Latte" in menu["text"]
            assert (await b.back())["title"] == "Home"
            book_ref = next(e["ref"] for e in (await b.read())["elements"] if e["text"] == "Book a table")
            with pytest.raises(br.BrowserError, match="isn't a link"):
                await b.follow(book_ref)  # buttons need the approved click
            assert (await b.screenshot())[:2] == b"\xff\xd8"  # a JPEG
        finally:
            await b.close()
    run(go())


def test_click_must_match_the_approved_label_and_type_submits(site):
    async def go():
        b = br.Browser(allow_local=True)
        try:
            page = await b.open(site + "/")
            book = next(e["ref"] for e in page["elements"] if e["text"] == "Book a table")
            with pytest.raises(br.BrowserError, match="nothing clicked"):
                await b.click(book, "Delete account")
            await b.click(book, "Book a table")
            field = next(e["ref"] for e in (await b.read())["elements"] if e["kind"] == "field")
            result = await b.type(field, "oat latte", submit=True)
            assert "Results for: /search?q=oat+latte" in result["text"]
        finally:
            await b.close()
    run(go())


def test_browser_actions_go_through_an_approval_card(tmp_path):
    from core import db
    from core.api import create_app
    from tests.fakes import FakeBackend

    async def no_review(*a):
        return {"memories": [], "summary": ""}, None

    app = create_app(conn=db.connect(tmp_path / "j.db"), backend_factory=FakeBackend, token="t",
                     review_runner=no_review, connectors=True)
    broker = app.state.broker

    async def scenario():
        gate = asyncio.create_task(app.state.tool_hooks["gate"](
            "mcp__tools__browser_click", {"ref": 3, "label": "Book", "purpose": "book a table"},
            SimpleNamespace(tool_use_id="u1")))
        while not broker.list():
            await asyncio.sleep(0.01)
        await broker.decide(broker.list()[0]["id"], False)
        return await gate

    assert run(scenario()).behavior == "deny"
