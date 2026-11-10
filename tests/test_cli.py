import pytest
from helpers import Site, make_fetcher, page

from pricemon import cli
from pricemon.models import BLOCKED, OK, FetchResult

A = "https://a.example/p/drill"
B = "https://b.example/p/drill"

CONFIG = f"""\
client: acme-shop
products:
  - name: Drill
    urls:
      - {A}
      - {B}
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    """An empty project folder as the working directory, holding one client config."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "clients").mkdir()
    (tmp_path / "clients" / "acme-shop.yaml").write_text(CONFIG, encoding="utf-8")
    return tmp_path


class StubFetcher:
    def __init__(self, result):
        self.result = result
        self.rules = None

    def fetch(self, url, rules=None):
        self.rules = rules
        return self.result


def use_stub(monkeypatch, result):
    stub = StubFetcher(result)
    monkeypatch.setattr(cli, "build_fetcher", lambda settings: stub)
    return stub


def test_validate_lists_sites_and_their_rules(project, capsys):
    (project / "sites").mkdir()
    (project / "sites" / "b.yaml").write_text("domain: b.example\nprice: {css: .price}\n", encoding="utf-8")
    assert cli.main(["validate", "clients/acme-shop.yaml"]) == 0
    out = capsys.readouterr().out
    assert "OK: acme-shop: 1 product(s), 2 URL(s), 2 site(s)" in out
    assert "a.example: no site rules" in out
    assert "b.example: site rules found" in out


def test_invalid_config_exits_1(project, capsys):
    (project / "clients" / "bad.yaml").write_text("client: acme\n", encoding="utf-8")
    assert cli.main(["validate", "clients/bad.yaml"]) == 1
    assert "Config error" in capsys.readouterr().err


def test_check_url_prints_fields_and_their_sources(project, capsys, monkeypatch):
    use_stub(monkeypatch, FetchResult(OK, 200, page("119.00")))
    assert cli.main(["check-url", A]) == 0
    out = capsys.readouterr().out
    assert f"URL:        {A}" in out
    assert "Site rules: none" in out
    assert "Fetch:      ok (HTTP 200)" in out
    assert "Title:      Drill  [jsonld]" in out
    assert "Price:      119.00  [jsonld]" in out
    assert "Currency:   EUR  [jsonld]" in out
    assert "In stock:   yes  [jsonld]" in out
    assert "Verdict:    works" in out


def test_check_url_blocked_site_is_unsupported(project, capsys, monkeypatch):
    use_stub(monkeypatch, FetchResult(BLOCKED, 403, error="HTTP 403"))
    assert cli.main(["check-url", A]) == 0
    out = capsys.readouterr().out
    assert "Fetch:      blocked (HTTP 403)" in out
    assert "Error:      HTTP 403" in out
    assert "unsupported" in out


def test_check_url_without_price_asks_for_site_rules(project, capsys, monkeypatch):
    use_stub(monkeypatch, FetchResult(OK, 200, "<html><body><h1>Drill</h1></body></html>"))
    assert cli.main(["check-url", A]) == 0
    out = capsys.readouterr().out
    assert "no price found" in out
    assert "sites/a.example.yaml" in out


def test_check_url_browser_flag_selects_the_browser_fetcher(project, capsys, monkeypatch):
    stub = use_stub(monkeypatch, FetchResult(OK, 200, page("119.00")))
    assert cli.main(["check-url", A, "--browser"]) == 0
    assert stub.rules.fetcher == "browser"
    assert stub.rules.domain == "a.example"


def test_run_dry_run_writes_the_database_and_prints_a_summary(project, capsys, monkeypatch):
    site = Site({A: (200, page("119.00")), B: (200, page("125.00"))})
    monkeypatch.setattr(cli, "build_fetcher", lambda settings: make_fetcher(site))
    assert cli.main(["run", "clients/acme-shop.yaml", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "acme-shop: run 1 (dry run, nothing sent)" in out
    assert "URLs:   2 total, 2 ok, 0 failed" in out
    assert "Events: 0 (0 alerted, 0 to verify)" in out
    assert (project / "data" / "acme-shop" / "pricemon.db").exists()


def test_run_with_failed_pages_still_exits_0(project, capsys, monkeypatch):
    site = Site({A: (200, page("119.00"))})
    monkeypatch.setattr(cli, "build_fetcher", lambda settings: make_fetcher(site))
    assert cli.main(["run", "clients/acme-shop.yaml", "--dry-run"]) == 0
    assert "2 total, 1 ok, 1 failed" in capsys.readouterr().out


def test_fatal_error_exits_1(project, capsys, monkeypatch):
    def broken(settings):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "build_fetcher", broken)
    assert cli.main(["run", "clients/acme-shop.yaml"]) == 1
    assert "Fatal error: boom" in capsys.readouterr().err
