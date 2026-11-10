"""Digest text and dispatch to the configured channels.

One digest per run per channel, never one message per event.
"""

from pathlib import Path
from typing import Mapping, Sequence

from ..config import ClientConfig, Settings, hostname
from ..models import OK, VERIFY, Event
from ..store import Store
from .email import send_email
from .telegram import TelegramAttachmentError, send_telegram

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


def client_digest(
    config: ClientConfig, events: list[Event], earlier: int = 0, unchecked: tuple[int, int] | None = None
) -> str | None:
    """The message for the client, or None when nothing should be sent.

    `earlier` is how many of the listed events could not be delivered in an earlier run.
    `unchecked` is (pages not checked, pages total) for this run, or None when every page was checked.
    """
    alerted = [event for event in events if event.alerted]
    if not alerted:
        if not config.notify.send_when_no_changes:
            return None
        lines = [f"{config.client}: no changes today"]
    else:
        noun = "change" if len(alerted) == 1 else "changes"
        lines = [f"{config.client}: {len(alerted)} {noun} today", ""]
        lines.extend(event_line(event) for event in alerted)
        if earlier > 0:
            lines.extend(["", f"Includes {earlier} change(s) that could not be delivered earlier."])
    if unchecked is not None and unchecked[0] > 0:
        lines.extend(["", f"{unchecked[0]} of {unchecked[1]} pages could not be checked today."])
    return "\n".join(lines)


def operator_digest(
    config: ClientConfig,
    store: Store,
    run_id: int,
    events: list[Event],
    threshold: int,
    delivery_problems: Sequence[str] = (),
) -> str | None:
    """The message for the operator, or None when nothing needs attention.

    A failing URL is reported in the run where its failure streak reaches the
    threshold, and again every `threshold` runs while it keeps failing.
    """
    # A real run ignores failures seen in dry runs; a dry run counts everything.
    include_dry_runs = bool(store.run(run_id)["dry_run"])
    lines = [f"DELIVERY {problem}" for problem in delivery_problems]
    for obs in store.run_observations(run_id):
        if obs.status in (OK, VERIFY):  # a held price has its own VERIFY line
            continue
        failures = store.consecutive_failures(obs.url, up_to_run_id=run_id, include_dry_runs=include_dry_runs)
        # Alert when the streak reaches the threshold, and again every `threshold`
        # runs while the page keeps failing, so one lost alert is not the last word.
        if failures >= threshold and failures % threshold == 0:
            lines.append(
                f"FAILING {obs.url}: {obs.status} ({obs.error or 'no detail'}), "
                f"{failures} failed run(s) in a row"
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
        """Send the client and operator digests for a run. Returns warnings; never raises for a channel failure.

        The client digest holds every alerted event that has not reached the
        client yet, so a digest that failed to send is retried in the next run.
        """
        warnings: list[str] = []
        delivery_problems: list[str] = []

        pending = store.undelivered_events(run_id)
        earlier = sum(1 for event in pending if event.run_id != run_id)
        observations = store.run_observations(run_id)
        failed = sum(1 for obs in observations if obs.status != OK)
        body = client_digest(config, pending, earlier, (failed, len(observations)) if failed else None)
        if body is not None:
            subject = f"Price monitor: {config.client}"
            channel_warnings: list[str] = []
            delivered = []
            if config.notify.email:
                delivered.append(self._email(config.notify.email, subject, body, report_path, channel_warnings))
            if config.notify.telegram_chat_id:
                delivered.append(self._telegram(config.notify.telegram_chat_id, body, report_path, channel_warnings))
            warnings.extend(channel_warnings)
            if pending:
                delivery_problems.extend(channel_warnings)
            if not delivered:
                # A report-only client has no channel, so there is nothing to resend.
                warnings.append("client digest not sent: no email or telegram_chat_id in the client config")
                store.mark_delivered(run_id)
            elif any(delivered):
                store.mark_delivered(run_id)

        alert = operator_digest(config, store, run_id, events, self.threshold, delivery_problems)
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

    def test(self, config: ClientConfig) -> list[tuple[str, bool | None, str]]:
        """Send a test message through every channel once: (label, outcome, detail) per channel.

        The outcome is True when sent, False when it failed (the detail says why)
        and None when the channel is not configured.
        """
        subject = f"Price monitor test: {config.client}"
        body = f"This is a test message from the price monitor for {config.client}. No action is needed."
        operator_email = self.env.get("OPERATOR_EMAIL")
        operator_chat = self.env.get("OPERATOR_TELEGRAM_CHAT_ID")
        channels = [
            ("client email", ", ".join(config.notify.email),
             lambda w: self._email(config.notify.email, subject, body, None, w)),
            ("client telegram", config.notify.telegram_chat_id or "",
             lambda w: self._telegram(config.notify.telegram_chat_id, body, None, w)),
            ("operator email", operator_email or "",
             lambda w: self._email([operator_email], subject, body, None, w)),
            ("operator telegram", operator_chat or "",
             lambda w: self._telegram(operator_chat, body, None, w)),
        ]
        results: list[tuple[str, bool | None, str]] = []
        for name, target, attempt in channels:
            if not target:
                results.append((name, None, "not configured"))
                continue
            problems: list[str] = []
            sent = attempt(problems)
            # A refused recipient still counts as handed over for a digest, but a test must show it.
            ok = sent and not problems
            results.append((f"{name} ({target})", ok, "sent" if ok else "; ".join(problems)))
        return results

    def _email(self, to: list[str], subject: str, body: str, attachment: Path | None, warnings: list[str]) -> bool:
        """True when the message was handed to the mail server."""
        if any(not self.env.get(key) for key in _SMTP_KEYS):
            warnings.append("email skipped: SMTP settings missing in .env")
            return False
        try:
            refused = self._send_email(self.env, to, subject, body, attachment)
        except Exception as exc:
            warnings.append(f"email failed: {type(exc).__name__}: {exc}")
            return False
        if refused:
            warnings.append(f"email refused for: {', '.join(sorted(refused))}")
        return True

    def _telegram(self, chat_id: str, text: str, attachment: Path | None, warnings: list[str]) -> bool:
        """True when the message was sent."""
        token = self.env.get("TELEGRAM_BOT_TOKEN")
        if not token:
            warnings.append("telegram skipped: TELEGRAM_BOT_TOKEN missing in .env")
            return False
        try:
            self._send_telegram(self.env, chat_id, text, attachment)
        except TelegramAttachmentError as exc:
            # The text arrived; only the report is missing.
            warnings.append(f"telegram report not attached: {str(exc).replace(token, '***')}")
            return True
        except Exception as exc:
            # The API URL contains the bot token: never let it reach a warning or a log.
            detail = str(exc).replace(token, "***")
            warnings.append(f"telegram failed: {type(exc).__name__}: {detail}")
            return False
        return True
