"""The portfolio demo keeps working as the engine changes."""

import importlib.util
from pathlib import Path

from openpyxl import load_workbook

DEMO = Path(__file__).resolve().parents[1] / "demo" / "make_demo.py"


def load_demo():
    spec = importlib.util.spec_from_file_location("make_demo", DEMO)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_writes_a_report_and_both_digests(tmp_path):
    files = load_demo().make_demo(tmp_path / "out")
    assert all(path.exists() for path in files.values())
    assert not (tmp_path / "out" / "work").exists()

    digest = files["client_digest"].read_text(encoding="utf-8").splitlines()
    assert digest[0] == "demo-coffee-shop: 3 changes today"
    assert "Hand grinder C40 at grindstore.example: 219.00 to 199.00 GBP, down 9.1%" in digest
    assert "Gooseneck kettle 0.9 l at beanbarn.example: out of stock" in digest
    assert "Pour-over dripper V60 at brewhouse.example: 22.50 to 25.00 GBP, up 11.1%" in digest
    assert digest[-1] == "1 of 9 pages could not be checked today."

    alert = files["operator_alert"].read_text(encoding="utf-8")
    assert "FAILING https://beanbarn.example/products/pour-over-dripper-v60: blocked (HTTP 403)" in alert

    workbook = load_workbook(files["report_xlsx"])
    assert workbook["Prices"].max_row == 10
    assert workbook["Changes"].max_row == 4
    assert workbook["Problems"].max_row == 2


def test_demo_can_run_twice_into_the_same_folder(tmp_path):
    demo = load_demo()
    demo.make_demo(tmp_path / "out")
    files = demo.make_demo(tmp_path / "out")
    assert files["client_digest"].read_text(encoding="utf-8").startswith("demo-coffee-shop: 3 changes today")
