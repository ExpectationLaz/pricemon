"""Polite page fetching: rate limit, retries, robots.txt and block detection.

Bot protection is never bypassed. A refused or challenged request is reported
as `blocked` and left alone.
"""

import re
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .config import Settings, SiteRules, hostname
from .models import BLOCKED, DISALLOWED, FETCH_ERROR, NOT_FOUND, OK, FetchResult

# Page titles used by bot-protection interstitials.
_CHALLENGE_TITLES = (
    "just a moment",
    "attention required",
    "access denied",
    "pardon our interruption",
    "robot check",
    "are you a robot",
)
# Markers that only appear on block pages, not on ordinary pages with a CAPTCHA widget.
_CHALLENGE_MARKERS = ("captcha-delivery.com", "px-captcha")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_FIRST_RETRY_DELAY = 2.0


def looks_like_challenge(html: str) -> bool:
    """True when the page is a bot-protection challenge instead of real content."""
    match = _TITLE.search(html)
    title = match.group(1).strip().lower() if match else ""
    if any(title.startswith(prefix) for prefix in _CHALLENGE_TITLES):
        return True
    lowered = html.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


def classify(status: int, html: str) -> FetchResult | None:
    """Turn a response into a result. None means the request may be retried."""
    if looks_like_challenge(html):
        return FetchResult(BLOCKED, status, error="challenge page")
    if status in (403, 429):
        return FetchResult(BLOCKED, status, error=f"HTTP {status}")
    if status in (404, 410):
        return FetchResult(NOT_FOUND, status, error=f"HTTP {status}")
    if status >= 500:
        return None
    if status >= 400:
        return FetchResult(FETCH_ERROR, status, error=f"HTTP {status}")
    return FetchResult(OK, status, html)


class Fetcher:
    """Fetches pages for one run."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None, clock=time.monotonic, sleep=time.sleep):
        self.settings = settings
        self.client = client or httpx.Client(
            headers={"User-Agent": settings.user_agent},
            timeout=settings.timeout_seconds,
            follow_redirects=True,
        )
        self._clock = clock
        self._sleep = sleep
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}

    def fetch(self, url: str, rules: SiteRules | None = None) -> FetchResult:
        host = hostname(url)
        interval = self.settings.min_interval_seconds
        if rules is not None and rules.min_interval_seconds is not None:
            interval = rules.min_interval_seconds
        if self.settings.respect_robots_txt and not self._allowed(url, host, interval):
            return FetchResult(DISALLOWED, error="disallowed by robots.txt")
        return self._fetch_http(url, host, interval)

    def _wait(self, host: str, interval: float) -> None:
        """Sleep until `interval` seconds have passed since the last request to this host."""
        last = self._last_request.get(host)
        if last is not None:
            remaining = interval - (self._clock() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request[host] = self._clock()

    def _allowed(self, url: str, host: str, interval: float) -> bool:
        if host not in self._robots:
            self._robots[host] = self._load_robots(url, host, interval)
        parser = self._robots[host]
        return True if parser is None else parser.can_fetch(self.settings.user_agent, url)

    def _load_robots(self, url: str, host: str, interval: float) -> RobotFileParser | None:
        """The parsed robots.txt for a host, or None when there is none to obey."""
        parts = urlsplit(url)
        self._wait(host, interval)
        try:
            response = self.client.get(f"{parts.scheme}://{parts.netloc}/robots.txt")
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        return parser

    def _fetch_http(self, url: str, host: str, interval: float) -> FetchResult:
        attempts = self.settings.retries + 1
        delay = _FIRST_RETRY_DELAY
        error = "no attempt made"
        http_status = None
        for attempt in range(attempts):
            self._wait(host, interval)
            try:
                response = self.client.get(url)
            except httpx.HTTPError as exc:
                error = f"{type(exc).__name__}: {exc}"
                http_status = None
            else:
                result = classify(response.status_code, response.text)
                if result is not None:
                    return result
                error = f"HTTP {response.status_code}"
                http_status = response.status_code
            if attempt < attempts - 1:
                self._sleep(delay)
                delay *= 2
        return FetchResult(FETCH_ERROR, http_status, error=error)
