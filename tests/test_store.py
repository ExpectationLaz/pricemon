from decimal import Decimal

import pytest

from pricemon.models import FETCH_ERROR, OK, Event, Observation
from pricemon.store import Store

A = "https://a.example/p/1"
B = "https://b.example/p/1"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "acme" / "pricemon.db")
    yield s
    s.close()


def ok(url, price, in_stock=True, currency="EUR"):
    return Observation(
        url=url, product_name="Drill", fetched_at="2026-11-10T07:00:00+00:00", status=OK,
        http_status=200, title="Drill", price=Decimal(price), currency=currency,
        in_stock=in_stock, method="jsonld",
    )


def failed(url, error="HTTP 500"):
    return Observation(
        url=url, product_name="Drill", fetched_at="2026-11-10T07:00:00+00:00", status=FETCH_ERROR,
        http_status=500, error=error,
    )


def test_store_creates_its_folder_and_tracks_runs(store):
    run_id = store.start_run("2026-11-10T07:00:00+00:00", dry_run=True)
    store.finish_run(run_id, "2026-11-10T07:01:00+00:00", urls_total=3, urls_ok=2, urls_failed=1)
    run = store.run(run_id)
    assert run["started_at"] == "2026-11-10T07:00:00+00:00"
    assert run["finished_at"] == "2026-11-10T07:01:00+00:00"
    assert (run["urls_total"], run["urls_ok"], run["urls_failed"], run["dry_run"]) == (3, 2, 1, 1)
    second = store.start_run("2026-11-11T07:00:00+00:00")
    assert [r["id"] for r in store.runs()] == [run_id, second]
    assert store.run(second)["dry_run"] == 0


def test_observation_round_trip_keeps_types(store):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")
    saved = store.add_observation(run_id, ok(A, "119.90", in_stock=False))
    unknown = store.add_observation(run_id, ok(B, "5", in_stock=None, currency=None))
    assert saved.id is not None and unknown.id != saved.id
    rows = store.run_observations(run_id)
    assert rows[0] == saved
    assert rows[0].price == Decimal("119.90") and str(rows[0].price) == "119.90"
    assert rows[0].in_stock is False
    assert rows[1].in_stock is None and rows[1].currency is None


def test_failed_observation_round_trip(store):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")
    saved = store.add_observation(run_id, failed(A))
    row = store.run_observations(run_id)[0]
    assert row == saved
    assert (row.status, row.price, row.error) == (FETCH_ERROR, None, "HTTP 500")


def test_last_ok_skips_failures_and_respects_before_run_id(store):
    assert store.last_ok(A) is None
    run1 = store.start_run("2026-11-10T07:00:00+00:00")
    store.add_observation(run1, ok(A, "119"))
    run2 = store.start_run("2026-11-11T07:00:00+00:00")
    store.add_observation(run2, failed(A))
    run3 = store.start_run("2026-11-12T07:00:00+00:00")
    store.add_observation(run3, ok(A, "109"))
    assert store.last_ok(A).price == Decimal("109")
    assert store.last_ok(A, before_run_id=run3).price == Decimal("119")
    assert store.last_ok(A, before_run_id=run1) is None
    assert store.last_ok(B) is None


def test_consecutive_failures_counts_back_to_the_last_ok(store):
    run1 = store.start_run("2026-11-10T07:00:00+00:00")
    store.add_observation(run1, ok(A, "119"))
    assert store.consecutive_failures(A) == 0
    run2 = store.start_run("2026-11-11T07:00:00+00:00")
    store.add_observation(run2, failed(A))
    run3 = store.start_run("2026-11-12T07:00:00+00:00")
    store.add_observation(run3, failed(A))
    assert store.consecutive_failures(A) == 2
    assert store.consecutive_failures(A, up_to_run_id=run2) == 1
    assert store.consecutive_failures(A, up_to_run_id=run1) == 0
    assert store.consecutive_failures(B) == 0


def test_event_round_trip_takes_currency_from_the_observation(store):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")
    obs = store.add_observation(run_id, ok(A, "109"))
    event = Event(
        url=A, product_name="Drill", type="price_down", old_price=Decimal("119"),
        new_price=Decimal("109"), change_pct=Decimal("-8.40"), verify=False, alerted=True,
        currency="EUR",
    )
    stock = Event(
        url=A, product_name="Drill", type="out_of_stock", old_price=Decimal("109"),
        new_price=Decimal("109"), change_pct=None, verify=False, alerted=False, currency="EUR",
    )
    store.add_event(run_id, obs.id, event)
    store.add_event(run_id, obs.id, stock)
    assert store.run_events(run_id) == [event, stock]
    assert store.run_events(run_id + 1) == []


def test_data_survives_reopening(tmp_path):
    path = tmp_path / "acme" / "pricemon.db"
    first = Store(path)
    run_id = first.start_run("2026-11-10T07:00:00+00:00")
    first.add_observation(run_id, ok(A, "119"))
    first.close()
    second = Store(path)
    assert second.last_ok(A).price == Decimal("119")
    second.close()


def test_dry_run_observations_can_be_excluded(store):
    real = store.start_run("2026-11-10T07:00:00+00:00")
    store.add_observation(real, ok(A, "119"))
    dry = store.start_run("2026-11-11T07:00:00+00:00", dry_run=True)
    store.add_observation(dry, ok(A, "109"))
    assert store.last_ok(A).price == Decimal("109")
    assert store.last_ok(A, include_dry_runs=False).price == Decimal("119")
    assert store.last_ok(A, before_run_id=dry, include_dry_runs=False).price == Decimal("119")

    dry_failure = store.start_run("2026-11-12T07:00:00+00:00", dry_run=True)
    store.add_observation(dry_failure, failed(A))
    assert store.consecutive_failures(A) == 1
    assert store.consecutive_failures(A, include_dry_runs=False) == 0
    assert store.last_ok(B, include_dry_runs=False) is None
