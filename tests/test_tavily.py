import pytest

from core import connectors, tavily


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(tavily, "api_key", lambda: "tvly-test")


def test_search_shapes_and_bounds(key):
    seen = {}

    def post(path, body, k):
        seen.update(path=path, body=body, key=k)
        return {"answer": "Up 3%.", "results": [{"title": "T", "url": "https://x", "published_date": "today",
                                                  "content": "y" * 5000}]}

    out = tavily.search("nvda today", topic="news", time_range="day", max_results=50, post=post)
    assert seen["path"] == "search" and seen["key"] == "tvly-test"
    assert seen["body"]["max_results"] == 10 and seen["body"]["topic"] == "news" and seen["body"]["time_range"] == "day"
    assert out["answer"] == "Up 3%." and len(out["results"][0]["content"]) == tavily.SNIPPET_CHARS


def test_bad_topic_and_range_fall_back(key):
    out = {}
    tavily.search("x", topic="gossip", time_range="decade", post=lambda p, b, k: out.update(b) or {"results": []})
    assert out["topic"] == "general" and "time_range" not in out


def test_read_only_web_links(key):
    sent = {}

    def post(path, body, k):
        sent.update(body)
        return {"results": [{"url": "https://a", "raw_content": "z" * 20000}], "failed_results": []}

    out = tavily.read(["file:///C:/secret.txt", "https://a", "https://b", "https://c", "https://d"], post=post)
    assert sent["urls"] == ["https://a", "https://b", "https://c"]
    assert out["pages"][0]["truncated"] and len(out["pages"][0]["text"]) == tavily.PAGE_CHARS


def test_no_key_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(tavily, "api_key", lambda: None)
    with pytest.raises(tavily.TavilyError, match="isn't set up"):
        tavily.search("x")


def test_web_results_mark_the_conversation():
    assert connectors.reads_outside_content("mcp__tools__search")
    assert connectors.reads_outside_content("mcp__tools__read_page")
    assert not connectors.reads_outside_content("mcp__tools__weather")
