import pytest
from openpyxl import load_workbook
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


def test_run_prints_the_report_path(project, capsys, monkeypatch):
    site = Site({A: (200, page("119.00")), B: (200, page("125.00"))})
    monkeypatch.setattr(cli, "build_fetcher", lambda settings: make_fetcher(site))
    assert cli.main(["run", "clients/acme-shop.yaml", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Report: " in out and ".xlsx" in out


def test_report_command_rebuilds_todays_report(project, capsys, monkeypatch):
    site = Site({A: (200, page("119.00")), B: (200, page("125.00"))})
    monkeypatch.setattr(cli, "build_fetcher", lambda settings: make_fetcher(site))
    cli.main(["run", "clients/acme-shop.yaml"])
    for old in (project / "data" / "acme-shop" / "reports").iterdir():
        old.unlink()
    requests_before = len(site.requested)
    capsys.readouterr()
    assert cli.main(["report", "clients/acme-shop.yaml"]) == 0
    out = capsys.readouterr().out
    assert ".xlsx" in out
    assert len(list((project / "data" / "acme-shop" / "reports").iterdir())) == 2
    assert len(site.requested) == requests_before  # nothing was fetched


def test_report_command_without_a_run_on_that_date(project, capsys, monkeypatch):
    site = Site({A: (200, page("119.00")), B: (200, page("125.00"))})
    monkeypatch.setattr(cli, "build_fetcher", lambda settings: make_fetcher(site))
    cli.main(["run", "clients/acme-shop.yaml"])
    capsys.readouterr()
    assert cli.main(["report", "clients/acme-shop.yaml", "--date", "2000-01-01"]) == 1
    assert "No run found on 2000-01-01" in capsys.readouterr().err


def test_report_command_without_any_data(project, capsys):
    assert cli.main(["report", "clients/acme-shop.yaml"]) == 1
    assert "No data for client acme-shop" in capsys.readouterr().err


def test_report_command_uses_the_real_run_not_a_later_dry_run(project, capsys, monkeypatch):
    site = Site({A: (200, page("119.00")), B: (200, page("125.00"))})
    monkeypatch.setattr(cli, "build_fetcher", lambda settings: make_fetcher(site))
    assert cli.main(["run", "clients/acme-shop.yaml"]) == 0
    site.pages[A] = (200, page("109.00"))
    assert cli.main(["run", "clients/acme-shop.yaml", "--dry-run"]) == 0
    reports = project / "data" / "acme-shop" / "reports"
    names = sorted(p.name for p in reports.iterdir())
    assert len(names) == 4
    assert sum("dry-run" in name for name in names) == 2

    capsys.readouterr()
    assert cli.main(["report", "clients/acme-shop.yaml"]) == 0
    assert "dry-run" not in capsys.readouterr().out
    real_xlsx = next(p for p in reports.iterdir() if p.suffix == ".xlsx" and "dry-run" not in p.name)
    prices = {
        row[-1]: row[5] for row in load_workbook(real_xlsx)["Prices"].iter_rows(min_row=2, values_only=True)
    }
    assert prices[A] == 119

    assert cli.main(["report", "clients/acme-shop.yaml", "--dry-run"]) == 0
    assert "dry-run" in capsys.readouterr().out


def test_report_command_without_a_dry_run_on_that_date(project, capsys, monkeypatch):
    site = Site({A: (200, page("119.00")), B: (200, page("125.00"))})
    monkeypatch.setattr(cli, "build_fetcher", lambda settings: make_fetcher(site))
    cli.main(["run", "clients/acme-shop.yaml"])
    capsys.readouterr()
    assert cli.main(["report", "clients/acme-shop.yaml", "--dry-run"]) == 1
    assert "No dry run found on" in capsys.readouterr().err


def test_report_command_rejects_a_malformed_date(project, capsys):
    assert cli.main(["report", "clients/acme-shop.yaml", "--date", "yesterday"]) == 1
    assert "Invalid date 'yesterday': use YYYY-MM-DD" in capsys.readouterr().err


def test_run_notifies_unless_dry_run(project, capsys, monkeypatch):
    site = Site({A: (200, page("119.00")), B: (200, page("125.00"))})
    calls = []

    class FakeNotifier:
        def send(self, config, store, run_id, events, report_path):
            calls.append(run_id)
            return ["telegram skipped: TELEGRAM_BOT_TOKEN missing in .env"]

    monkeypatch.setattr(cli, "build_fetcher", lambda settings: make_fetcher(site))
    monkeypatch.setattr(cli, "build_notifier", lambda settings: FakeNotifier())
    assert cli.main(["run", "clients/acme-shop.yaml", "--dry-run"]) == 0
    assert calls == []
    capsys.readouterr()
    assert cli.main(["run", "clients/acme-shop.yaml"]) == 0
    assert calls == [2]
    assert "Warning: telegram skipped: TELEGRAM_BOT_TOKEN missing in .env" in capsys.readouterr().out


def test_dotenv_file_is_loaded(project, monkeypatch):
    import os

    (project / ".env").write_text("PRICEMON_TEST_MARKER=loaded\n", encoding="utf-8")
    try:
        assert cli.main(["validate", "clients/acme-shop.yaml"]) == 0
        assert os.environ.get("PRICEMON_TEST_MARKER") == "loaded"
    finally:
        os.environ.pop("PRICEMON_TEST_MARKER", None)
