"""Email through SMTP."""

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Mapping

_XLSX_SUBTYPE = "vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def build_message(sender: str, to: list[str], subject: str, body: str, attachment: Path | None) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()
    message.set_content(body)
    if attachment is not None:
        attachment = Path(attachment)
        message.add_attachment(
            attachment.read_bytes(), maintype="application", subtype=_XLSX_SUBTYPE, filename=attachment.name
        )
    return message


def send_email(
    env: Mapping[str, str], to: list[str], subject: str, body: str, attachment: Path | None = None
) -> dict:
    """Send one email. Port 465 uses SSL from the start; any other port uses STARTTLS.

    Returns the recipients the server refused (empty when all were accepted).
    """
    message = build_message(env["SMTP_FROM"], to, subject, body, attachment)
    port = int(env.get("SMTP_PORT") or 587)
    if port == 465:
        with smtplib.SMTP_SSL(env["SMTP_HOST"], port, timeout=30, context=ssl.create_default_context()) as smtp:
            smtp.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
            return smtp.send_message(message)
    else:
        with smtplib.SMTP(env["SMTP_HOST"], port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
            return smtp.send_message(message)
