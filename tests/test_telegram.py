import httpx
import pytest

from helpers import ENV

from pricemon.notify.telegram import TelegramError, send_telegram


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_sends_text_then_document(tmp_path):
    report = tmp_path / "2026-11-10.xlsx"
    report.write_bytes(b"fake xlsx")
    seen = []

    def handler(request):
        seen.append((request.url.path, request.content))
        return httpx.Response(200, json={"ok": True})

    send_telegram(ENV, "123", "hello", report, client=client_for(handler))
    assert [path for path, _ in seen] == ["/botTOKEN/sendMessage", "/botTOKEN/sendDocument"]
    assert b"chat_id=123" in seen[0][1] and b"text=hello" in seen[0][1]
    assert b"2026-11-10.xlsx" in seen[1][1] and b"fake xlsx" in seen[1][1]


def test_text_only_when_there_is_no_attachment():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"ok": True})

    send_telegram(ENV, "123", "hello", client=client_for(handler))
    assert seen == ["/botTOKEN/sendMessage"]


def test_api_error_does_not_leak_the_token():
    def handler(request):
        return httpx.Response(401, json={"ok": False})

    with pytest.raises(TelegramError) as error:
        send_telegram(ENV, "123", "hello", client=client_for(handler))
    assert str(error.value) == "sendMessage returned HTTP 401"
    assert "TOKEN" not in str(error.value)


def test_network_error_does_not_leak_the_token():
    def handler(request):
        raise httpx.ConnectError("cannot reach api.telegram.org/botTOKEN", request=request)

    with pytest.raises(TelegramError) as error:
        send_telegram(ENV, "123", "hello", client=client_for(handler))
    assert str(error.value) == "sendMessage failed: ConnectError"


def test_unexpected_error_does_not_leak_the_token():
    def handler(request):
        raise ValueError("bad request to api.telegram.org/botTOKEN")

    with pytest.raises(TelegramError) as error:
        send_telegram(ENV, "123", "hello", client=client_for(handler))
    assert str(error.value) == "sendMessage failed: ValueError"


def test_long_text_is_truncated():
    seen = []

    def handler(request):
        seen.append(request.content)
        return httpx.Response(200, json={"ok": True})

    send_telegram(ENV, "123", "z" * 5000, client=client_for(handler))
    assert seen[0].count(b"z") == 4000
    assert b"truncated" in seen[0]
