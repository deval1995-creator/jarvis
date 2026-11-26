import asyncio

from fastapi.testclient import TestClient

from core import connectors, db
from core.api import create_app
from core.display_mcp import MAX_WINDOWS, WindowBoard, clean_window
from tests.fakes import FakeBackend

PORTFOLIO = {
    "title": "IBKR Portfolio", "subtitle": "as of 14:32 ET",
    "stats": [{"label": "Net liq", "value": "$30,000"}, {"label": "Day P&L", "value": "+$412", "tone": "up"}],
    "table": {"columns": ["Symbol", "Qty", "P&L"], "rows": [["AAPL", "10", "+$120"], ["TSLA", "5", "-$40"]]},
    "note": "Account U123",
}


def test_window_is_cleaned_to_known_plain_fields():
    w = clean_window({**PORTFOLIO, "html": "<script>x</script>", "stats": [{"label": "a", "value": 5, "tone": "evil"}]})
    assert "html" not in w
    kinds = [p["type"] for p in w["panels"]]
    assert kinds == ["stats", "table"]  # old single-section fields become panels
    assert w["panels"][0]["stats"] == [{"label": "a", "value": "5", "tone": "neutral"}]
    assert w["panels"][1]["table"]["rows"][1] == ["TSLA", "5", "-$40"]
    assert w["size"] == "medium" and w["columns"] == 1


def test_panels_charts_and_sizes():
    w = clean_window({"title": "P&L", "size": "full", "columns": 9, "panels": [
        {"type": "chart", "title": "Daily P&L", "span": 2,
         "chart": {"kind": "bar", "x": ["Mon", "Tue"], "series": [{"name": "P&L", "values": [120, "-$40", "oops"]}], "unit": "$"}},
        {"type": "chart", "chart": {"kind": "pie", "series": [{"values": [60, 40]}]}},
        {"type": "chart", "chart": {"kind": "radar", "series": [{"values": [1]}]}},   # unknown kind: dropped
        {"type": "chart", "chart": {"kind": "line", "series": [{"values": ["x", None]}]}},  # no numbers: dropped
        {"type": "text", "text": "Mostly flat."},
        {"type": "nope"},
    ]})
    assert w["size"] == "full" and w["columns"] == 3
    assert [p["type"] for p in w["panels"]] == ["chart", "chart", "text"]
    bar = w["panels"][0]
    assert bar["span"] == 2 and bar["title"] == "Daily P&L"
    assert bar["chart"]["series"][0]["values"] == [120.0, -40.0, None]
    assert bar["chart"]["x"] == ["Mon", "Tue", "3"]  # labels padded to the values
    assert w["panels"][1]["chart"]["x"] == ["1", "2"]


def test_graph_or_chart_defaults_to_large():
    assert clean_window({"title": "g", "graph": {"nodes": [{"id": "a", "label": "A"}]}})["size"] == "large"


def test_board_opens_replaces_closes_and_caps():
    events = []

    async def on_change(e):
        events.append(e["type"])

    async def run():
        b = WindowBoard(on_change)
        w = await b.show(PORTFOLIO)
        again = await b.show({"title": "IBKR Portfolio", "replace": w["id"]})
        assert again["id"] == w["id"] and len(b.list()) == 1
        for i in range(MAX_WINDOWS + 2):
            await b.show({"title": f"w{i}"})
        assert len(b.list()) == MAX_WINDOWS
        assert await b.close("all") and b.list() == []

    asyncio.run(run())
    assert events[0] == "window" and "window_closed" in events


def test_windows_api(tmp_path):
    async def no_review(*a):
        return {"memories": [], "summary": ""}, None

    app = create_app(conn=db.connect(tmp_path / "j.db"), backend_factory=FakeBackend, token="t", review_runner=no_review)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        w = asyncio.run(app.state.board.show(PORTFOLIO))
        h = {"X-Jarvis-Token": "t"}
        assert [x["id"] for x in c.get("/api/windows", headers=h).json()] == [w["id"]]
        assert c.delete(f"/api/windows/{w['id']}", headers=h).json() == {"closed": [w["id"]]}
        assert c.get("/api/windows", headers=h).json() == []
        assert c.get("/api/windows").status_code == 401


def test_ibkr_is_read_only():
    p = "mcp__claude_ai_Interactive_Brokers_IBKR__"
    assert p + "get_account_positions" in connectors.ALLOWED_TOOLS
    assert p + "create_order_instruction" in connectors.DISALLOWED_TOOLS
    assert p + "create_order_instruction" not in connectors.NEEDS_APPROVAL
    assert not connectors.reads_outside_content(p + "get_account_positions")
    assert connectors.reads_outside_content("mcp__claude_ai_Gmail__get_thread")
