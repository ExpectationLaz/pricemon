"""Digest text and dispatch to the configured channels.

One digest per run per channel, never one message per event.
"""

from pathlib import Path
from typing import Mapping

from ..config import ClientConfig, Settings, hostname
from ..models import OK, Event
from ..store import Store
from .email import send_email
from .telegram import send_telegram

_SMTP_KEYS = ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM")
_STOCK_TEXT = {"out_of_stock": "out of stock", "back_in_stock": "back in stock"}


def event_line(event: Event) -> str:
    """One line of digest text, e.g. 'Drill at shop.example: 129.00 to 119.00 EUR, down 7.8%'."""
    where = f"{event.product_name} at {hostname(event.url)}"
    if event.type in _STOCK_TEXT:
        return f"{where}: {_STOCK_TEXT[event.type]}"
    direction = "down" if event.type == "price_down" else "up"
    currency = f" {event.currency}" if event.currency else ""
    return (
        f"{where}: {event.old_price:.2f} to {event.new_price:.2f}{currency}, "
        f"{direction} {abs(event.change_pct):.1f}%"
    )


def client_digest(config: ClientConfig, events: list[Event]) -> str | None:
    """The message for the client, or None when nothing should be sent."""
    alerted = [event for event in events if event.alerted]
    if not alerted:
        return f"{config.client}: no changes today" if config.notify.send_when_no_changes else None
    noun = "change" if len(alerted) == 1 else "changes"
    lines = [f"{config.client}: {len(alerted)} {noun} today", ""]
    lines.extend(event_line(event) for event in alerted)
    return "\n".join(lines)


def operator_digest(
    config: ClientConfig, store: Store, run_id: int, events: list[Event], threshold: int
) -> str | None:
    """The message for the operator, or None when nothing needs attention.

    A failing URL is reported in the run where its failure streak reaches the
    threshold, and not again until it has recovered and failed again.
    """
    # A real run ignores failures seen in dry runs; a dry run counts everything.
    include_dry_runs = bool(store.run(run_id)["dry_run"])
    lines = []
    for obs in store.run_observations(run_id):
        if obs.status == OK:
            continue
        failures = store.consecutive_failures(obs.url, up_to_run_id=run_id, include_dry_runs=include_dry_runs)
        if failures == threshold:
            lines.append(
                f"FAILING {obs.url}: {obs.status} ({obs.error or 'no detail'}), "
                f"{threshold} failed run(s) in a row"
            )
    lines.extend(f"VERIFY {event_line(event)}" for event in events if event.verify)
    if not lines:
        return None
    return "\n".join([f"{config.client}: {len(lines)} item(s) need attention", "", *lines])


class Notifier:
    def __init__(self, env: Mapping[str, str], settings: Settings, send_email=send_email, send_telegram=send_telegram):
        self.env = env
        self.threshold = settings.operator_failure_threshold
        self._send_email = send_email
        self._send_telegram = send_telegram

    def send(
        self, config: ClientConfig, store: Store, run_id: int, events: list[Event], report_path: Path | None
    ) -> list[str]:
        """Send the client and operator digests for a run. Returns warnings; never raises for a channel failure."""
        warnings: list[str] = []

        body = client_digest(config, events)
        if body is not None:
            subject = f"Price monitor: {config.client}"
            if config.notify.email:
                self._email(config.notify.email, subject, body, report_path, warnings)
            if config.notify.telegram_chat_id:
                self._telegram(config.notify.telegram_chat_id, body, report_path, warnings)
            if not config.notify.email and not config.notify.telegram_chat_id:
                warnings.append("client digest not sent: no email or telegram_chat_id in the client config")

        alert = operator_digest(config, store, run_id, events, self.threshold)
        if alert is not None:
            subject = f"Price monitor operator alert: {config.client}"
            operator_email = self.env.get("OPERATOR_EMAIL")
            operator_chat = self.env.get("OPERATOR_TELEGRAM_CHAT_ID")
            if operator_email:
                self._email([operator_email], subject, alert, None, warnings)
            if operator_chat:
                self._telegram(operator_chat, alert, None, warnings)
            if not operator_email and not operator_chat:
                warnings.append("operator alert not sent: set OPERATOR_EMAIL or OPERATOR_TELEGRAM_CHAT_ID in .env")
        return warnings

    def _email(self, to: list[str], subject: str, body: str, attachment: Path | None, warnings: list[str]) -> None:
        if any(not self.env.get(key) for key in _SMTP_KEYS):
            warnings.append("email skipped: SMTP settings missing in .env")
            return
        try:
            self._send_email(self.env, to, subject, body, attachment)
        except Exception as exc:
            warnings.append(f"email failed: {type(exc).__name__}: {exc}")

    def _telegram(self, chat_id: str, text: str, attachment: Path | None, warnings: list[str]) -> None:
        if not self.env.get("TELEGRAM_BOT_TOKEN"):
            warnings.append("telegram skipped: TELEGRAM_BOT_TOKEN missing in .env")
            return
        try:
            self._send_telegram(self.env, chat_id, text, attachment)
        except Exception as exc:
            warnings.append(f"telegram failed: {type(exc).__name__}: {exc}")
