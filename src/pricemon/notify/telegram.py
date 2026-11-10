"""Telegram through the Bot API.

The API URL contains the bot token, so errors raised here never include the URL.
"""

from pathlib import Path
from typing import Mapping

import httpx

_API = "https://api.telegram.org"
_MAX_TEXT = 4000  # Telegram's limit is 4096 characters


class TelegramError(Exception):
    """A Bot API call failed. The message is safe to log."""


def _post(client: httpx.Client, base: str, method: str, **kwargs) -> None:
    try:
        response = client.post(f"{base}/{method}", **kwargs)
    except httpx.HTTPError as exc:
        raise TelegramError(f"{method} failed: {type(exc).__name__}") from None
    if response.status_code != 200:
        raise TelegramError(f"{method} returned HTTP {response.status_code}")


def send_telegram(
    env: Mapping[str, str],
    chat_id: str,
    text: str,
    attachment: Path | None = None,
    client: httpx.Client | None = None,
) -> None:
    """Send a text message, followed by the attachment as a document if given."""
    base = f"{_API}/bot{env['TELEGRAM_BOT_TOKEN']}"
    if len(text) > _MAX_TEXT:
        text = text[:_MAX_TEXT] + "\n... (truncated, see the report)"
    owns_client = client is None
    client = client or httpx.Client(timeout=60)
    try:
        _post(client, base, "sendMessage", data={"chat_id": chat_id, "text": text})
        if attachment is not None:
            attachment = Path(attachment)
            with attachment.open("rb") as handle:
                _post(client, base, "sendDocument", data={"chat_id": chat_id}, files={"document": (attachment.name, handle)})
    finally:
        if owns_client:
            client.close()
