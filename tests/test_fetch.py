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
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
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
