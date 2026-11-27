"""Jarvis's own web browser (Playwright, headless Chromium).

For what search and page extraction can't do: pages that need JavaScript,
following links, looking at a page, and (with Deval's approval) clicking
buttons and filling in forms.

Safety:
- A fresh, separate browser with no saved logins: never Deval's Chrome, so no
  email, bank or broker session is ever exposed.
- Reading, following links, going back and screenshots are free. Clicking
  buttons and typing go through an approval card (see connectors.NEEDS_APPROVAL),
  and the element must still match the text the card showed.
- No local files, no localhost (Jarvis's own Core lives there), no private
  network addresses.
- Page text is written by strangers: callers treat it as data.
- Starts on first use; closes after IDLE_SECS without use.
"""

from __future__ import annotations

import asyncio
import functools
import ipaddress
import logging
import re
import socket
import time
from urllib.parse import urlparse

log = logging.getLogger(__name__)

IDLE_SECS = 600
TEXT_CHARS = 6000
MAX_ELEMENTS = 80
NAV_TIMEOUT_MS = 20000

# Numbers every link, button and field on the page (data-jarvis-ref) and lists them.
_MARK_JS = r"""
(max) => {
  const out = [];
  let n = 0;
  const seen = new Set();
  const visible = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  for (const el of document.querySelectorAll('a[href], button, input, select, textarea, [role=button], [role=link]')) {
    if (seen.has(el) || !visible(el)) continue;
    seen.add(el);
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && ['hidden'].includes(type)) continue;
    n += 1;
    el.setAttribute('data-jarvis-ref', String(n));
    let kind = tag === 'a' || el.getAttribute('role') === 'link' ? 'link'
      : tag === 'select' ? 'select' : tag === 'textarea' || (tag === 'input' && !['submit','button','checkbox','radio','reset','image'].includes(type)) ? 'field'
      : 'button';
    const label = (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder')
                   || el.getAttribute('title') || el.getAttribute('name') || '').trim().replace(/\s+/g, ' ').slice(0, 80);
    const item = { ref: n, kind, text: label };
    if (kind === 'link') item.href = el.href;
    if (out.length < max) out.push(item);
  }
  return out;
}
"""


class BrowserError(RuntimeError):
    pass


def check_url(url: str, allow_local: bool = False) -> str:
    """Only public http(s) pages."""
    url = (url or "").strip()
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:(?!//)", url) and not re.match(r"^[^:/]+:\d+(/|$)", url):
        raise BrowserError("only http(s) web addresses can be opened")  # javascript:, data:, mailto: ...
    if url and "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise BrowserError("only http(s) web addresses can be opened")
    if allow_local:
        return url
    problem = _host_problem(p.hostname.lower())
    if problem:
        raise BrowserError(problem)
    return url


@functools.lru_cache(maxsize=2048)
def _host_problem(host: str) -> str | None:
    """Why this host is off limits, or None. Cached: pages make many requests per site."""
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return "local addresses are blocked"
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        return f"can't find {host}"
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a.split("%")[0])
        except ValueError:
            return f"can't check the address of {host}"
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return "local and private network addresses are blocked"
    return None


class Browser:
    def __init__(self, allow_local: bool = False, headless: bool = True):
        self.allow_local = allow_local
        self.headless = headless
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None
        self._lock = asyncio.Lock()
        self._last_used = 0.0
        self._idle_task: asyncio.Task | None = None

    async def _ensure(self):
        if self.page is not None and not self.page.is_closed():
            return self.page
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.headless)
        # A fresh context: no cookies, logins or history from anywhere else.
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 860}, locale="en-CA", timezone_id="America/Toronto",
            accept_downloads=False,
        )
        self._context.set_default_timeout(NAV_TIMEOUT_MS)

        async def guard(route):  # every request, including redirects and page scripts
            try:
                check_url(route.request.url, self.allow_local)
            except BrowserError:
                return await route.abort()
            await route.continue_()

        await self._context.route("**/*", guard)
        self.page = await self._context.new_page()
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._close_when_idle())
        return self.page

    async def _close_when_idle(self):
        while True:
            await asyncio.sleep(30)
            if self._browser is not None and time.monotonic() - self._last_used > IDLE_SECS:
                await self.close()
                return

    async def close(self):
        for thing in (self._context, self._browser):
            try:
                if thing is not None:
                    await thing.close()
            except Exception:
                log.debug("browser close", exc_info=True)
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                log.debug("playwright stop", exc_info=True)
        self._pw = self._browser = self._context = self.page = None

    async def _snapshot(self, offset: int = 0) -> dict:
        page = self.page
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception:
            pass
        elements = await page.evaluate(_MARK_JS, MAX_ELEMENTS)
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        text = " ".join(text.split()) if len(text) > 200000 else text
        chunk = text[offset:offset + TEXT_CHARS]
        return {"url": page.url, "title": await page.title(), "text": chunk,
                "more_text": len(text) > offset + TEXT_CHARS, "text_offset": offset,
                "elements": elements}

    async def _run(self, fn):
        async with self._lock:
            self._last_used = time.monotonic()
            await self._ensure()
            try:
                return await fn(self.page)
            finally:
                self._last_used = time.monotonic()

    async def open(self, url: str) -> dict:
        url = check_url(url, self.allow_local)

        async def go(page):
            try:
                await page.goto(url, wait_until="domcontentloaded")
            except Exception as e:
                raise BrowserError(f"couldn't open {url}: {str(e).splitlines()[0][:150]}") from e
            await page.wait_for_timeout(800)  # let scripts draw the page
            return await self._snapshot()

        return await self._run(go)

    async def read(self, offset: int = 0) -> dict:
        async def rd(page):
            if page.url in ("about:blank", ""):
                raise BrowserError("no page is open")
            return await self._snapshot(max(0, int(offset)))

        return await self._run(rd)

    async def _element(self, page, ref: int):
        el = page.locator(f'[data-jarvis-ref="{int(ref)}"]').first
        if await el.count() == 0:
            raise BrowserError(f"no element {ref} on this page (read the page again for fresh numbers)")
        return el

    async def _label(self, el) -> str:
        return " ".join((await el.evaluate(
            "e => (e.innerText || e.value || e.getAttribute('aria-label') || e.getAttribute('placeholder') "
            "|| e.getAttribute('title') || e.getAttribute('name') || '')")).split())[:80]

    async def follow(self, ref: int) -> dict:
        """Open a link (free: it only navigates)."""
        async def fl(page):
            el = await self._element(page, ref)
            href = await el.evaluate("e => e.href || ''")
            if not href:
                raise BrowserError(f"element {ref} isn't a link; clicking buttons needs Deval's approval")
            target = check_url(href, self.allow_local)
            await page.goto(target, wait_until="domcontentloaded")
            await page.wait_for_timeout(800)
            return await self._snapshot()

        return await self._run(fl)

    async def back(self) -> dict:
        async def bk(page):
            await page.go_back(wait_until="domcontentloaded")
            await page.wait_for_timeout(500)
            return await self._snapshot()

        return await self._run(bk)

    async def click(self, ref: int, expect_text: str) -> dict:
        """Click a button or control (approved by Deval). The element must still
        carry the text the approval card showed."""
        async def ck(page):
            el = await self._element(page, ref)
            label = await self._label(el)
            if expect_text and expect_text.strip().lower() not in label.lower():
                raise BrowserError(f"element {ref} now says {label!r}, not {expect_text!r}; nothing clicked")
            await el.click()
            await page.wait_for_timeout(1200)
            return await self._snapshot()

        return await self._run(ck)

    async def type(self, ref: int, text: str, submit: bool = False) -> dict:
        """Type into a field (approved by Deval), optionally pressing Enter."""
        async def tp(page):
            el = await self._element(page, ref)
            await el.fill(text)
            if submit:
                await el.press("Enter")
                await page.wait_for_timeout(1500)
            return await self._snapshot()

        return await self._run(tp)

    async def screenshot(self) -> bytes:
        async def sh(page):
            if page.url in ("about:blank", ""):
                raise BrowserError("no page is open")
            return await page.screenshot(type="jpeg", quality=70)

        return await self._run(sh)
