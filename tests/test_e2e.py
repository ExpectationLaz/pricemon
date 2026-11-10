"""Two full runs through the real pipeline, with a fake website and fake senders."""

from decimal import Decimal
from pathlib import Path

from helpers import ENV, Outbox, Site, make_fetcher, page
from openpyxl import load_workbook

from pricemon.config import ClientConfig, Settings
from pricemon.models import BLOCKED, OK
from pricemon.notify import Notifier
from pricemon.runner import run_client
from pricemon.store import Store

A = "https://a.example/p/drill"
B = "https://b.example/p/drill"

CONFIG = ClientConfig.model_validate(
    {
        "client": "acme",
        "products": [{"name": "Drill", "own_price": "129.00", "urls": [A, B]}],
        "notify": {"email": ["owner@acme.example"], "telegram_chat_id": "123"},
    }
)


def test_two_runs_end_to_end(tmp_path):
    site = Site({A: (200, page("119.00")), B: (200, page("125.00"))})
    outbox = Outbox()
    notifier = Notifier(
        ENV, Settings(operator_failure_threshold=1), send_email=outbox.email, send_telegram=outbox.telegram
    )

    def run():
        return run_client(
            CONFIG, site_rules={}, data_dir=tmp_path / "data", fetcher=make_fetcher(site), notifier=notifier
        )

    # Run 1: baseline. Nothing to report to anyone.
    first = run()
    assert (first.urls_ok, first.urls_failed, first.events, first.warnings) == (2, 0, [], [])
    assert outbox.emails == [] and outbox.telegrams == []

    # Run 2: A drops in price, B starts refusing requests.
    site.pages[A] = (200, page("109.00"))
    site.pages[B] = (403, "")
    second = run()
    assert (second.urls_ok, second.urls_failed, second.warnings) == (1, 1, [])

    # The client hears about the price drop, with the report attached.
    client_email, operator_email = outbox.emails
    assert client_email[0] == ["owner@acme.example"]
    assert client_email[2] == (
        "acme: 1 change today\n\nDrill at a.example: 119.00 to 109.00 EUR, down 8.4%"
        "\n\n1 of 2 pages could not be checked today."  # B is blocked
    )
    assert Path(client_email[3]).suffix == ".xlsx" and Path(client_email[3]).exists()

    # The operator hears about the blocked page, without the report.
    assert operator_email[0] == ["op@example.com"]
    assert f"FAILING {B}: blocked (HTTP 403), 1 failed run(s) in a row" in operator_email[2]
    assert operator_email[3] is None
    assert [chat for chat, _, _ in outbox.telegrams] == ["123", "999"]

    # The database holds both runs.
    store = Store(tmp_path / "data" / "acme" / "pricemon.db")
    statuses = {o.url: o.status for o in store.run_observations(2)}
    events = store.run_events(2)
    store.close()
    assert statuses == {A: OK, B: BLOCKED}
    assert [(e.type, e.old_price, e.new_price, e.alerted) for e in events] == [
        ("price_down", Decimal("119.00"), Decimal("109.00"), True)
    ]

    # The report shows the change, and the blocked page with its last known price.
    workbook = load_workbook(second.report_paths[0])
    assert workbook["Changes"].max_row == 2
    assert workbook["Problems"].max_row == 2
    prices = {row[-1]: row for row in workbook["Prices"].iter_rows(min_row=2, values_only=True)}
    assert prices[A][5] == 109 and prices[A][8] == 119
    assert prices[B][5] == 125 and prices[B][11] == "blocked"


def test_a_digest_that_could_not_be_sent_is_resent_once(tmp_path):
    """Run 2 drops in price while the mail server is down; run 3 resends; run 4 sends nothing."""
    site = Site({A: (200, page("119.00")), B: (200, page("125.00"))})
    outbox = Outbox()
    mail_up = {"value": False}

    def email(env, to, subject, body, attachment=None):
        if not mail_up["value"] and to == ["owner@acme.example"]:
            raise OSError("connection refused")
        outbox.email(env, to, subject, body, attachment)

    config = ClientConfig.model_validate(
        {
            "client": "acme",
            "products": [{"name": "Drill", "urls": [A, B]}],
            "notify": {"email": ["owner@acme.example"]},
        }
    )
    notifier = Notifier(ENV, Settings(), send_email=email, send_telegram=outbox.telegram)

    def run():
        return run_client(
            config, site_rules={}, data_dir=tmp_path / "data", fetcher=make_fetcher(site), notifier=notifier
        )

    run()
    site.pages[A] = (200, page("109.00"))
    second = run()
    assert second.undelivered == 1
    assert [e[0] for e in outbox.emails] == [["op@example.com"]]  # only the operator heard, via DELIVERY
    assert "DELIVERY email failed: OSError: connection refused" in outbox.emails[0][2]

    mail_up["value"] = True
    third = run()  # nothing new changed
    assert third.undelivered == 0
    client_email = outbox.emails[1]
    assert client_email[0] == ["owner@acme.example"]
    assert client_email[2].splitlines() == [
        "acme: 1 change today",
        "",
        "Drill at a.example: 119.00 to 109.00 EUR, down 8.4%",
        "",
        "Includes 1 change(s) that could not be delivered earlier.",
    ]

    fourth = run()
    assert fourth.undelivered == 0
    assert len(outbox.emails) == 2
