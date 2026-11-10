from decimal import Decimal

import pytest

from helpers import ENV, Outbox

from pricemon.config import ClientConfig, Settings
from pricemon.models import FETCH_ERROR, OK, Event, Observation
from pricemon.notify.telegram import TelegramAttachmentError
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


def record(store, run_id, *events):
    """Store events the way the runner does: the notifier reads them back from the store."""
    for event in events:
        obs = store.add_observation(
            run_id,
            Observation(
                url=A, product_name="Drill", fetched_at="2026-11-10T07:00:05+00:00", status=OK,
                price=Decimal("119.00"), currency="EUR",
            ),
        )
        store.add_event(run_id, obs.id, event)


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
    record(store, run_id, price_event(), stock_event())
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
    record(store, run_id, price_event())
    cfg = config(email=["owner@acme.example"], telegram_chat_id="123")
    warnings = make_notifier(env, outbox).send(cfg, store, run_id, [price_event()], tmp_path / "r.xlsx")
    # the second warning is the operator alert about the failed delivery, which hits the same problem
    assert warnings == ["email skipped: SMTP settings missing in .env"] * 2
    assert outbox.emails == []
    assert len(outbox.telegrams) == 2  # the client digest and the operator's DELIVERY alert


def test_missing_bot_token_skips_telegram_with_a_warning(store, tmp_path):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")
    outbox = Outbox()
    env = {**ENV, "TELEGRAM_BOT_TOKEN": ""}
    record(store, run_id, price_event())
    cfg = config(email=["owner@acme.example"], telegram_chat_id="123")
    warnings = make_notifier(env, outbox).send(cfg, store, run_id, [price_event()], tmp_path / "r.xlsx")
    assert warnings == ["telegram skipped: TELEGRAM_BOT_TOKEN missing in .env"] * 2
    assert outbox.telegrams == []
    assert len(outbox.emails) == 2  # the client digest and the operator's DELIVERY alert


def test_a_failing_channel_becomes_a_warning_and_the_other_still_sends(store, tmp_path):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")
    outbox = Outbox()

    def broken_email(env, to, subject, body, attachment=None):
        raise OSError("connection refused")

    notifier = Notifier(ENV, Settings(), send_email=broken_email, send_telegram=outbox.telegram)
    record(store, run_id, price_event())
    cfg = config(email=["owner@acme.example"], telegram_chat_id="123")
    warnings = notifier.send(cfg, store, run_id, [price_event()], tmp_path / "r.xlsx")
    assert warnings == ["email failed: OSError: connection refused"] * 2
    assert len(outbox.telegrams) == 2  # the client digest and the operator's DELIVERY alert


def test_a_telegram_failure_never_puts_the_bot_token_in_a_warning(store, tmp_path):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")

    def broken_telegram(env, chat_id, text, attachment=None):
        raise RuntimeError("cannot open https://api.telegram.org/botTOKEN/sendMessage")

    notifier = Notifier(ENV, Settings(), send_email=Outbox().email, send_telegram=broken_telegram)
    record(store, run_id, price_event())
    warnings = notifier.send(config(telegram_chat_id="123"), store, run_id, [price_event()], tmp_path / "r.xlsx")
    assert warnings == ["telegram failed: RuntimeError: cannot open https://api.telegram.org/bot***/sendMessage"] * 2


def test_events_without_a_client_channel_give_a_warning(store, tmp_path):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")
    outbox = Outbox()
    record(store, run_id, price_event())
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


# delivery tracking and resend


def new_run(store, day):
    return store.start_run(f"2026-11-{day:02d}T07:00:00+00:00")


def failing_email(env, to, subject, body, attachment=None):
    if to == ["owner@acme.example"]:
        raise OSError("connection refused")


def test_client_digest_mentions_changes_that_could_not_be_delivered_earlier():
    text = client_digest(config(), [price_event(), stock_event()], earlier=1)
    assert text.splitlines() == [
        "acme-shop: 2 changes today", "", DOWN, OUT, "", "Includes 1 change(s) that could not be delivered earlier.",
    ]
    assert "Includes" not in client_digest(config(), [price_event()])


def test_a_failed_email_is_resent_in_the_next_run_and_then_not_again(store, tmp_path):
    cfg = config(email=["owner@acme.example"])
    broken = Notifier(ENV, Settings(), send_email=failing_email, send_telegram=Outbox().telegram)
    run2 = new_run(store, 2)
    record(store, run2, price_event())
    warnings = broken.send(cfg, store, run2, [price_event()], tmp_path / "r.xlsx")
    assert warnings == ["email failed: OSError: connection refused"]
    assert len(store.undelivered_events(run2)) == 1

    outbox = Outbox()
    run3 = new_run(store, 3)
    assert make_notifier(ENV, outbox).send(cfg, store, run3, [], tmp_path / "r.xlsx") == []
    (email,) = outbox.emails
    assert email[2].splitlines() == [
        "acme-shop: 1 change today", "", DOWN, "", "Includes 1 change(s) that could not be delivered earlier.",
    ]
    assert store.undelivered_events(run3) == []

    run4 = new_run(store, 4)
    make_notifier(ENV, outbox).send(cfg, store, run4, [], tmp_path / "r.xlsx")
    assert len(outbox.emails) == 1


def test_this_runs_changes_are_not_called_earlier_ones(store):
    run_id = new_run(store, 2)
    record(store, run_id, price_event())
    outbox = Outbox()
    make_notifier(ENV, outbox).send(config(email=["owner@acme.example"]), store, run_id, [price_event()], None)
    assert "Includes" not in outbox.emails[0][2]


def test_one_working_channel_delivers_and_the_operator_hears_about_the_other(store, tmp_path):
    run_id = new_run(store, 2)
    record(store, run_id, price_event())
    outbox = Outbox()
    notifier = Notifier(ENV, Settings(), send_email=failing_email, send_telegram=outbox.telegram)
    cfg = config(email=["owner@acme.example"], telegram_chat_id="123")
    warnings = notifier.send(cfg, store, run_id, [price_event()], tmp_path / "r.xlsx")
    assert warnings == ["email failed: OSError: connection refused"]
    assert store.undelivered_events(run_id) == []
    client_message, operator_message = outbox.telegrams
    assert client_message[0] == "123"
    assert operator_message[0] == "999"
    assert operator_message[1].splitlines() == [
        "acme-shop: 1 item(s) need attention", "", "DELIVERY email failed: OSError: connection refused",
    ]


def test_when_every_client_channel_fails_the_events_stay_undelivered(store):
    run_id = new_run(store, 2)
    record(store, run_id, price_event(), stock_event())
    outbox = Outbox()

    def client_email_fails(env, to, subject, body, attachment=None):
        if to == ["owner@acme.example"]:
            raise OSError("mailbox full")
        outbox.email(env, to, subject, body, attachment)

    def client_telegram_fails(env, chat_id, text, attachment=None):
        if chat_id == "123":
            raise RuntimeError("chat not found")
        outbox.telegram(env, chat_id, text, attachment)

    notifier = Notifier(ENV, Settings(), send_email=client_email_fails, send_telegram=client_telegram_fails)
    cfg = config(email=["owner@acme.example"], telegram_chat_id="123")
    warnings = notifier.send(cfg, store, run_id, [price_event(), stock_event()], None)
    assert warnings == ["email failed: OSError: mailbox full", "telegram failed: RuntimeError: chat not found"]
    assert len(store.undelivered_events(run_id)) == 2
    (operator_email,) = outbox.emails
    assert operator_email[0] == ["op@example.com"]
    assert operator_email[2].splitlines() == [
        "acme-shop: 2 item(s) need attention",
        "",
        "DELIVERY email failed: OSError: mailbox full",
        "DELIVERY telegram failed: RuntimeError: chat not found",
    ]


def test_skipped_channels_are_delivery_problems_too(store):
    run_id = new_run(store, 2)
    record(store, run_id, price_event())
    outbox = Outbox()
    env = {**ENV, "TELEGRAM_BOT_TOKEN": ""}
    make_notifier(env, outbox).send(config(telegram_chat_id="123"), store, run_id, [price_event()], None)
    assert len(store.undelivered_events(run_id)) == 1
    assert "DELIVERY telegram skipped: TELEGRAM_BOT_TOKEN missing in .env" in outbox.emails[0][2]


def test_delivery_lines_come_before_failing_lines(store):
    run_id = failing_run(store, 1)
    text = operator_digest(config(), store, run_id, [], 1, delivery_problems=["email failed: OSError: x"])
    assert text.splitlines() == [
        "acme-shop: 2 item(s) need attention",
        "",
        "DELIVERY email failed: OSError: x",
        f"FAILING {A}: fetch_error (HTTP 500), 1 failed run(s) in a row",
    ]


def test_a_client_without_channels_has_nothing_to_resend(store):
    run_id = new_run(store, 2)
    record(store, run_id, price_event())
    outbox = Outbox()
    warnings = make_notifier(ENV, outbox).send(config(), store, run_id, [price_event()], None)
    assert warnings == ["client digest not sent: no email or telegram_chat_id in the client config"]
    assert store.undelivered_events(run_id) == []
    assert outbox.emails == [] and outbox.telegrams == []  # and the operator is not nagged about it


def test_dry_run_events_are_never_resent(store):
    dry = store.start_run("2026-11-02T07:00:00+00:00", dry_run=True)
    record(store, dry, price_event())
    run_id = new_run(store, 3)
    outbox = Outbox()
    make_notifier(ENV, outbox).send(config(email=["owner@acme.example"]), store, run_id, [], None)
    assert outbox.emails == []


def test_refused_recipients_are_a_warning_but_the_email_counts_as_delivered(store):
    run_id = new_run(store, 2)
    record(store, run_id, price_event())
    outbox = Outbox()

    def refusing(env, to, subject, body, attachment=None):
        if len(to) == 3:
            return {"b@x.example": (550, b"no"), "a@x.example": (550, b"no")}
        return None

    notifier = Notifier(ENV, Settings(), send_email=refusing, send_telegram=outbox.telegram)
    cfg = config(email=["a@x.example", "b@x.example", "c@x.example"])
    warnings = notifier.send(cfg, store, run_id, [price_event()], None)
    assert warnings == ["email refused for: a@x.example, b@x.example"]
    assert store.undelivered_events(run_id) == []
    assert "DELIVERY email refused for: a@x.example, b@x.example" in outbox.telegrams[0][1]


# test messages


def test_test_tries_every_channel_in_order():
    outbox = Outbox()
    cfg = config(email=["owner@acme.example"], telegram_chat_id="123")
    results = make_notifier(ENV, outbox).test(cfg)
    assert results == [
        ("client email (owner@acme.example)", True, "sent"),
        ("client telegram (123)", True, "sent"),
        ("operator email (op@example.com)", True, "sent"),
        ("operator telegram (999)", True, "sent"),
    ]
    subject = "Price monitor test: acme-shop"
    body = "This is a test message from the price monitor for acme-shop. No action is needed."
    assert [(e[1], e[2], e[3]) for e in outbox.emails] == [(subject, body, None)] * 2
    assert [(t[1], t[2]) for t in outbox.telegrams] == [(body, None)] * 2


def test_test_reports_failures_and_unconfigured_channels():
    outbox = Outbox()
    env = {key: value for key, value in ENV.items() if key != "OPERATOR_EMAIL"}
    env["TELEGRAM_BOT_TOKEN"] = ""
    notifier = Notifier(env, Settings(), send_email=failing_email, send_telegram=outbox.telegram)
    results = notifier.test(config(email=["owner@acme.example"], telegram_chat_id="123"))
    assert results == [
        ("client email (owner@acme.example)", False, "email failed: OSError: connection refused"),
        ("client telegram (123)", False, "telegram skipped: TELEGRAM_BOT_TOKEN missing in .env"),
        ("operator email", None, "not configured"),
        ("operator telegram (999)", False, "telegram skipped: TELEGRAM_BOT_TOKEN missing in .env"),
    ]


def test_test_flags_a_refused_recipient():
    def refusing(env, to, subject, body, attachment=None):
        return {"b@x.example": (550, b"no")}

    notifier = Notifier(ENV, Settings(), send_email=refusing, send_telegram=Outbox().telegram)
    first = notifier.test(config(email=["a@x.example", "b@x.example"]))[0]
    assert first == ("client email (a@x.example, b@x.example)", False, "email refused for: b@x.example")


# pages that could not be checked


def test_client_digest_says_how_many_pages_could_not_be_checked():
    text = client_digest(config(), [price_event()], earlier=1, unchecked=(2, 5))
    assert text.splitlines() == [
        "acme-shop: 1 change today", "", DOWN, "",
        "Includes 1 change(s) that could not be delivered earlier.", "",
        "2 of 5 pages could not be checked today.",
    ]
    quiet = config(send_when_no_changes=True)
    assert client_digest(quiet, [], unchecked=(1, 3)).splitlines() == [
        "acme-shop: no changes today", "", "1 of 3 pages could not be checked today.",
    ]
    assert client_digest(quiet, []) == "acme-shop: no changes today"
    assert client_digest(config(), [], unchecked=(1, 3)) is None  # the rule for sending is unchanged


def test_the_client_hears_when_every_page_failed_on_a_no_changes_day(store, tmp_path):
    run_id = failing_run(store, 1)
    store.add_observation(
        run_id,
        Observation(
            url="https://www.competitor-b.example/p/1", product_name="Drill", fetched_at="2026-11-01T07:00:06+00:00",
            status=FETCH_ERROR, error="HTTP 500",
        ),
    )
    outbox = Outbox()
    cfg = config(email=["owner@acme.example"], send_when_no_changes=True)
    make_notifier(ENV, outbox).send(cfg, store, run_id, [], tmp_path / "r.xlsx")
    client_mail = outbox.emails[0]
    assert client_mail[0] == ["owner@acme.example"]
    assert client_mail[2].splitlines() == [
        "acme-shop: no changes today", "", "2 of 2 pages could not be checked today.",
    ]


def test_a_digest_with_changes_also_reports_unchecked_pages(store, tmp_path):
    run_id = new_run(store, 2)
    record(store, run_id, price_event())
    store.add_observation(
        run_id,
        Observation(
            url="https://www.competitor-b.example/p/1", product_name="Drill", fetched_at="2026-11-02T07:00:06+00:00",
            status="verify", error="held",
        ),
    )
    outbox = Outbox()
    make_notifier(ENV, outbox).send(config(email=["owner@acme.example"]), store, run_id, [price_event()], None)
    assert outbox.emails[0][2].splitlines() == [
        "acme-shop: 1 change today", "", DOWN, "", "1 of 2 pages could not be checked today.",
    ]


def test_nothing_is_sent_when_pages_failed_but_nothing_changed(store, tmp_path):
    run_id = failing_run(store, 1)
    outbox = Outbox()
    make_notifier(ENV, outbox).send(config(email=["owner@acme.example"]), store, run_id, [], None)
    assert outbox.emails == []


def test_a_telegram_text_that_arrived_without_its_report_counts_as_delivered(store, tmp_path):
    run_id = new_run(store, 2)
    record(store, run_id, price_event())
    outbox = Outbox()

    def no_attachment(env, chat_id, text, attachment=None):
        if chat_id == "123":
            raise TelegramAttachmentError("sendDocument returned HTTP 400")
        outbox.telegram(env, chat_id, text, attachment)

    notifier = Notifier(ENV, Settings(), send_email=outbox.email, send_telegram=no_attachment)
    warnings = notifier.send(config(telegram_chat_id="123"), store, run_id, [price_event()], tmp_path / "r.xlsx")
    assert warnings == ["telegram report not attached: sendDocument returned HTTP 400"]
    assert store.undelivered_events(run_id) == []
    (operator_email,) = outbox.emails
    assert "DELIVERY telegram report not attached: sendDocument returned HTTP 400" in operator_email[2]
