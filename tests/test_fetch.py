import httpx
import pytest

from pricemon.config import Settings, SiteRules
from pricemon.fetch import Fetcher, looks_like_challenge
from pricemon.models import BLOCKED, DISALLOWED, FETCH_ERROR, NOT_FOUND, OK

PAGE = "<html><head><title>Drill</title></head><body>ok</body></html>"
CHALLENGE = "<html><head><title>Just a moment...</title></head><body></body></html>"


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def make(handler, **overrides):
    """A Fetcher on a mock transport and a fake clock. Robots checks are off unless enabled."""
    clock = FakeClock()
    settings = Settings(**{"respect_robots_txt": False, **overrides})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return Fetcher(settings, client=client, clock=clock.now, sleep=clock.sleep), clock


def counting(responses):
    """A handler that returns the given (status, body) pairs in order, repeating the last."""
    calls = []

    def handler(request):
        calls.append(request.url.path)
        status, body = responses[min(len(calls), len(responses)) - 1]
        return httpx.Response(status, text=body)

    return handler, calls


def test_ok_page():
    handler, calls = counting([(200, PAGE)])
    fetcher, _ = make(handler)
    result = fetcher.fetch("https://a.example/p/1")
    assert (result.status, result.http_status, result.html, result.error) == (OK, 200, PAGE, None)
    assert calls == ["/p/1"]


@pytest.mark.parametrize("status", [404, 410])
def test_missing_page_is_not_found(status):
    handler, calls = counting([(status, "")])
    fetcher, _ = make(handler)
    result = fetcher.fetch("https://a.example/p/1")
    assert (result.status, result.http_status) == (NOT_FOUND, status)
    assert len(calls) == 1


@pytest.mark.parametrize("status", [403, 429])
def test_refused_request_is_blocked_and_not_retried(status):
    handler, calls = counting([(status, "")])
    fetcher, _ = make(handler)
    result = fetcher.fetch("https://a.example/p/1")
    assert (result.status, result.http_status, result.error) == (BLOCKED, status, f"HTTP {status}")
    assert len(calls) == 1


def test_other_client_error_is_fetch_error_and_not_retried():
    handler, calls = counting([(400, "")])
    fetcher, _ = make(handler)
    result = fetcher.fetch("https://a.example/p/1")
    assert (result.status, result.error) == (FETCH_ERROR, "HTTP 400")
    assert len(calls) == 1


def test_server_errors_are_retried_with_doubling_delay():
    handler, calls = counting([(500, ""), (502, ""), (200, PAGE)])
    fetcher, clock = make(handler, min_interval_seconds=0)
    result = fetcher.fetch("https://a.example/p/1")
    assert result.status == OK
    assert len(calls) == 3
    assert clock.sleeps == [2.0, 4.0]


def test_server_error_after_all_retries_is_fetch_error():
    handler, calls = counting([(500, "")])
    fetcher, _ = make(handler, min_interval_seconds=0)
    result = fetcher.fetch("https://a.example/p/1")
    assert (result.status, result.http_status, result.error) == (FETCH_ERROR, 500, "HTTP 500")
    assert len(calls) == 3


def test_network_error_is_retried_then_fetch_error():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("boom", request=request)

    fetcher, _ = make(handler, min_interval_seconds=0, retries=1)
    result = fetcher.fetch("https://a.example/p/1")
    assert result.status == FETCH_ERROR
    assert "ConnectError" in result.error
    assert len(calls) == 2


@pytest.mark.parametrize("status", [200, 503])
def test_challenge_page_is_blocked_and_not_retried(status):
    handler, calls = counting([(status, CHALLENGE)])
    fetcher, _ = make(handler, min_interval_seconds=0)
    result = fetcher.fetch("https://a.example/p/1")
    assert (result.status, result.error) == (BLOCKED, "challenge page")
    assert len(calls) == 1


def test_challenge_detection_ignores_ordinary_captcha_widgets():
    page = '<html><head><title>Drill</title></head><body><div class="g-recaptcha"></div></body></html>'
    assert looks_like_challenge(page) is False
    assert looks_like_challenge(CHALLENGE) is True
    assert looks_like_challenge('<html><body><script src="https://geo.captcha-delivery.com/x"></script></body></html>') is True


def test_requests_to_one_host_are_spaced_out():
    handler, _ = counting([(200, PAGE)])
    fetcher, clock = make(handler, min_interval_seconds=5)
    fetcher.fetch("https://a.example/p/1")
    fetcher.fetch("https://www.a.example/p/2")
    assert clock.sleeps == [5.0]


def test_different_hosts_do_not_wait_for_each_other():
    handler, _ = counting([(200, PAGE)])
    fetcher, clock = make(handler, min_interval_seconds=5)
    fetcher.fetch("https://a.example/p/1")
    fetcher.fetch("https://b.example/p/1")
    assert clock.sleeps == []


def test_site_rules_can_override_the_interval():
    handler, _ = counting([(200, PAGE)])
    fetcher, clock = make(handler, min_interval_seconds=5)
    rules = SiteRules(domain="a.example", min_interval_seconds=1)
    fetcher.fetch("https://a.example/p/1", rules)
    fetcher.fetch("https://a.example/p/2", rules)
    assert clock.sleeps == [1.0]


def robots_site(robots_status, robots_body):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(robots_status, text=robots_body)
        return httpx.Response(200, text=PAGE)

    return handler, calls


def test_robots_disallow_is_respected_and_fetched_once():
    handler, calls = robots_site(200, "User-agent: *\nDisallow: /private/\n")
    fetcher, _ = make(handler, respect_robots_txt=True, min_interval_seconds=0)
    blocked = fetcher.fetch("https://a.example/private/x")
    allowed = fetcher.fetch("https://a.example/p/1")
    assert (blocked.status, blocked.error) == (DISALLOWED, "disallowed by robots.txt")
    assert allowed.status == OK
    assert calls == ["/robots.txt", "/p/1"]


def test_missing_robots_file_allows_everything():
    handler, calls = robots_site(404, "")
    fetcher, _ = make(handler, respect_robots_txt=True, min_interval_seconds=0)
    assert fetcher.fetch("https://a.example/private/x").status == OK
    assert calls == ["/robots.txt", "/private/x"]


def test_robots_is_ignored_when_disabled():
    handler, calls = robots_site(200, "User-agent: *\nDisallow: /\n")
    fetcher, _ = make(handler, respect_robots_txt=False)
    assert fetcher.fetch("https://a.example/p/1").status == OK
    assert calls == ["/p/1"]


@pytest.mark.parametrize("status", [500, 503])
def test_unreadable_robots_file_stops_the_host_for_this_run(status):
    handler, calls = robots_site(status, "")
    fetcher, _ = make(handler, respect_robots_txt=True, min_interval_seconds=0)
    first = fetcher.fetch("https://a.example/p/1")
    second = fetcher.fetch("https://a.example/p/2")
    assert (first.status, first.http_status, first.error) == (
        FETCH_ERROR, status, f"robots.txt unavailable: HTTP {status}",
    )
    assert second == first
    assert calls == ["/robots.txt"]


def test_robots_network_error_stops_the_host_for_this_run():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, text=PAGE)

    fetcher, _ = make(handler, respect_robots_txt=True, min_interval_seconds=0)
    result = fetcher.fetch("https://a.example/p/1")
    assert (result.status, result.http_status, result.error) == (
        FETCH_ERROR, None, "robots.txt unavailable: ConnectError",
    )
    assert fetcher.fetch("https://a.example/p/2") == result
    assert calls == ["/robots.txt"]


def test_robots_429_marks_the_host_blocked():
    handler, calls = robots_site(429, "")
    fetcher, _ = make(handler, respect_robots_txt=True, min_interval_seconds=0)
    result = fetcher.fetch("https://a.example/p/1")
    assert (result.status, result.http_status, result.error) == (BLOCKED, 429, "robots.txt: HTTP 429")
    assert fetcher.fetch("https://a.example/p/2") == result
    assert calls == ["/robots.txt"]


def test_robots_403_means_no_rules():
    handler, calls = robots_site(403, "")
    fetcher, _ = make(handler, respect_robots_txt=True, min_interval_seconds=0)
    assert fetcher.fetch("https://a.example/p/1").status == OK
    assert calls == ["/robots.txt", "/p/1"]


def test_a_failing_robots_file_on_one_host_does_not_affect_another():
    def handler(request):
        if request.url.host == "a.example" and request.url.path == "/robots.txt":
            return httpx.Response(500, text="")
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="")
        return httpx.Response(200, text=PAGE)

    fetcher, _ = make(handler, respect_robots_txt=True, min_interval_seconds=0)
    assert fetcher.fetch("https://a.example/p/1").status == FETCH_ERROR
    assert fetcher.fetch("https://b.example/p/1").status == OK


# redirects


def redirecting(routes, default=(200, PAGE)):
    """A handler for a fake web: maps 'host/path' to (status, body-or-location). Records 'host/path' requests."""
    calls = []

    def handler(request):
        key = f"{request.url.host}{request.url.path}"
        calls.append(key)
        status, value = routes.get(key, default)
        if 300 <= status < 400:
            return httpx.Response(status, headers={"Location": value} if value else {})
        return httpx.Response(status, text=value)

    return handler, calls


def test_a_same_host_redirect_is_followed_with_the_rate_limit_between_requests():
    handler, calls = redirecting({"a.example/old": (301, "/new")})
    fetcher, clock = make(handler, min_interval_seconds=5)
    result = fetcher.fetch("https://a.example/old")
    assert (result.status, result.html) == (OK, PAGE)
    assert calls == ["a.example/old", "a.example/new"]
    assert clock.sleeps == [5.0]


@pytest.mark.parametrize(
    "location",
    ["https://a.example/new", "https://www.a.example/new", "http://a.example/new", "new"],
)
def test_same_host_covers_absolute_www_and_scheme_changes_and_relative_targets(location):
    handler, calls = redirecting({"a.example/old": (302, location), "www.a.example/old": (302, location)})
    fetcher, _ = make(handler)
    assert fetcher.fetch("https://a.example/old").status == OK
    assert len(calls) == 2 and calls[1].endswith("/new")


def test_a_redirect_to_another_site_is_an_error_and_that_site_gets_no_request():
    handler, calls = redirecting({"a.example/old": (301, "https://b.example/product")})
    fetcher, _ = make(handler)
    result = fetcher.fetch("https://a.example/old")
    assert (result.status, result.http_status, result.html) == (FETCH_ERROR, 301, None)
    assert result.error == "redirected to another site: b.example"
    assert calls == ["a.example/old"]


def test_an_injected_client_that_follows_redirects_itself_is_not_trusted_to():
    handler, calls = redirecting({"a.example/old": (301, "https://b.example/product")})
    clock = FakeClock()
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    fetcher = Fetcher(Settings(respect_robots_txt=False), client=client, clock=clock.now, sleep=clock.sleep)
    assert fetcher.fetch("https://a.example/old").error == "redirected to another site: b.example"
    assert calls == ["a.example/old"]


def test_a_same_host_redirect_to_a_path_robots_disallows_is_disallowed():
    handler, calls = redirecting(
        {
            "a.example/robots.txt": (200, "User-agent: *\nDisallow: /private/\n"),
            "a.example/old": (301, "/private/x"),
        }
    )
    fetcher, _ = make(handler, respect_robots_txt=True, min_interval_seconds=0)
    result = fetcher.fetch("https://a.example/old")
    assert (result.status, result.error) == (DISALLOWED, "disallowed by robots.txt")
    assert calls == ["a.example/robots.txt", "a.example/old"]


def test_a_redirect_loop_gives_up():
    handler, calls = redirecting({"a.example/a": (302, "/b"), "a.example/b": (302, "/a")})
    fetcher, _ = make(handler)
    result = fetcher.fetch("https://a.example/a")
    assert (result.status, result.error) == (FETCH_ERROR, "too many redirects")
    assert len(calls) == 6  # the first request and five hops


def test_five_hops_are_allowed_and_do_not_use_up_the_retries():
    routes = {f"a.example/{n}": (302, f"/{n + 1}") for n in range(5)}
    handler, calls = redirecting(routes)
    fetcher, clock = make(handler, retries=0, min_interval_seconds=0)
    assert fetcher.fetch("https://a.example/0").status == OK
    assert len(calls) == 6 and clock.sleeps == []


def test_a_redirect_without_a_location_is_a_fetch_error():
    handler, calls = redirecting({"a.example/old": (302, "")})
    fetcher, _ = make(handler)
    result = fetcher.fetch("https://a.example/old")
    assert (result.status, result.http_status, result.error) == (FETCH_ERROR, 302, "HTTP 302")
    assert len(calls) == 1


def test_robots_txt_that_redirects_on_the_same_host_is_followed_once():
    handler, calls = redirecting(
        {
            "a.example/robots.txt": (301, "/rules.txt"),
            "a.example/rules.txt": (200, "User-agent: *\nDisallow: /private/\n"),
        }
    )
    fetcher, _ = make(handler, respect_robots_txt=True, min_interval_seconds=0)
    assert fetcher.fetch("https://a.example/private/x").status == DISALLOWED
    assert calls == ["a.example/robots.txt", "a.example/rules.txt"]


def test_robots_txt_redirecting_elsewhere_or_twice_means_no_rules():
    handler, calls = redirecting({"a.example/robots.txt": (301, "https://b.example/robots.txt")})
    fetcher, _ = make(handler, respect_robots_txt=True, min_interval_seconds=0)
    assert fetcher.fetch("https://a.example/p/1").status == OK
    assert "b.example/robots.txt" not in calls

    handler, calls = redirecting(
        {
            "a.example/robots.txt": (301, "/one.txt"),
            "a.example/one.txt": (301, "/two.txt"),
            "a.example/two.txt": (200, "User-agent: *\nDisallow: /\n"),
        }
    )
    fetcher, _ = make(handler, respect_robots_txt=True, min_interval_seconds=0)
    assert fetcher.fetch("https://a.example/p/1").status == OK
    assert "a.example/two.txt" not in calls


import sys
import types

BROWSER_RULES = SiteRules(domain="a.example", fetcher="browser")


def install_fake_playwright(
    monkeypatch, status=200, html=PAGE, launch_error=None, first_body=None, load_state_error=None, body_error=None
):
    """Put a stand-in for playwright.sync_api into sys.modules. Returns the list of what the fake saw.

    `first_body` is the navigation response body (defaults to `html`, the final DOM).
    """
    visited = []
    first_body = html if first_body is None else first_body

    class FakeResponse:
        def __init__(self):
            self.status = status

        def text(self):
            if body_error:
                raise body_error
            return first_body

    class FakePage:
        def goto(self, url, timeout=None, wait_until=None):
            visited.append((url, timeout, wait_until))
            return FakeResponse()

        def wait_for_load_state(self, state=None, timeout=None):
            visited.append(("wait", state, timeout))
            if load_state_error:
                raise load_state_error

        def content(self):
            return html

    class FakeBrowser:
        def new_page(self, user_agent=None):
            return FakePage()

        def close(self):
            visited.append("closed")

    class FakeChromium:
        def launch(self):
            if launch_error:
                raise launch_error
            return FakeBrowser()

    class FakeManager:
        def __enter__(self):
            return types.SimpleNamespace(chromium=FakeChromium())

        def __exit__(self, *exc):
            return False

    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: FakeManager()
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    return visited


def no_http(request):
    raise AssertionError("the browser fetcher must not use the HTTP client")


def test_browser_fetcher_returns_rendered_html(monkeypatch):
    visited = install_fake_playwright(monkeypatch)
    fetcher, _ = make(no_http, timeout_seconds=20)
    result = fetcher.fetch("https://a.example/p/1", BROWSER_RULES)
    assert (result.status, result.http_status, result.html) == (OK, 200, PAGE)
    assert visited == [("https://a.example/p/1", 20000, "load"), ("wait", "networkidle", 5000), "closed"]


def test_browser_fetcher_reports_a_blocked_page(monkeypatch):
    install_fake_playwright(monkeypatch, status=403, html="")
    fetcher, _ = make(no_http)
    result = fetcher.fetch("https://a.example/p/1", BROWSER_RULES)
    assert (result.status, result.error) == (BLOCKED, "HTTP 403")


def test_browser_failure_is_a_fetch_error(monkeypatch):
    install_fake_playwright(monkeypatch, launch_error=RuntimeError("chromium is not installed"))
    fetcher, _ = make(no_http)
    result = fetcher.fetch("https://a.example/p/1", BROWSER_RULES)
    assert result.status == FETCH_ERROR
    assert result.error == "browser: RuntimeError: chromium is not installed"


def test_browser_fetcher_without_playwright_explains_how_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)  # makes the import fail
    fetcher, _ = make(no_http)
    result = fetcher.fetch("https://a.example/p/1", BROWSER_RULES)
    assert result.status == FETCH_ERROR
    assert 'pip install "pricemon[browser]"' in result.error
    assert "playwright install chromium" in result.error


def test_a_challenge_in_the_first_response_is_blocked_even_if_the_final_page_looks_normal(monkeypatch):
    install_fake_playwright(monkeypatch, html=PAGE, first_body=CHALLENGE)
    fetcher, _ = make(no_http)
    result = fetcher.fetch("https://a.example/p/1", BROWSER_RULES)
    assert (result.status, result.error, result.html) == (BLOCKED, "challenge page", None)


def test_a_challenge_in_the_final_page_is_still_blocked(monkeypatch):
    install_fake_playwright(monkeypatch, html=CHALLENGE, first_body=PAGE)
    fetcher, _ = make(no_http)
    assert fetcher.fetch("https://a.example/p/1", BROWSER_RULES).status == BLOCKED


def test_a_page_that_never_goes_idle_does_not_fail_the_fetch(monkeypatch):
    install_fake_playwright(monkeypatch, load_state_error=TimeoutError("networkidle"))
    fetcher, _ = make(no_http)
    result = fetcher.fetch("https://a.example/p/1", BROWSER_RULES)
    assert (result.status, result.html) == (OK, PAGE)


def test_an_unreadable_first_response_is_not_treated_as_a_challenge(monkeypatch):
    install_fake_playwright(monkeypatch, body_error=RuntimeError("body not available"))
    fetcher, _ = make(no_http)
    result = fetcher.fetch("https://a.example/p/1", BROWSER_RULES)
    assert (result.status, result.html) == (OK, PAGE)


def test_robots_txt_is_checked_before_the_browser_is_launched(monkeypatch):
    visited = install_fake_playwright(monkeypatch)
    handler, calls = robots_site(200, "User-agent: *\nDisallow: /private/\n")
    fetcher, _ = make(handler, respect_robots_txt=True, min_interval_seconds=0)
    result = fetcher.fetch("https://a.example/private/x", BROWSER_RULES)
    assert (result.status, result.error) == (DISALLOWED, "disallowed by robots.txt")
    assert visited == [] and calls == ["/robots.txt"]
