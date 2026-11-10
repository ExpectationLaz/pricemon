import ssl
from email.utils import parsedate_to_datetime

import pytest

from helpers import ENV

from pricemon.notify.email import build_message, send_email


def test_build_message_with_attachment(tmp_path):
    report = tmp_path / "2026-11-10.xlsx"
    report.write_bytes(b"fake xlsx")
    message = build_message("monitor@example.com", ["a@x.example", "b@x.example"], "Subject", "Body text", report)
    assert message["From"] == "monitor@example.com"
    assert message["To"] == "a@x.example, b@x.example"
    assert message["Subject"] == "Subject"
    assert message.get_body().get_content().strip() == "Body text"
    (attachment,) = list(message.iter_attachments())
    assert attachment.get_filename() == "2026-11-10.xlsx"
    assert attachment.get_payload(decode=True) == b"fake xlsx"


def test_build_message_without_attachment():
    message = build_message("monitor@example.com", ["a@x.example"], "Subject", "Body text", None)
    assert list(message.iter_attachments()) == []


@pytest.fixture
def smtp(monkeypatch):
    """Replace smtplib's classes with recorders. Returns the list of connections made."""
    class Connections(list):
        refused = {}  # what the fake server answers to send_message

    connections = Connections()
    refused = connections.refused

    def recorder(kind):
        class FakeSMTP:
            def __init__(self, host, port, timeout=None, context=None):
                self.kind, self.host, self.port, self.calls = kind, host, port, []
                self.context = context  # what SMTP_SSL was given
                self.starttls_context = None
                connections.append(self)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def starttls(self, context=None):
                self.calls.append("starttls")
                self.starttls_context = context

            def login(self, user, password):
                self.calls.append(("login", user, password))

            def send_message(self, message):
                self.calls.append(("send", message["To"], message["Subject"]))
                return refused

        return FakeSMTP

    monkeypatch.setattr("pricemon.notify.email.smtplib.SMTP", recorder("plain"))
    monkeypatch.setattr("pricemon.notify.email.smtplib.SMTP_SSL", recorder("ssl"))
    return connections


def test_send_email_uses_starttls_on_port_587(smtp):
    send_email(ENV, ["a@x.example"], "Subject", "Body")
    (connection,) = smtp
    assert (connection.kind, connection.host, connection.port) == ("plain", "smtp.example.com", 587)
    assert connection.calls == ["starttls", ("login", "monitor", "secret"), ("send", "a@x.example", "Subject")]


def test_send_email_uses_ssl_on_port_465(smtp):
    send_email({**ENV, "SMTP_PORT": "465"}, ["a@x.example"], "Subject", "Body")
    (connection,) = smtp
    assert (connection.kind, connection.port) == ("ssl", 465)
    assert connection.calls == [("login", "monitor", "secret"), ("send", "a@x.example", "Subject")]


def test_send_email_returns_the_refused_recipients(smtp):
    assert send_email(ENV, ["a@x.example"], "Subject", "Body") == {}
    smtp.refused["b@x.example"] = (550, b"no such user")
    assert send_email(ENV, ["a@x.example", "b@x.example"], "Subject", "Body") == {"b@x.example": (550, b"no such user")}
    assert send_email({**ENV, "SMTP_PORT": "465"}, ["b@x.example"], "Subject", "Body") == smtp.refused


def assert_verifies_certificates(context):
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_starttls_verifies_the_server_certificate(smtp):
    send_email(ENV, ["a@x.example"], "Subject", "Body")
    assert_verifies_certificates(smtp[0].starttls_context)


def test_ssl_connection_verifies_the_server_certificate(smtp):
    send_email({**ENV, "SMTP_PORT": "465"}, ["a@x.example"], "Subject", "Body")
    assert_verifies_certificates(smtp[0].context)


def test_build_message_has_a_date_and_a_message_id():
    message = build_message("monitor@example.com", ["a@x.example"], "Subject", "Body text", None)
    assert parsedate_to_datetime(message["Date"]) is not None
    assert message["Message-ID"].startswith("<") and message["Message-ID"].endswith(">")
    other = build_message("monitor@example.com", ["a@x.example"], "Subject", "Body text", None)
    assert other["Message-ID"] != message["Message-ID"]
