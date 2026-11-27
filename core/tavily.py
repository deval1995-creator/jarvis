"""Web search and page reading through Tavily (built for AI agents).

Search: 1.5-3 s with a short answer and dated sources. Read: a page as clean
text in well under a second. Much faster than Claude Code's built-in
WebSearch/WebFetch (5-12 s and ~3 s), which stay as the fallback.

The key lives in Jarvis's data folder (tavily_api_key), never in the repo.
Results are web content written by strangers: callers treat them as data.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

from core import db

API = "https://api.tavily.com/"
TOPICS = {"general", "news", "finance"}
TIME_RANGES = {"day", "week", "month", "year"}
SNIPPET_CHARS = 700
PAGE_CHARS = 12000


class TavilyError(RuntimeError):
    pass


def api_key() -> str | None:
    path = db.default_db_path().parent / "tavily_api_key"
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return key or None


def _post(path: str, body: dict, key: str, timeout: float = 30) -> dict:
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        if e.code in (401, 403):
            raise TavilyError("Tavily rejected the API key") from e
        if e.code == 429 or e.code == 432:
            raise TavilyError("Tavily's free monthly allowance is used up (or rate limited)") from e
        raise TavilyError(f"Tavily error {e.code}: {detail}") from e


def search(query: str, topic: str = "general", time_range: str | None = None, max_results: int = 5,
           post=_post) -> dict:
    key = api_key()
    if not key:
        raise TavilyError("Tavily isn't set up (no API key)")
    body = {"query": query[:400], "topic": topic if topic in TOPICS else "general",
            "max_results": max(1, min(10, int(max_results))), "include_answer": True, "search_depth": "basic"}
    if time_range in TIME_RANGES:
        body["time_range"] = time_range
    d = post("search", body, key)
    return {
        "answer": d.get("answer"),
        "results": [{"title": r.get("title"), "url": r.get("url"), "published": r.get("published_date"),
                     "content": (r.get("content") or "")[:SNIPPET_CHARS]} for r in d.get("results", [])],
        "source": "Tavily web search",
    }


def read(urls: list[str], post=_post) -> dict:
    key = api_key()
    if not key:
        raise TavilyError("Tavily isn't set up (no API key)")
    urls = [u for u in urls if isinstance(u, str) and u.startswith(("http://", "https://"))][:3]
    if not urls:
        raise TavilyError("give one to three http(s) links")
    d = post("extract", {"urls": urls}, key)
    pages = []
    for r in d.get("results", []):
        text = r.get("raw_content") or ""
        pages.append({"url": r.get("url"), "text": text[:PAGE_CHARS], "truncated": len(text) > PAGE_CHARS})
    return {"pages": pages, "failed": [f.get("url") if isinstance(f, dict) else f for f in d.get("failed_results", [])]}


async def search_async(*a, **k) -> dict:
    return await asyncio.to_thread(search, *a, **k)


async def read_async(*a, **k) -> dict:
    return await asyncio.to_thread(read, *a, **k)
