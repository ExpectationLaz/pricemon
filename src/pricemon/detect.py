"""Change detection: compare a new observation with the previous good one."""

from decimal import Decimal

from .config import Alerts
from .models import Event, Observation

_TWO_PLACES = Decimal("0.01")


def detect(previous: Observation | None, new: Observation, alerts: Alerts) -> list[Event]:
    """Events for one URL. Pure: no I/O. The first observation is a baseline."""
    if previous is None:
        return []
    events: list[Event] = []

    comparable = previous.price is not None and new.price is not None and previous.currency == new.currency
    if comparable and new.price != previous.price:
        change = (new.price - previous.price) / previous.price * 100
        kind = "price_down" if new.price < previous.price else "price_up"
        verify = abs(change) >= alerts.verify_threshold_pct
        alerted = kind in alerts.events and not verify and abs(change) >= alerts.min_price_change_pct
        events.append(
            Event(
                url=new.url,
                product_name=new.product_name,
                type=kind,
                old_price=previous.price,
                new_price=new.price,
                change_pct=change.quantize(_TWO_PLACES),
                verify=verify,
                alerted=alerted,
                currency=new.currency,
            )
        )

    stock_kind = None
    if previous.in_stock is True and new.in_stock is False:
        stock_kind = "out_of_stock"
    elif previous.in_stock is False and new.in_stock is True:
        stock_kind = "back_in_stock"
    if stock_kind is not None:
        events.append(
            Event(
                url=new.url,
                product_name=new.product_name,
                type=stock_kind,
                old_price=previous.price,
                new_price=new.price,
                change_pct=None,
                verify=False,
                alerted=stock_kind in alerts.events,
                currency=new.currency,
            )
        )
    return events
