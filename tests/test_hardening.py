"""Small safety nets: credentials, logging, and repository files."""

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pricemon import cli

ROOT = Path(__file__).resolve().parent.parent

# Set while this file is collected, i.e. before any fixture runs, like a developer's shell would have them.
os.environ["SMTP_PASSWORD"] = "real-password-from-the-shell"
os.environ["TELEGRAM_BOT_TOKEN"] = "real-token-from-the-shell"


@pytest.fixture(scope="module", autouse=True)
def clean_up_environment():
    yield
    os.environ.pop("SMTP_PASSWORD", None)
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)


def test_tests_never_see_real_credentials():
    for name in ("SMTP_HOST", "SMTP_PASSWORD", "TELEGRAM_BOT_TOKEN", "OPERATOR_EMAIL"):
        assert name not in os.environ


def test_the_fatal_error_message_names_the_exception_class(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "clients").mkdir()
    (tmp_path / "clients" / "acme.yaml").write_text(
        "client: acme\nproducts:\n  - name: Drill\n    urls: [https://a.example/p/1]\n", encoding="utf-8"
    )

    def broken(settings):
        raise KeyError("missing thing")

    monkeypatch.setattr(cli, "build_fetcher", broken)
    assert cli.main(["run", "clients/acme.yaml"]) == 1
    assert capsys.readouterr().err.strip() == "Fatal error: KeyError: 'missing thing'"


def test_importing_the_telegram_module_silences_httpx_request_logging():
    # A fresh interpreter, so the import really happens here and no other test is disturbed.
    code = "import logging, pricemon.notify.telegram; print(logging.getLogger('httpx').level)"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert int(result.stdout) == logging.WARNING


def test_the_scheduling_wrapper_runs_python_in_utf8_mode():
    lines = (ROOT / "run-client.cmd").read_text(encoding="utf-8").splitlines()
    run_line = next(i for i, line in enumerate(lines) if "pricemon.exe" in line)
    assert "set PYTHONUTF8=1" in [line.strip() for line in lines[:run_line]]


def test_cmd_files_are_checked_out_with_windows_line_endings():
    assert (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines() == ["*.cmd text eol=crlf"]
