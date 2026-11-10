from decimal import Decimal

import pytest

from helpers import ENV, Outbox

from pricemon.config import ClientConfig, Settings
from pricemon.models import FETCH_ERROR, Event, Observation
from pricemon.notify import Notifier, client_digest, event_line, operator_digest
from pricemon.store import Store

A = "https://www.competitor-a.example/p/1"
DOWN = "Drill at competitor-a.example: 129.00 to 119.00 EUR, down 7.8%"
OUT = "Drill at competitor-a.example: out of stock"


def config(**notify):
    return ClientConfig.model_validate(
        {"client": "acme-shop", "products": [{"name": "Drill", "urls": [A]}], "notify": notify}
    )


def price_event(kind="price_down", old="129.00", new="119.00", pct="-7.75", verify=False, alerted=True, currency="EUR"):
    return Event(
        url=A, product_name="Drill", type=kind, old_price=Decimal(old), new_price=Decimal(new),
        change_pct=Decimal(pct), verify=verify, alerted=alerted, currency=currency,
    )


def stock_event(kind="out_of_stock", alerted=True):
    return Event(
        url=A, product_name="Drill", type=kind, old_price=Decimal("119.00"), new_price=Decimal("119.00"),
        change_pct=None, verify=False, alerted=alerted, currency="EUR",
    )


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "acme-shop" / "pricemon.db")
    yield s
    s.close()


def failing_run(store, day):
    run_id = store.start_run(f"2026-11-{day:02d}T07:00:00+00:00")
    store.add_observation(
        run_id,
        Observation(
            url=A, product_name="Drill", fetched_at=f"2026-11-{day:02d}T07:00:05+00:00",
            status=FETCH_ERROR, http_status=500, error="HTTP 500",
        ),
    )
    return run_id


def make_notifier(env, outbox, threshold=3):
    settings = Settings(operator_failure_threshold=threshold)
    return Notifier(env, settings, send_email=outbox.email, send_telegram=outbox.telegram)


# digest text


def test_event_lines():
    assert event_line(price_event()) == DOWN
    up = price_event("price_up", old="100", new="110.5", pct="10.50", currency=None)
    assert event_line(up) == "Drill at competitor-a.example: 100.00 to 110.50, up 10.5%"
    assert event_line(stock_event()) == OUT
    assert event_line(stock_event("back_in_stock")) == "Drill at competitor-a.example: back in stock"


def test_client_digest_lists_only_alerted_events():
    events = [price_event(), stock_event(), price_event("price_up", alerted=False)]
    assert client_digest(config(), events).splitlines() == ["acme-shop: 2 changes today", "", DOWN, OUT]


def test_client_digest_singular():
    assert client_digest(config(), [price_event()]).splitlines()[0] == "acme-shop: 1 change today"


def test_client_digest_without_alerted_events():
    quiet = [price_event(alerted=False)]
    assert client_digest(config(), quiet) is None
    assert client_digest(config(send_when_no_changes=True), quiet) == "acme-shop: no changes today"


def test_operator_digest_fires_when_the_threshold_is_reached(store):
    cfg = config()
    first, second, third = failing_run(store, 1), failing_run(store, 2), failing_run(store, 3)
    assert operator_digest(cfg, store, first, [], 3) is None
    assert operator_digest(cfg, store, second, [], 3) is None
    assert operator_digest(cfg, store, third, [], 3).splitlines() == [
        "acme-shop: 1 item(s) need attention",
        "",
        f"FAILING {A}: fetch_error (HTTP 500), 3 failed run(s) in a row",
    ]
    fourth = failing_run(store, 4)
    assert operator_digest(cfg, store, fourth, [], 3) is None


def test_operator_digest_repeats_while_the_page_keeps_failing(store):
    cfg = config()
    runs = [failing_run(store, day) for day in range(1, 8)]
    alerted = [day for day, run_id in enumerate(runs, start=1) if operator_digest(cfg, store, run_id, [], 3)]
    assert alerted == [3, 6]
    assert f"FAILING {A}: fetch_error (HTTP 500), 6 failed run(s) in a row" in operator_digest(
        cfg, store, runs[5], [], 3
    )


def test_operator_digest_ignores_failures_seen_in_dry_runs(store):
    cfg = config()
    failing_run(store, 1)
    failing_run(store, 2)
    dry = store.start_run("2026-11-03T07:00:00+00:00", dry_run=True)
    store.add_observation(
        dry,
        Observation(
            url=A, product_name="Drill", fetched_at="2026-11-03T07:00:05+00:00",
            status=FETCH_ERROR, http_status=500, error="HTTP 500",
        ),
    )
    third_real_failure = failing_run(store, 4)
    text = operator_digest(cfg, store, third_real_failure, [], 3)
    assert text is not None
    assert f"FAILING {A}: fetch_error (HTTP 500), 3 failed run(s) in a row" in text


def test_operator_digest_includes_verify_events(store):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")
    suspicious = price_event(new="12.90", pct="-90.00", verify=True, alerted=False)
    text = operator_digest(config(), store, run_id, [suspicious, price_event()], 3)
    assert text.splitlines() == [
        "acme-shop: 1 item(s) need attention",
        "",
        "VERIFY Drill at competitor-a.example: 129.00 to 12.90 EUR, down 90.0%",
    ]


# sending


def test_client_gets_one_digest_per_channel_with_the_report(store, tmp_path):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")
    report = tmp_path / "2026-11-10.xlsx"
    outbox = Outbox()
    cfg = config(email=["owner@acme.example"], telegram_chat_id="123")
    warnings = make_notifier(ENV, outbox).send(cfg, store, run_id, [price_event(), stock_event()], report)
    assert warnings == []
    assert outbox.emails == [
        (["owner@acme.example"], "Price monitor: acme-shop", f"acme-shop: 2 changes today\n\n{DOWN}\n{OUT}", report)
    ]
    assert outbox.telegrams == [("123", f"acme-shop: 2 changes today\n\n{DOWN}\n{OUT}", report)]


def test_nothing_is_sent_without_changes(store, tmp_path):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")
    outbox = Outbox()
    cfg = config(email=["owner@acme.example"])
    assert make_notifier(ENV, outbox).send(cfg, store, run_id, [], tmp_path / "r.xlsx") == []
    assert outbox.emails == [] and outbox.telegrams == []


def test_daily_report_is_sent_without_changes_when_asked(store, tmp_path):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")
    outbox = Outbox()
    cfg = config(email=["owner@acme.example"], send_when_no_changes=True)
    make_notifier(ENV, outbox).send(cfg, store, run_id, [], tmp_path / "r.xlsx")
    assert outbox.emails[0][2] == "acme-shop: no changes today"


def test_missing_smtp_settings_skip_email_with_a_warning(store, tmp_path):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")
    outbox = Outbox()
    env = {key: value for key, value in ENV.items() if key != "SMTP_PASSWORD"}
    cfg = config(email=["owner@acme.example"], telegram_chat_id="123")
    warnings = make_notifier(env, outbox).send(cfg, store, run_id, [price_event()], tmp_path / "r.xlsx")
    assert warnings == ["email skipped: SMTP settings missing in .env"]
    assert outbox.emails == []
    assert len(outbox.telegrams) == 1


def test_missing_bot_token_skips_telegram_with_a_warning(store, tmp_path):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")
    outbox = Outbox()
    env = {**ENV, "TELEGRAM_BOT_TOKEN": ""}
    cfg = config(email=["owner@acme.example"], telegram_chat_id="123")
    warnings = make_notifier(env, outbox).send(cfg, store, run_id, [price_event()], tmp_path / "r.xlsx")
    assert warnings == ["telegram skipped: TELEGRAM_BOT_TOKEN missing in .env"]
    assert outbox.telegrams == []
    assert len(outbox.emails) == 1


def test_a_failing_channel_becomes_a_warning_and_the_other_still_sends(store, tmp_path):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")
    outbox = Outbox()

    def broken_email(env, to, subject, body, attachment=None):
        raise OSError("connection refused")

    notifier = Notifier(ENV, Settings(), send_email=broken_email, send_telegram=outbox.telegram)
    cfg = config(email=["owner@acme.example"], telegram_chat_id="123")
    warnings = notifier.send(cfg, store, run_id, [price_event()], tmp_path / "r.xlsx")
    assert warnings == ["email failed: OSError: connection refused"]
    assert len(outbox.telegrams) == 1


def test_a_telegram_failure_never_puts_the_bot_token_in_a_warning(store, tmp_path):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")

    def broken_telegram(env, chat_id, text, attachment=None):
        raise RuntimeError("cannot open https://api.telegram.org/botTOKEN/sendMessage")

    notifier = Notifier(ENV, Settings(), send_email=Outbox().email, send_telegram=broken_telegram)
    warnings = notifier.send(config(telegram_chat_id="123"), store, run_id, [price_event()], tmp_path / "r.xlsx")
    assert warnings == ["telegram failed: RuntimeError: cannot open https://api.telegram.org/bot***/sendMessage"]


def test_events_without_a_client_channel_give_a_warning(store, tmp_path):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")
    outbox = Outbox()
    warnings = make_notifier(ENV, outbox).send(config(), store, run_id, [price_event()], tmp_path / "r.xlsx")
    assert warnings == ["client digest not sent: no email or telegram_chat_id in the client config"]


def test_operator_alert_goes_to_the_operator_without_the_report(store, tmp_path):
    run_id = failing_run(store, 1)
    outbox = Outbox()
    warnings = make_notifier(ENV, outbox, threshold=1).send(config(), store, run_id, [], tmp_path / "r.xlsx")
    assert warnings == []
    expected = f"acme-shop: 1 item(s) need attention\n\nFAILING {A}: fetch_error (HTTP 500), 1 failed run(s) in a row"
    assert outbox.emails == [(["op@example.com"], "Price monitor operator alert: acme-shop", expected, None)]
    assert outbox.telegrams == [("999", expected, None)]


def test_operator_alert_without_operator_contacts_gives_a_warning(store, tmp_path):
    run_id = failing_run(store, 1)
    outbox = Outbox()
    env = {key: value for key, value in ENV.items() if not key.startswith("OPERATOR_")}
    warnings = make_notifier(env, outbox, threshold=1).send(config(), store, run_id, [], tmp_path / "r.xlsx")
    assert warnings == ["operator alert not sent: set OPERATOR_EMAIL or OPERATOR_TELEGRAM_CHAT_ID in .env"]
    assert outbox.emails == [] and outbox.telegrams == []
