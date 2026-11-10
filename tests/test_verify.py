"""A suspicious price is held at the last trusted price until the operator checks it."""

from decimal import Decimal

import pytest
from helpers import ENV, Outbox, Site, make_fetcher, page
from openpyxl import load_workbook

from pricemon.config import ClientConfig, Settings
from pricemon.models import OK, VERIFY
from pricemon.notify import Notifier, operator_digest
from pricemon.runner import run_client
from pricemon.store import Store

A = "https://a.example/p/drill"

CONFIG = ClientConfig.model_validate(
    {
        "client": "acme",
        "products": [{"name": "Drill", "urls": [A]}],
        "notify": {"email": ["owner@acme.example"]},
    }
)
HELD = "price changed by -90.00% (129.00 to 12.90): held for verification"


class World:
    """One client, one page, real notifier with fake senders."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.site = Site({A: (200, page("129.00"))})
        self.outbox = Outbox()
        self.notifier = Notifier(ENV, Settings(), send_email=self.outbox.email, send_telegram=self.outbox.telegram)

    def run(self, price=None, **kwargs):
        if price is not None:
            self.site.pages[A] = (200, page(price))
        return run_client(
            CONFIG, site_rules={}, data_dir=self.tmp_path / "data", fetcher=make_fetcher(self.site),
            notifier=self.notifier, **kwargs,
        )

    def store(self):
        return Store(self.tmp_path / "data" / "acme" / "pricemon.db")

    def client_emails(self):
        return [mail for mail in self.outbox.emails if mail[0] == ["owner@acme.example"]]

    def operator_emails(self):
        return [mail for mail in self.outbox.emails if mail[0] == ["op@example.com"]]


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_a_suspicious_price_is_stored_as_held_and_nobody_tells_the_client(world):
    world.run()
    summary = world.run("12.90")
    assert (summary.urls_ok, summary.urls_failed) == (0, 1)
    store = world.store()
    (observation,) = store.run_observations(2)
    (event,) = store.run_events(2)
    store.close()
    assert observation.status == VERIFY
    assert observation.error == HELD
    assert (observation.price, observation.currency, observation.title) == (Decimal("12.90"), "EUR", "Drill")
    assert observation.in_stock is True and observation.method == "jsonld"
    assert (event.type, event.verify, event.alerted) == ("price_down", True, False)
    assert world.client_emails() == []
    (operator_mail,) = world.operator_emails()
    assert "VERIFY Drill at a.example: 129.00 to 12.90 EUR, down 90.0%" in operator_mail[2]
    assert "FAILING" not in operator_mail[2]
    assert summary.undelivered == 0


def test_the_next_run_compares_with_the_last_trusted_price_and_repeats_the_alert(world):
    world.run()
    world.run("12.90")
    third = world.run("12.90")
    assert [(e.old_price, e.new_price, e.verify, e.alerted) for e in third.events] == [
        (Decimal("129.00"), Decimal("12.90"), True, False)
    ]
    assert len(world.operator_emails()) == 2
    assert all("VERIFY" in mail[2] for mail in world.operator_emails())
    assert world.client_emails() == []


def test_a_further_move_is_held_too_and_the_client_never_sees_the_unverified_price(world):
    world.run()
    world.run("12.90")
    third = world.run("12.50")
    assert [(e.old_price, e.new_price, e.alerted) for e in third.events] == [
        (Decimal("129.00"), Decimal("12.50"), False)
    ]
    assert world.client_emails() == []
    assert not any("12.90 to 12.50" in mail[2] for mail in world.outbox.emails)


def test_a_return_to_a_normal_price_is_an_ordinary_alerted_change(world):
    world.run()
    world.run("12.90")
    third = world.run("125.00")
    assert [(e.type, e.old_price, e.new_price, e.verify, e.alerted) for e in third.events] == [
        ("price_down", Decimal("129.00"), Decimal("125.00"), False, True)
    ]
    assert third.urls_ok == 1
    (client_mail,) = world.client_emails()
    assert "129.00 to 125.00 EUR" in client_mail[2]


def test_the_stock_event_of_a_held_observation_is_held_too(world):
    world.run()
    world.site.pages[A] = (200, page("12.90", availability="OutOfStock"))
    summary = world.run()
    assert sorted((e.type, e.alerted) for e in summary.events) == [("out_of_stock", False), ("price_down", False)]
    assert world.client_emails() == []


def test_accepting_a_held_price_makes_it_the_baseline_and_tells_the_client(world):
    world.run()
    world.run("12.90")
    store = world.store()
    held = store.latest_observation(A)
    store.accept_observation(held.id, CONFIG.alerts.events)
    store.close()

    third = world.run("12.90")  # unchanged since the accepted price
    assert third.events == []
    (client_mail,) = world.client_emails()
    assert "129.00 to 12.90 EUR" in client_mail[2]
    assert "Includes 1 change(s) that could not be delivered earlier." in client_mail[2]

    fourth = world.run("12.00")  # compared against the accepted 12.90
    assert [(e.old_price, e.new_price) for e in fourth.events] == [(Decimal("12.90"), Decimal("12.00"))]


def test_a_held_page_shows_its_last_trusted_price_in_the_report(world):
    world.run()
    summary = world.run("12.90")
    workbook = load_workbook(summary.report_paths[0])
    (row,) = [r for r in workbook["Prices"].iter_rows(min_row=2, values_only=True)]
    assert row[5] == 129 and row[11] == "verify"
    (problem,) = [r for r in workbook["Problems"].iter_rows(min_row=2, values_only=True)]
    assert problem[2] == "verify" and problem[3] == HELD


def test_operator_digest_has_no_failing_line_for_a_held_observation(world):
    world.run()
    world.run("12.90")
    store = world.store()
    try:
        text = operator_digest(CONFIG, store, 2, store.run_events(2), 1)
    finally:
        store.close()
    assert text.splitlines() == [
        "acme: 1 item(s) need attention",
        "",
        "VERIFY Drill at a.example: 129.00 to 12.90 EUR, down 90.0%",
    ]


def test_a_dry_run_holds_a_suspicious_price_too_but_never_affects_real_runs(world):
    world.run()
    dry = world.run("12.90", dry_run=True)
    assert dry.urls_failed == 1
    real = world.run("125.00")
    assert [(e.old_price, e.new_price, e.alerted) for e in real.events] == [
        (Decimal("129.00"), Decimal("125.00"), True)
    ]


def test_ok_observations_are_unchanged_by_the_check(world):
    world.run()
    summary = world.run("119.00")
    store = world.store()
    (observation,) = store.run_observations(2)
    store.close()
    assert observation.status == OK and observation.error is None
    assert summary.urls_ok == 1
