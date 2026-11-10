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


# delivery tracking


def alerted_event(url=A, kind="price_down", alerted=True):
    return Event(
        url=url, product_name="Drill", type=kind, old_price=Decimal("119"), new_price=Decimal("109"),
        change_pct=Decimal("-8.40"), verify=False, alerted=alerted, currency="EUR",
    )


def add_event_in_new_run(store, event, dry_run=False, day=10):
    run_id = store.start_run(f"2026-11-{day:02d}T07:00:00+00:00", dry_run=dry_run)
    obs = store.add_observation(run_id, ok(event.url, "109"))
    store.add_event(run_id, obs.id, event)
    return run_id


def test_events_read_back_carry_their_id_and_run_id(store):
    run_id = add_event_in_new_run(store, alerted_event())
    (event,) = store.run_events(run_id)
    assert event.id is not None and event.run_id == run_id
    assert event == alerted_event()  # id and run_id do not take part in equality


def test_undelivered_events_are_alerted_real_and_oldest_first(store):
    first = add_event_in_new_run(store, alerted_event(A), day=10)
    add_event_in_new_run(store, alerted_event(B, alerted=False), day=11)  # never alerted
    add_event_in_new_run(store, alerted_event(B), dry_run=True, day=12)  # a dry run
    third = add_event_in_new_run(store, alerted_event(B, kind="out_of_stock"), day=13)
    assert [(e.url, e.type, e.run_id) for e in store.undelivered_events(third)] == [
        (A, "price_down", first),
        (B, "out_of_stock", third),
    ]
    assert [e.run_id for e in store.undelivered_events(first)] == [first]
    assert store.undelivered_events(first)[0].currency == "EUR"


def test_mark_delivered_marks_exactly_what_undelivered_events_returned(store):
    first = add_event_in_new_run(store, alerted_event(A), day=10)
    second = add_event_in_new_run(store, alerted_event(B), day=11)
    store.mark_delivered(first)
    assert [e.run_id for e in store.undelivered_events(second)] == [second]
    store.mark_delivered(second)
    assert store.undelivered_events(second) == []


def test_mark_delivered_leaves_dry_run_events_alone(store):
    dry = add_event_in_new_run(store, alerted_event(), dry_run=True)
    store.mark_delivered(dry)
    flags = [row["delivered"] for row in store.db.execute("SELECT delivered FROM events")]
    assert flags == [0]


def test_a_database_from_before_delivery_tracking_is_upgraded(tmp_path):
    import sqlite3

    path = tmp_path / "old" / "pricemon.db"
    path.parent.mkdir()
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE runs (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
            urls_total INTEGER NOT NULL DEFAULT 0, urls_ok INTEGER NOT NULL DEFAULT 0,
            urls_failed INTEGER NOT NULL DEFAULT 0, dry_run INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE observations (id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, url TEXT NOT NULL,
            product_name TEXT NOT NULL, fetched_at TEXT NOT NULL, status TEXT NOT NULL, http_status INTEGER,
            title TEXT, price TEXT, currency TEXT, in_stock INTEGER, method TEXT, error TEXT);
        CREATE TABLE events (id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, observation_id INTEGER NOT NULL,
            url TEXT NOT NULL, product_name TEXT NOT NULL, type TEXT NOT NULL, old_price TEXT, new_price TEXT,
            change_pct TEXT, verify INTEGER NOT NULL DEFAULT 0, alerted INTEGER NOT NULL DEFAULT 0);
        INSERT INTO runs (id, started_at) VALUES (1, '2026-11-10T07:00:00+00:00');
        INSERT INTO observations (id, run_id, url, product_name, fetched_at, status, price, currency)
            VALUES (1, 1, 'https://a.example/p/1', 'Drill', '2026-11-10T07:00:00+00:00', 'ok', '109', 'EUR');
        INSERT INTO events (run_id, observation_id, url, product_name, type, old_price, new_price, change_pct, alerted)
            VALUES (1, 1, 'https://a.example/p/1', 'Drill', 'price_down', '119', '109', '-8.40', 1);
        """
    )
    old.commit()
    old.close()
    upgraded = Store(path)
    try:
        assert [e.type for e in upgraded.undelivered_events(1)] == ["price_down"]
        upgraded.mark_delivered(1)
        assert upgraded.undelivered_events(1) == []
    finally:
        upgraded.close()
    Store(path).close()  # opening an upgraded database again does not fail
