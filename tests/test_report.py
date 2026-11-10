import csv
from decimal import Decimal

import pytest
from openpyxl import load_workbook

from pricemon.config import ClientConfig
from pricemon.detect import detect
from pricemon.models import FETCH_ERROR, OK, Observation
from pricemon.report import CHANGES_HEADER, PRICES_HEADER, PROBLEMS_HEADER, write_report
from pricemon.store import Store

A = "https://www.a.example/p/drill"
B = "https://b.example/p/drill"

CONFIG = ClientConfig.model_validate(
    {"client": "acme", "products": [{"name": "Drill", "sku": "BOS-1", "own_price": "129.00", "urls": [A, B]}]}
)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "acme" / "pricemon.db")
    yield s
    s.close()


def ok(url, price, currency="EUR", in_stock=True):
    return Observation(
        url=url, product_name="Drill", fetched_at="2026-11-10T07:00:05+00:00", status=OK,
        http_status=200, title="Drill 12V", price=Decimal(price), currency=currency,
        in_stock=in_stock, method="jsonld",
    )


def failed(url):
    return Observation(
        url=url, product_name="Drill", fetched_at="2026-11-10T07:00:05+00:00",
        status=FETCH_ERROR, http_status=500, error="HTTP 500",
    )


def add(store, run_id, observation):
    """Store an observation and its events, the way the runner does."""
    previous = store.last_ok(observation.url)
    saved = store.add_observation(run_id, observation)
    if saved.status == OK:
        for event in detect(previous, saved, CONFIG.alerts):
            store.add_event(run_id, saved.id, event)


def two_runs(store):
    run1 = store.start_run("2026-11-09T07:00:00+00:00")
    add(store, run1, ok(A, "119.00"))
    add(store, run1, ok(B, "50.00", in_stock=False))
    run2 = store.start_run("2026-11-10T07:00:00+00:00")
    add(store, run2, ok(A, "109.00"))
    add(store, run2, failed(B))
    return run2


def sheet(path, name):
    """(header, list of row dicts) for one sheet."""
    rows = [list(row) for row in load_workbook(path)[name].iter_rows(values_only=True)]
    return rows[0], [dict(zip(rows[0], row)) for row in rows[1:]]


def test_workbook_has_three_sheets_with_headers(store, tmp_path):
    paths = write_report(CONFIG, store, two_runs(store), tmp_path / "reports")
    assert paths[0].suffix == ".xlsx" and paths[0].exists()
    assert load_workbook(paths[0]).sheetnames == ["Prices", "Changes", "Problems"]
    assert sheet(paths[0], "Prices")[0] == PRICES_HEADER
    assert sheet(paths[0], "Changes")[0] == CHANGES_HEADER
    assert sheet(paths[0], "Problems")[0] == PROBLEMS_HEADER


def test_prices_sheet(store, tmp_path):
    paths = write_report(CONFIG, store, two_runs(store), tmp_path / "reports")
    _, rows = sheet(paths[0], "Prices")
    by_url = {row["URL"]: row for row in rows}
    assert len(rows) == 2

    a = by_url[A]
    assert (a["Product"], a["SKU"], a["Own price"], a["Competitor"]) == ("Drill", "BOS-1", 129, "a.example")
    assert (a["Title"], a["Price"], a["Currency"], a["In stock"]) == ("Drill 12V", 109, "EUR", "yes")
    assert a["Previous price"] == 119
    assert a["Change %"] == pytest.approx(-8.4)
    assert a["Status"] == "ok"
    assert a["Last checked"] is not None

    b = by_url[B]  # failed this run: shows the last known values
    assert (b["Price"], b["Currency"], b["In stock"]) == (50, "EUR", "no")
    assert (b["Previous price"], b["Change %"]) == (None, None)
    assert b["Status"] == "fetch_error"


def test_changes_and_problems_sheets(store, tmp_path):
    paths = write_report(CONFIG, store, two_runs(store), tmp_path / "reports")
    _, changes = sheet(paths[0], "Changes")
    assert len(changes) == 1
    change = changes[0]
    assert (change["Product"], change["Competitor"], change["Change"]) == ("Drill", "a.example", "price down")
    assert (change["Old price"], change["New price"], change["Verify"], change["URL"]) == (119, 109, None, A)
    assert change["Change %"] == pytest.approx(-8.4)

    _, problems = sheet(paths[0], "Problems")
    assert len(problems) == 1
    problem = problems[0]
    assert (problem["Product"], problem["Competitor"], problem["Status"]) == ("Drill", "b.example", "fetch_error")
    assert (problem["Reason"], problem["Failed runs in a row"], problem["URL"]) == ("HTTP 500", 1, B)


def test_large_change_is_marked_verify(store, tmp_path):
    run1 = store.start_run("2026-11-09T07:00:00+00:00")
    add(store, run1, ok(A, "100.00"))
    run2 = store.start_run("2026-11-10T07:00:00+00:00")
    add(store, run2, ok(A, "10.00"))
    _, changes = sheet(write_report(CONFIG, store, run2, tmp_path / "reports")[0], "Changes")
    assert changes[0]["Verify"] == "verify"


def test_currency_change_is_a_problem_not_a_price_change(store, tmp_path):
    run1 = store.start_run("2026-11-09T07:00:00+00:00")
    add(store, run1, ok(A, "100.00", currency="EUR"))
    run2 = store.start_run("2026-11-10T07:00:00+00:00")
    add(store, run2, ok(A, "110.00", currency="USD"))
    path = write_report(CONFIG, store, run2, tmp_path / "reports")[0]
    _, prices = sheet(path, "Prices")
    assert (prices[0]["Price"], prices[0]["Previous price"], prices[0]["Change %"]) == (110, None, None)
    _, changes = sheet(path, "Changes")
    assert changes == []
    _, problems = sheet(path, "Problems")
    assert (problems[0]["Status"], problems[0]["Reason"]) == (
        "currency_changed", "currency changed from EUR to USD",
    )


def test_url_that_never_worked(store, tmp_path):
    run1 = store.start_run("2026-11-10T07:00:00+00:00")
    add(store, run1, failed(B))
    _, prices = sheet(write_report(CONFIG, store, run1, tmp_path / "reports")[0], "Prices")
    row = prices[0]
    assert (row["Price"], row["In stock"], row["Last checked"], row["Status"]) == (None, "unknown", None, "fetch_error")


def test_csv_matches_the_prices_sheet(store, tmp_path):
    paths = write_report(CONFIG, store, two_runs(store), tmp_path / "reports")
    assert [p.suffix for p in paths] == [".xlsx", ".csv"]
    assert paths[0].stem == paths[1].stem
    with paths[1].open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == PRICES_HEADER
    assert len(rows) == 3
    a = dict(zip(rows[0], next(row for row in rows[1:] if row[-1] == A)))
    assert (a["Price"], a["Previous price"], a["Change %"]) == ("109.00", "119.00", "-8.40")
    b = dict(zip(rows[0], next(row for row in rows[1:] if row[-1] == B)))
    assert b["Previous price"] == ""


def test_csv_is_optional(store, tmp_path):
    config = CONFIG.model_copy(update={"report": CONFIG.report.model_copy(update={"formats": ["xlsx"]})})
    paths = write_report(config, store, two_runs(store), tmp_path / "reports")
    assert [p.suffix for p in paths] == [".xlsx"]


def test_same_day_report_is_overwritten(store, tmp_path):
    run2 = two_runs(store)
    first = write_report(CONFIG, store, run2, tmp_path / "reports")
    second = write_report(CONFIG, store, run2, tmp_path / "reports")
    assert first == second
    assert len(list((tmp_path / "reports").iterdir())) == 2


def test_report_of_a_real_run_ignores_dry_runs(store, tmp_path):
    run1 = store.start_run("2026-11-08T07:00:00+00:00")
    store.add_observation(run1, ok(A, "119.00"))
    dry = store.start_run("2026-11-09T07:00:00+00:00", dry_run=True)
    store.add_observation(dry, ok(A, "100.00"))
    store.add_observation(dry, failed(B))
    run3 = store.start_run("2026-11-10T07:00:00+00:00")
    store.add_observation(run3, ok(A, "109.00"))
    store.add_observation(run3, failed(B))

    path = write_report(CONFIG, store, run3, tmp_path / "reports")[0]
    _, prices = sheet(path, "Prices")
    a = next(row for row in prices if row["URL"] == A)
    assert a["Previous price"] == 119
    _, problems = sheet(path, "Problems")
    assert problems[0]["Failed runs in a row"] == 1

    dry_path = write_report(CONFIG, store, dry, tmp_path / "dry-reports")[0]
    _, dry_prices = sheet(dry_path, "Prices")
    dry_a = next(row for row in dry_prices if row["URL"] == A)
    assert dry_a["Previous price"] == 119
