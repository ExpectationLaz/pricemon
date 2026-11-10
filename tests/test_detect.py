from decimal import Decimal

from pricemon.config import Alerts
from pricemon.detect import detect
from pricemon.models import OK, Observation

URL = "https://a.example/p/1"
ALERTS = Alerts()


def obs(price, in_stock=True, currency="EUR"):
    return Observation(
        url=URL, product_name="Drill", fetched_at="2026-11-10T07:00:00+00:00", status=OK,
        price=Decimal(price), currency=currency, in_stock=in_stock,
    )


def test_first_observation_is_a_baseline():
    assert detect(None, obs("119"), ALERTS) == []


def test_no_change_gives_no_events():
    assert detect(obs("119.00"), obs("119"), ALERTS) == []


def test_price_down():
    (event,) = detect(obs("129.00"), obs("119.00"), ALERTS)
    assert event.type == "price_down"
    assert (event.url, event.product_name, event.currency) == (URL, "Drill", "EUR")
    assert (event.old_price, event.new_price) == (Decimal("129.00"), Decimal("119.00"))
    assert event.change_pct == Decimal("-7.75")
    assert (event.verify, event.alerted) == (False, True)


def test_price_up():
    (event,) = detect(obs("100"), obs("110"), ALERTS)
    assert (event.type, event.change_pct, event.alerted) == ("price_up", Decimal("10.00"), True)


def test_small_change_is_recorded_but_not_alerted():
    (event,) = detect(obs("100.00"), obs("100.50"), ALERTS)
    assert (event.type, event.change_pct, event.verify, event.alerted) == ("price_up", Decimal("0.50"), False, False)


def test_change_at_the_minimum_is_alerted():
    (event,) = detect(obs("100"), obs("101"), ALERTS)
    assert event.alerted is True


def test_large_change_is_marked_verify_and_not_alerted():
    (event,) = detect(obs("100"), obs("30"), ALERTS)
    assert (event.type, event.change_pct, event.verify, event.alerted) == ("price_down", Decimal("-70.00"), True, False)


def test_event_type_not_selected_is_not_alerted():
    alerts = Alerts(events=["price_down"])
    (event,) = detect(obs("100"), obs("110"), alerts)
    assert (event.type, event.alerted) == ("price_up", False)


def test_currency_change_gives_no_price_event():
    assert detect(obs("100", currency="EUR"), obs("110", currency="USD"), ALERTS) == []


def test_two_missing_currencies_count_as_the_same():
    (event,) = detect(obs("100", currency=None), obs("110", currency=None), ALERTS)
    assert (event.type, event.currency) == ("price_up", None)


def test_out_of_stock_and_back_in_stock():
    (out,) = detect(obs("100", in_stock=True), obs("100", in_stock=False), ALERTS)
    assert (out.type, out.change_pct, out.verify, out.alerted) == ("out_of_stock", None, False, True)
    assert (out.old_price, out.new_price) == (Decimal("100"), Decimal("100"))
    (back,) = detect(obs("100", in_stock=False), obs("100", in_stock=True), ALERTS)
    assert back.type == "back_in_stock"


def test_unknown_stock_gives_no_stock_event():
    assert detect(obs("100", in_stock=None), obs("100", in_stock=False), ALERTS) == []
    assert detect(obs("100", in_stock=True), obs("100", in_stock=None), ALERTS) == []


def test_price_and_stock_can_change_together():
    events = detect(obs("100", in_stock=True), obs("90", in_stock=False), ALERTS)
    assert [e.type for e in events] == ["price_down", "out_of_stock"]


def test_stock_event_not_selected_is_not_alerted():
    alerts = Alerts(events=["price_down", "price_up"])
    (event,) = detect(obs("100", in_stock=True), obs("100", in_stock=False), alerts)
    assert event.alerted is False
