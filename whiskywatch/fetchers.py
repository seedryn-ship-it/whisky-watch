"""HTTP 가져오기: 기본은 requests, 차단이 심한 샵은 선택적으로 Playwright."""

from __future__ import annotations

import random
import time
import urllib.robotparser
from urllib.parse import urlparse

import requests


class FetchError(Exception):
    pass


class BlockedError(FetchError):
    """봇 차단(403/429/캡차 등)으로 보이는 응답."""


_BLOCK_MARKERS = (
    "cf-chl", "just a moment", "captcha", "access denied", "attention required",
    "px-captcha", "are you a robot", "unusual traffic",
)


def _looks_blocked(text: str) -> bool:
    head = text[:6000].lower()
    return len(text) < 80_000 and any(m in head for m in _BLOCK_MARKERS)


class RequestsFetcher:
    kind = "requests"

    def __init__(
        self,
        user_agent: str,
        timeout: float = 25,
        delay: tuple[float, float] = (1.5, 3.5),
        retries: int = 2,
        respect_robots: bool = True,
    ):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-GB,en;q=0.9",
            }
        )
        self.timeout = timeout
        self.delay = delay
        self.retries = retries
        self.respect_robots = respect_robots
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._ua = user_agent

    def _allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        p = urlparse(url)
        root = f"{p.scheme}://{p.netloc}"
        if root not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.session.get(f"{root}/robots.txt", timeout=self.timeout)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                    self._robots[root] = rp
                else:
                    self._robots[root] = None  # robots.txt 없음 -> 허용
            except requests.RequestException:
                self._robots[root] = None
        rp = self._robots[root]
        return True if rp is None else rp.can_fetch(self._ua, url)

    def get(self, url: str) -> str:
        if not self._allowed(url):
            raise FetchError(f"robots.txt 가 접근을 허용하지 않음: {url}")
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            time.sleep(random.uniform(*self.delay))
            try:
                r = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as e:
                last = e
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (403, 429):
                raise BlockedError(f"HTTP {r.status_code}: {url}")
            if r.status_code >= 500:
                last = FetchError(f"HTTP {r.status_code}: {url}")
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise FetchError(f"HTTP {r.status_code}: {url}")
            if _looks_blocked(r.text):
                raise BlockedError(f"봇 차단 페이지로 보임: {url}")
            return r.text
        raise FetchError(f"요청 실패: {url} ({last})")

    def close(self) -> None:
        self.session.close()


class PlaywrightFetcher:
    """requests 가 차단될 때만 쓰는 헤드리스 브라우저 (config: fetch: playwright)."""

    kind = "playwright"

    def __init__(self, user_agent: str, timeout: float = 30, delay: tuple[float, float] = (1.5, 3.5)):
        self._ua = user_agent
        self.timeout_ms = int(timeout * 1000)
        self.delay = delay
        self._pw = None
        self._browser = None
        self._ctx = None

    def _start(self) -> None:
        if self._ctx is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover
            raise FetchError("playwright 가 설치되어 있지 않습니다 (pip install playwright)") from e
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._ctx = self._browser.new_context(user_agent=self._ua, locale="en-GB")

    def get(self, url: str) -> str:
        self._start()
        time.sleep(random.uniform(*self.delay))
        page = self._ctx.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            if resp is not None and resp.status in (403, 429):
                raise BlockedError(f"HTTP {resp.status}: {url}")
            if resp is not None and resp.status >= 400:
                raise FetchError(f"HTTP {resp.status}: {url}")
            html = page.content()
        except FetchError:
            raise
        except Exception as e:
            raise FetchError(f"브라우저 요청 실패: {url} ({e})") from e
        finally:
            page.close()
        if _looks_blocked(html):
            raise BlockedError(f"봇 차단 페이지로 보임: {url}")
        return html

    def close(self) -> None:
        for obj in (self._ctx, self._browser):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._pw = self._browser = self._ctx = None


class FetcherPool:
    """샵별 fetch 설정(requests/playwright)에 맞는 fetcher 를 지연 생성."""

    def __init__(self, user_agent: str, timeout: float, delay: tuple[float, float], respect_robots: bool):
        self._args = (user_agent, timeout, delay)
        self._respect_robots = respect_robots
        self._pool: dict[str, object] = {}

    def get(self, kind: str):
        if kind not in self._pool:
            ua, timeout, delay = self._args
            if kind == "playwright":
                self._pool[kind] = PlaywrightFetcher(ua, timeout, delay)
            else:
                self._pool[kind] = RequestsFetcher(ua, timeout, delay, respect_robots=self._respect_robots)
        return self._pool[kind]

    def close(self) -> None:
        for f in self._pool.values():
            f.close()
        self._pool.clear()
