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
TEXT_CHARS = 12000
MAX_ELEMENTS = 120
NAV_TIMEOUT_MS = 45000  # slow sites (restaurants with 60+ files) can take 10-25 s
READY_TIMEOUT_MS = 15000

# Numbers every link, button and field on the page (data-jarvis-ref) and lists them.
_MARK_JS = r"""
(max) => {
  const out = [];
  let n = 0;
  const seen = new Set();
  const visible = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  for (const el of document.querySelectorAll('a[href], button, input, select, textarea, [role=button], [role=link], [role=tab], [aria-expanded], summary')) {
    if (seen.has(el) || !visible(el)) continue;
    seen.add(el);
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && ['hidden'].includes(type)) continue;
    n += 1;
    el.setAttribute('data-jarvis-ref', String(n));
    const role = (el.getAttribute('role') || '').toLowerCase();
    let kind = role === 'tab' || tag === 'summary' || (el.hasAttribute('aria-expanded') && tag !== 'a') ? 'tab'
      : tag === 'a' || role === 'link' ? 'link'
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


MAX_SHOT_HEIGHT = 12000

# Scroll down the page in steps (so lazy images load), then back to the top.
_SCROLL_THROUGH_JS = r"""
async () => {
  const step = innerHeight * 0.9, max = Math.min(document.documentElement.scrollHeight, 12000);
  for (let y = 0; y < max; y += step) { scrollTo(0, y); await new Promise(r => setTimeout(r, 120)); }
  scrollTo(0, 0);
}
"""
_SCROLL_TO_TEXT_JS = r"""
(text) => {
  const t = text.toLowerCase();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const el = walker.currentNode.parentElement;
    // only text that's actually shown (sites often keep hidden mobile copies)
    if (walker.currentNode.textContent.toLowerCase().includes(t) && el.getClientRects().length
        && getComputedStyle(el).visibility !== 'hidden') {
      el.scrollIntoView({block: 'start'});
      scrollBy(0, -80);
      return true;
    }
  }
  return false;
}
"""


_HIDDEN_TEXT_JS = r"""
() => {
  if (!document.body) return '';
  const copy = document.body.cloneNode(true);
  copy.querySelectorAll('script, style, noscript, svg').forEach(n => n.remove());
  copy.querySelectorAll('li, p, div, h1, h2, h3, h4, h5, h6, tr, br, section, article').forEach(n => n.append('\n'));
  return copy.textContent.replace(/[ \t]+/g, ' ').replace(/\n\s*\n+/g, '\n').trim();
}
"""

# Is this element a content-revealing control (tab, accordion, show-more), and
# nothing that could submit or send? Returns [ok, reason].
_REVEALABLE_JS = r"""
(el) => {
  const isSubmit = el.type === 'submit' || (el.tagName === 'BUTTON' && (el.getAttribute('type') || 'submit') === 'submit');
  if (el.closest('form') && isSubmit) return [false, 'would submit a form'];
  if (['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName)) return [false, 'is a form field'];
  const role = (el.getAttribute('role') || '').toLowerCase();
  const href = el.tagName === 'A' ? (el.getAttribute('href') || '') : null;
  const label = (el.innerText || el.getAttribute('aria-label') || '').trim().toLowerCase();
  if (['tab', 'treeitem'].includes(role)) return [true, ''];
  if (el.hasAttribute('aria-expanded') || el.hasAttribute('aria-controls') || el.hasAttribute('data-toggle')
      || el.hasAttribute('data-bs-toggle') || el.tagName === 'SUMMARY' || el.closest('summary')) return [true, ''];
  if (href !== null && (href.startsWith('#') || href.startsWith('javascript:void'))) return [true, ''];
  if (el.closest('[role=tablist], .tabs, .nav-tabs, .accordion, .vc_tta-tabs-list, .elementor-tabs')) return [true, ''];
  if (/^(show|see|view|load|read) (more|all)\b|^more$|^expand/.test(label)) return [true, ''];
  return [false, "isn't a tab or 'show more' control"];
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

    async def _snapshot(self, offset: int = 0, hidden: bool = False) -> dict:
        page = self.page
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=READY_TIMEOUT_MS)
        except Exception:
            pass
        elements = await page.evaluate(_MARK_JS, MAX_ELEMENTS)
        # innerText: what's on screen. With hidden: also text in closed tabs and
        # accordions (scripts and styles left out).
        text = await page.evaluate(_HIDDEN_TEXT_JS if hidden else "() => document.body ? document.body.innerText : ''")
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

    async def _goto(self, page, url: str) -> None:
        """Navigate, then wait for the page to be usable; a slow site that's still
        loading is read as far as it got rather than failing."""
        try:
            await page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT_MS)
        except Exception as e:
            raise BrowserError(f"couldn't open {url}: {str(e).splitlines()[0][:150]}") from e
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=READY_TIMEOUT_MS)
        except Exception:
            log.info("%s still loading; reading what has arrived", url)
        try:  # busy pages keep drawing (tabs, menus) after the document is ready
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        await page.wait_for_timeout(500)

    async def open(self, url: str) -> dict:
        url = check_url(url, self.allow_local)

        async def go(page):
            await self._goto(page, url)
            return await self._snapshot()

        return await self._run(go)

    async def read(self, offset: int = 0, hidden: bool = False) -> dict:
        async def rd(page):
            if page.url in ("about:blank", ""):
                raise BrowserError("no page is open")
            return await self._snapshot(max(0, int(offset)), hidden)

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
            await self._goto(page, target)
            return await self._snapshot()

        return await self._run(fl)

    async def back(self) -> dict:
        async def bk(page):
            await page.go_back(wait_until="domcontentloaded")
            await page.wait_for_timeout(500)
            return await self._snapshot()

        return await self._run(bk)

    async def reveal(self, ref: int) -> dict:
        """Click an in-page control that only shows content (a tab, an accordion,
        'show more', a same-page anchor). Free, because it can't send or submit
        anything; anything else needs the approved click."""
        async def rv(page):
            el = await self._element(page, ref)
            ok, why = await el.evaluate(_REVEALABLE_JS)
            if not ok:
                raise BrowserError(f"element {ref} {why}; clicking it needs Deval's approval (browser_click)")
            await el.click()
            await page.wait_for_timeout(700)
            # Bring what it opened into view: the tab's own panel when it names one.
            await el.evaluate("""e => {
              const id = e.getAttribute('aria-controls');
              const panel = id && document.getElementById(id);
              const target = panel && panel.getClientRects().length ? panel : e;
              target.scrollIntoView({block: 'start'}); scrollBy(0, -60);
            }""")
            await page.wait_for_timeout(300)
            return await self._snapshot()

        return await self._run(rv)

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

    async def screenshot(self, full_page: bool = True) -> bytes:
        """The visible part, or the whole page top to bottom (scrolled through
        first so lazy-loaded images appear; capped at MAX_SHOT_HEIGHT pixels)."""
        async def sh(page):
            if page.url in ("about:blank", ""):
                raise BrowserError("no page is open")
            if not full_page:
                return await page.screenshot(type="jpeg", quality=70)
            await page.evaluate(_SCROLL_THROUGH_JS)
            await page.wait_for_timeout(600)
            height = await page.evaluate("() => document.documentElement.scrollHeight")
            width = (page.viewport_size or {}).get("width", 1280)
            clip = {"x": 0, "y": 0, "width": width, "height": min(height, MAX_SHOT_HEIGHT)}
            for quality in (60, 45, 30):  # keep it small enough for a window
                shot = await page.screenshot(type="jpeg", quality=quality, full_page=True, clip=clip)
                if len(shot) < 2_000_000:
                    return shot
            return shot

        return await self._run(sh)

    async def scroll(self, to: str = "down") -> dict:
        """Move the view: down/up (one screen), top, bottom, or to the first text match."""
        async def sc(page):
            if to in ("down", "up"):
                await page.mouse.wheel(0, 760 if to == "down" else -760)
            elif to == "top":
                await page.evaluate("() => window.scrollTo(0, 0)")
            elif to == "bottom":
                await page.evaluate("() => window.scrollTo(0, document.documentElement.scrollHeight)")
            else:
                found = await page.evaluate(_SCROLL_TO_TEXT_JS, to)
                if not found:
                    raise BrowserError(f"couldn't find {to!r} on the page")
            await page.wait_for_timeout(700)
            snap = await self._snapshot()
            snap["scroll"] = await page.evaluate(
                "() => ({y: Math.round(scrollY), height: document.documentElement.scrollHeight, view: innerHeight})")
            return snap

        return await self._run(sc)
