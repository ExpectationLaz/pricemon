"""Email through SMTP."""

import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Mapping

_XLSX_SUBTYPE = "vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def build_message(sender: str, to: list[str], subject: str, body: str, attachment: Path | None) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    message.set_content(body)
    if attachment is not None:
        attachment = Path(attachment)
        message.add_attachment(
            attachment.read_bytes(), maintype="application", subtype=_XLSX_SUBTYPE, filename=attachment.name
        )
    return message


def send_email(env: Mapping[str, str], to: list[str], subject: str, body: str, attachment: Path | None = None) -> None:
    """Send one email. Port 465 uses SSL from the start; any other port uses STARTTLS."""
    message = build_message(env["SMTP_FROM"], to, subject, body, attachment)
    port = int(env.get("SMTP_PORT") or 587)
    if port == 465:
        with smtplib.SMTP_SSL(env["SMTP_HOST"], port, timeout=30) as smtp:
            smtp.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
            smtp.send_message(message)
    else:
        with smtplib.SMTP(env["SMTP_HOST"], port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
            smtp.send_message(message)
