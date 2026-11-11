"""Build a sample report and digests from fictional shops, for a portfolio.

Runs the real pipeline twice (a baseline day and a day with changes) against
made-up competitor shops served from memory. Nothing touches the network and
nothing is sent: the email and Telegram senders only record what they would
have sent.

Usage:
    .venv/Scripts/python.exe demo/make_demo.py [--out demo-output]

Writes into the output folder:
    sample-report.xlsx    the client's report for day 2
    sample-report.csv     the Prices sheet as CSV
    client-digest.txt     the message the client would receive
    operator-alert.txt    the message the operator would receive
"""

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import httpx

from pricemon.config import ClientConfig, Settings
from pricemon.fetch import Fetcher
from pricemon.notify import Notifier
from pricemon.runner import run_client

CLIENT = "demo-coffee-shop"

# Fictional competitors. The .example domain is reserved and never resolves.
SHOPS = ("brewhouse.example", "grindstore.example", "beanbarn.example")

PRODUCTS = {
    # product name: (our own price, {shop: day-1 price})
    "Hand grinder C40": ("229.00", {"brewhouse.example": "229.00", "grindstore.example": "219.00", "beanbarn.example": "235.00"}),
    "Gooseneck kettle 0.9 l": ("159.00", {"brewhouse.example": "165.00", "grindstore.example": "159.00", "beanbarn.example": "149.95"}),
    "Pour-over dripper V60": ("24.00", {"brewhouse.example": "22.50", "grindstore.example": "24.00", "beanbarn.example": "23.00"}),
}

DAY_1 = datetime(2026, 11, 9, 7, 0, tzinfo=timezone.utc)
DAY_2 = datetime(2026, 11, 10, 7, 0, tzinfo=timezone.utc)

# Placeholder secrets so the notifier composes messages; the senders below never send.
DEMO_ENV = {
    "SMTP_HOST": "smtp.demo.example",
    "SMTP_PORT": "587",
    "SMTP_USER": "demo",
    "SMTP_PASSWORD": "demo",
    "SMTP_FROM": "monitor@demo.example",
    "OPERATOR_EMAIL": "operator@demo.example",
}


def url_for(shop: str, product: str) -> str:
    return f"https://{shop}/products/{product.lower().replace(' ', '-').replace('.', '')}"


def product_page(name: str, price: str, in_stock: bool = True) -> str:
    data = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": name,
        "offers": {
            "@type": "Offer",
            "price": price,
            "priceCurrency": "GBP",
            "availability": f"https://schema.org/{'InStock' if in_stock else 'OutOfStock'}",
        },
    }
    return (
        f"<html><head><title>{name}</title>"
        f'<script type="application/ld+json">{json.dumps(data)}</script></head>'
        f"<body><h1>{name}</h1></body></html>"
    )


class FakeWeb:
    """The fictional shops: URL -> (HTTP status, page)."""

    def __init__(self):
        self.pages: dict[str, tuple[int, str]] = {}
        for product, (_, prices) in PRODUCTS.items():
            for shop, price in prices.items():
                self.pages[url_for(shop, product)] = (200, product_page(product, price))

    def handler(self, request: httpx.Request) -> httpx.Response:
        status, body = self.pages.get(str(request.url), (404, ""))
        return httpx.Response(status, text=body)

    def day_2_changes(self) -> None:
        self.pages[url_for("grindstore.example", "Hand grinder C40")] = (200, product_page("Hand grinder C40", "199.00"))
        self.pages[url_for("beanbarn.example", "Gooseneck kettle 0.9 l")] = (
            200,
            product_page("Gooseneck kettle 0.9 l", "149.95", in_stock=False),
        )
        self.pages[url_for("brewhouse.example", "Pour-over dripper V60")] = (200, product_page("Pour-over dripper V60", "25.00"))
        self.pages[url_for("beanbarn.example", "Pour-over dripper V60")] = (403, "")  # this shop refuses the request


class Outbox:
    def __init__(self):
        self.emails: list[tuple[list[str], str, str]] = []

    def email(self, env, to, subject, body, attachment=None):
        self.emails.append((to, subject, body))
        return {}

    def telegram(self, env, chat_id, text, attachment=None):
        raise AssertionError("the demo has no Telegram channel")


def build_config() -> ClientConfig:
    return ClientConfig.model_validate(
        {
            "client": CLIENT,
            "products": [
                {"name": product, "own_price": own, "urls": [url_for(shop, product) for shop in SHOPS]}
                for product, (own, _) in PRODUCTS.items()
            ],
            "notify": {"email": [f"owner@{CLIENT}.example"]},
        }
    )


def make_demo(out_dir: Path) -> dict[str, Path]:
    """Run the two demo days and write the sample files. Returns their paths."""
    out_dir = Path(out_dir)
    work_dir = out_dir / "work"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    web = FakeWeb()
    settings = Settings(respect_robots_txt=False, min_interval_seconds=0, retries=0, operator_failure_threshold=1)
    fetcher = Fetcher(settings, client=httpx.Client(transport=httpx.MockTransport(web.handler)), sleep=lambda s: None)
    outbox = Outbox()
    notifier = Notifier(DEMO_ENV, settings, send_email=outbox.email, send_telegram=outbox.telegram)
    config = build_config()

    def run(day: datetime):
        return run_client(
            config, site_rules={}, data_dir=work_dir, fetcher=fetcher, notifier=notifier, now=lambda: day
        )

    run(DAY_1)  # baseline: nothing to report
    web.day_2_changes()
    summary = run(DAY_2)

    report_xlsx = Path(summary.report_paths[0])
    report_csv = Path(summary.report_paths[1])
    files = {
        "report_xlsx": out_dir / "sample-report.xlsx",
        "report_csv": out_dir / "sample-report.csv",
        "client_digest": out_dir / "client-digest.txt",
        "operator_alert": out_dir / "operator-alert.txt",
    }
    shutil.copyfile(report_xlsx, files["report_xlsx"])
    shutil.copyfile(report_csv, files["report_csv"])
    by_recipient = {to[0]: body for to, _, body in outbox.emails}
    files["client_digest"].write_text(by_recipient[f"owner@{CLIENT}.example"] + "\n", encoding="utf-8")
    files["operator_alert"].write_text(by_recipient["operator@demo.example"] + "\n", encoding="utf-8")
    shutil.rmtree(work_dir)
    return files


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("demo-output"))
    args = parser.parse_args()
    files = make_demo(args.out)
    for path in files.values():
        print(path)
    print()
    print(files["client_digest"].read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
