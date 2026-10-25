"""Downloads Google Flights result pages and recognises when Google sent something else.

From the UK/EU, Google answers a cookie-less request with a "Before you continue" consent page
instead of results, so requests carry consent cookies. If that still doesn't work, a headless
Chromium (Playwright) can load the page like a normal browser and click through the consent page.
"""

import atexit
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlencode

from selectolax.lexbor import LexborHTMLParser

URL = "https://www.google.com/travel/flights"
DEBUG_DIR = Path(__file__).resolve().parent.parent / "results" / "debug"

# Stock "consent given" cookies for google.com (the same trick yt-dlp and others use).
CONSENT_COOKIES = {
    "SOCS": "CAESHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmVuIAEaBgiAo_CmBg",
    "CONSENT": "YES+cb.20230810-00-p0.en+FX+410",
}


class PageError(Exception):
    """Google returned a page without results. `kind` says what it was."""

    def __init__(self, kind: str, detail: str = ""):
        self.kind = kind  # consent | captcha | rate_limited | http_error | no_payload
        super().__init__(f"{kind}: {detail}" if detail else kind)

    @property
    def retryable(self) -> bool:
        return self.kind in ("rate_limited", "http_error")


def results_script(html: str) -> str | None:
    """The inline script holding the results (`AF_initDataCallback({key: 'ds:1', ...})`)."""
    node = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if node is not None:
        return node.text()
    # Fall back to finding it by its key, in case Google drops the class attribute.
    m = re.search(r"AF_initDataCallback\(\{key:\s*'ds:1'.*?\}\);", html, re.S)
    return m.group(0) if m else None


def classify(html: str, url: str = "", status: int = 200) -> str | None:
    """Why a page has no results, or None if it looks like a results page."""
    if results_script(html):
        return None
    low = html[:200_000].lower()
    if "consent.google" in url or "consent.google" in low or "before you continue" in low:
        return "consent"
    if "/sorry/" in url or "unusual traffic" in low or "recaptcha" in low:
        return "captcha"
    if status == 429:
        return "rate_limited"
    if status >= 400:
        return "http_error"
    return "no_payload"


def check_page(html: str, url: str = "", status: int = 200) -> str:
    kind = classify(html, url, status)
    if kind:
        raise PageError(kind, f"HTTP {status} {url[:80]}")
    return html


def save_debug(name: str, html: str) -> Path:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path = DEBUG_DIR / f"{re.sub(r'[^A-Za-z0-9_-]+', '_', name)}.html"
    path.write_text(html)
    return path


class HttpFetcher:
    name = "http"

    def __init__(self):
        self._local = threading.local()
        self._generation = 0

    def reset(self) -> None:
        """Drop the connections; each worker opens a fresh one on its next request."""
        self._generation += 1

    def _client(self):
        if getattr(self._local, "generation", None) != self._generation:
            self._local.generation = self._generation
            from primp import Client

            self._local.client = Client(impersonate="chrome_145", impersonate_os="macos", referer=True,
                                        cookie_store=True, cookies=CONSENT_COOKIES, timeout=30)
        return self._local.client

    def fetch(self, params: dict) -> tuple[str, str, int]:
        res = self._client().get(URL, params={**params, "gl": "GB"})
        return res.text, str(res.url), res.status_code


class BrowserFetcher:
    """Headless Chromium. Slower, but gets through anything a normal visit would.

    Playwright objects can't cross threads, so each browser lives on its own worker thread.
    """

    name = "browser"

    def __init__(self, browsers: int = 2):
        from concurrent.futures import ThreadPoolExecutor

        from playwright.sync_api import sync_playwright  # noqa: F401  (fail early if missing)

        self._local = threading.local()
        self._pool = ThreadPoolExecutor(max_workers=browsers, thread_name_prefix="browser")
        self._started: list = []
        atexit.register(self.close)

    def _page(self):
        if not hasattr(self._local, "page"):
            from playwright.sync_api import sync_playwright

            pw = sync_playwright().start()
            # CHROMIUM_PATH lets you use an already-installed Chrome/Chromium instead.
            browser = pw.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
            ctx = browser.new_context(locale="en-GB", timezone_id="Europe/London")
            ctx.add_cookies([{"name": k, "value": v, "domain": ".google.com", "path": "/"}
                             for k, v in CONSENT_COOKIES.items()])
            self._local.page = ctx.new_page()
            self._started.append((pw, browser))
        return self._local.page

    def _fetch(self, params: dict) -> tuple[str, str, int]:
        page = self._page()
        resp = page.goto(f"{URL}?{urlencode({**params, 'gl': 'GB'})}", wait_until="domcontentloaded",
                         timeout=45_000)
        if "consent.google" in page.url:
            for label in ("Accept all", "Reject all", "I agree"):
                button = page.get_by_role("button", name=label)
                if button.count():
                    button.first.click()
                    page.wait_for_load_state("domcontentloaded")
                    break
        try:
            page.wait_for_selector(r"script.ds\:1", state="attached", timeout=15_000)
        except Exception:
            pass
        return page.content(), page.url, resp.status if resp else 200

    def fetch(self, params: dict) -> tuple[str, str, int]:
        return self._pool.submit(self._fetch, params).result()

    def close(self) -> None:
        def stop(pw, browser):
            try:
                browser.close()
                pw.stop()
            except Exception:
                pass

        for pw, browser in self._started:
            try:
                self._pool.submit(stop, pw, browser).result(timeout=10)
            except Exception:
                pass
        self._started.clear()


def browser_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401

        return True
    except ImportError:
        return False
