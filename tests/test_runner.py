from decimal import Decimal

from helpers import Site, make_fetcher, page

from pricemon.config import ClientConfig
from pricemon.models import EXTRACT_ERROR, FETCH_ERROR, OK
from pricemon.runner import observe, order_round_robin, run_client
from pricemon.store import Store

A = "https://a.example/p/drill"
B = "https://b.example/p/drill"
C = "https://a.example/p/saw"

CONFIG = ClientConfig.model_validate(
    {"client": "acme", "products": [{"name": "Drill", "urls": [A, B]}, {"name": "Saw", "urls": [C]}]}
)


def healthy_site():
    return Site({A: (200, page("119.00")), B: (200, page("125.00")), C: (200, page("80.00", name="Saw"))})


def run(site, tmp_path, **kwargs):
    return run_client(CONFIG, site_rules={}, data_dir=tmp_path / "data", fetcher=make_fetcher(site), **kwargs)


def open_store(tmp_path):
    return Store(tmp_path / "data" / "acme" / "pricemon.db")


def test_round_robin_interleaves_hosts():
    items = [(A, "Drill"), (C, "Saw"), (B, "Drill")]
    assert order_round_robin(items) == [(A, "Drill"), (B, "Drill"), (C, "Saw")]
    assert order_round_robin([]) == []


def test_first_run_is_a_baseline(tmp_path):
    summary = run(healthy_site(), tmp_path)
    assert (summary.client, summary.run_id, summary.dry_run) == ("acme", 1, False)
    assert (summary.urls_total, summary.urls_ok, summary.urls_failed) == (3, 3, 0)
    assert summary.events == []
    store = open_store(tmp_path)
    observations = store.run_observations(1)
    stored_run = store.run(1)
    store.close()
    assert [o.url for o in observations] == [A, B, C]
    assert all(o.status == OK and o.method == "jsonld" for o in observations)
    assert (observations[0].price, observations[0].product_name) == (Decimal("119.00"), "Drill")
    assert observations[2].product_name == "Saw"
    assert stored_run["finished_at"] is not None
    assert (stored_run["urls_total"], stored_run["urls_ok"], stored_run["dry_run"]) == (3, 3, 0)


def test_second_run_detects_changes_and_survives_failures(tmp_path):
    site = healthy_site()
    run(site, tmp_path)
    site.pages[A] = (200, page("109.00"))
    site.pages[B] = (500, "")
    site.pages[C] = (200, "<html><body><h1>Saw</h1></body></html>")
    summary = run(site, tmp_path)
    assert (summary.urls_total, summary.urls_ok, summary.urls_failed) == (3, 1, 2)
    assert [(e.type, e.url, e.alerted) for e in summary.events] == [("price_down", A, True)]
    store = open_store(tmp_path)
    observations = {o.url: o for o in store.run_observations(2)}
    events = store.run_events(2)
    store.close()
    assert {url: o.status for url, o in observations.items()} == {A: OK, B: FETCH_ERROR, C: EXTRACT_ERROR}
    assert observations[B].error == "HTTP 500"
    assert "no price found" in observations[C].error
    assert [(e.type, e.old_price, e.new_price) for e in events] == [
        ("price_down", Decimal("119.00"), Decimal("109.00"))
    ]


def test_recovery_compares_with_the_last_good_price(tmp_path):
    site = healthy_site()
    run(site, tmp_path)
    site.pages[A] = (500, "")
    assert run(site, tmp_path).events == []
    site.pages[A] = (200, page("109.00"))
    summary = run(site, tmp_path)
    assert [(e.type, e.old_price) for e in summary.events] == [("price_down", Decimal("119.00"))]


def test_stock_change_is_detected(tmp_path):
    site = healthy_site()
    run(site, tmp_path)
    site.pages[B] = (200, page("125.00", availability="OutOfStock"))
    summary = run(site, tmp_path)
    assert [(e.type, e.url) for e in summary.events] == [("out_of_stock", B)]


def test_dry_run_is_recorded(tmp_path):
    summary = run(healthy_site(), tmp_path, dry_run=True)
    assert summary.dry_run is True
    store = open_store(tmp_path)
    assert store.run(1)["dry_run"] == 1
    store.close()


def test_a_dry_run_does_not_become_the_baseline_for_real_runs(tmp_path):
    site = healthy_site()
    run(site, tmp_path)  # real baseline: A is 119.00
    site.pages[A] = (200, page("109.00"))
    dry = run(site, tmp_path, dry_run=True)  # previews the drop
    assert [(e.type, e.old_price) for e in dry.events] == [("price_down", Decimal("119.00"))]
    real = run(site, tmp_path)  # must still report the drop against 119.00
    assert [(e.type, e.old_price) for e in real.events] == [("price_down", Decimal("119.00"))]


def test_first_real_run_after_only_dry_runs_is_a_baseline(tmp_path):
    site = healthy_site()
    run(site, tmp_path, dry_run=True)
    site.pages[A] = (200, page("109.00"))
    assert run(site, tmp_path).events == []


def test_extractor_crash_is_one_failed_page(tmp_path, monkeypatch):
    def boom(html, url, rules):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr("pricemon.runner.extract", boom)
    summary = run(healthy_site(), tmp_path)
    assert (summary.urls_ok, summary.urls_failed) == (0, 3)
    store = open_store(tmp_path)
    observations = store.run_observations(1)
    store.close()
    assert all(o.status == EXTRACT_ERROR and "parser exploded" in o.error for o in observations)


def test_fetcher_crash_is_one_failed_page():
    class BrokenFetcher:
        def fetch(self, url, rules=None):
            raise RuntimeError("socket exploded")

    obs = observe(A, "Drill", BrokenFetcher(), None)
    assert obs.status == FETCH_ERROR
    assert "socket exploded" in obs.error


def test_run_writes_the_report(tmp_path):
    summary = run(healthy_site(), tmp_path)
    assert [path.rsplit(".", 1)[1] for path in summary.report_paths] == ["xlsx", "csv"]
    reports = tmp_path / "data" / "acme" / "reports"
    assert sorted(p.suffix for p in reports.iterdir()) == [".csv", ".xlsx"]
