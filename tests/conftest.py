import pytest

# Credentials a developer may have in the shell: no test may send through them.
_SECRETS = (
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "SMTP_FROM",
    "TELEGRAM_BOT_TOKEN",
    "OPERATOR_EMAIL",
    "OPERATOR_TELEGRAM_CHAT_ID",
)


@pytest.fixture(autouse=True)
def no_real_credentials(monkeypatch):
    for name in _SECRETS:
        monkeypatch.delenv(name, raising=False)
