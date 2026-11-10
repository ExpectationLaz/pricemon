"""Command line interface: run, check-url, report, validate."""

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

from .config import (
    ConfigError,
    Settings,
    SiteRules,
    hostname,
    load_client,
    load_settings,
    load_site_rules,
    rules_for,
)
from .extract import ExtractError, extract
from .fetch import Fetcher
from .models import BLOCKED, DISALLOWED, FETCH_ERROR, NOT_FOUND, OK, RunSummary
from .notify import Notifier
from .report import write_report
from .runner import run_client
from .store import Store

# Relative to the working directory, which is the project folder.
SITES_DIR = Path("sites")
DATA_DIR = Path("data")
SETTINGS_PATH = Path("settings.yaml")
ENV_PATH = Path(".env")

_FETCH_VERDICTS = {
    BLOCKED: "unsupported: the site blocks automated requests",
    DISALLOWED: "unsupported: robots.txt disallows this page",
    NOT_FOUND: "page not found: check the URL",
    FETCH_ERROR: "could not fetch: try again later, or try --browser if the page needs JavaScript",
}
_STOCK_LABELS = {True: "yes", False: "no", None: "unknown"}


def build_fetcher(settings: Settings) -> Fetcher:
    return Fetcher(settings)


def build_notifier(settings: Settings) -> Notifier:
    return Notifier(os.environ, settings)


def _line(label: str, value) -> str:
    return f"{label + ':':<12}{value}"


def print_summary(summary: RunSummary) -> None:
    label = " (dry run, nothing sent)" if summary.dry_run else ""
    alerted = sum(1 for event in summary.events if event.alerted)
    to_verify = sum(1 for event in summary.events if event.verify)
    print(f"{summary.client}: run {summary.run_id}{label}")
    print(f"  URLs:   {summary.urls_total} total, {summary.urls_ok} ok, {summary.urls_failed} failed")
    print(f"  Events: {len(summary.events)} ({alerted} alerted, {to_verify} to verify)")
    for path in summary.report_paths:
        print(f"  Report: {path}")
    for warning in summary.warnings:
        print(f"  Warning: {warning}")


def cmd_run(args) -> int:
    config = load_client(args.config)
    settings = load_settings(SETTINGS_PATH)
    summary = run_client(
        config,
        site_rules=load_site_rules(SITES_DIR),
        data_dir=DATA_DIR,
        fetcher=build_fetcher(settings),
        notifier=build_notifier(settings),
        dry_run=args.dry_run,
    )
    print_summary(summary)
    return 0


def cmd_check_url(args) -> int:
    settings = load_settings(SETTINGS_PATH)
    host = hostname(args.url)
    rules = rules_for(args.url, load_site_rules(SITES_DIR))
    print(_line("URL", args.url))
    print(_line("Site rules", "found" if rules else "none"))
    if args.browser:
        rules = (rules or SiteRules(domain=host)).model_copy(update={"fetcher": "browser"})
    result = build_fetcher(settings).fetch(args.url, rules)
    http = f" (HTTP {result.http_status})" if result.http_status else ""
    print(_line("Fetch", f"{result.status}{http}"))
    if result.status != OK:
        if result.error:
            print(_line("Error", result.error))
        print(_line("Verdict", _FETCH_VERDICTS[result.status]))
        return 0
    try:
        found = extract(result.html, args.url, rules)
    except ExtractError as exc:
        print(_line("Error", exc))
        print(
            _line(
                "Verdict",
                f"needs a site rules file sites/{host}.yaml, or try --browser if the price is drawn by JavaScript",
            )
        )
        return 0
    fields = (
        ("Title", "title", found.title),
        ("Price", "price", found.price),
        ("Currency", "currency", found.currency),
        ("In stock", "in_stock", _STOCK_LABELS[found.in_stock]),
    )
    for label, name, value in fields:
        source = found.sources.get(name)
        shown = "-" if value is None else value
        print(_line(label, f"{shown}  [{source}]" if source else shown))
    note = "" if found.in_stock is not None else " (stock unknown: add a stock rule if the client wants stock alerts)"
    print(_line("Verdict", f"works{note}"))
    return 0


def cmd_validate(args) -> int:
    config = load_client(args.config)
    site_rules = load_site_rules(SITES_DIR)
    urls = [url for product in config.products for url in product.urls]
    hosts = sorted({hostname(url) for url in urls})
    print(f"OK: {config.client}: {len(config.products)} product(s), {len(urls)} URL(s), {len(hosts)} site(s)")
    for host in hosts:
        if host in site_rules:
            print(f"  {host}: site rules found")
        else:
            print(f"  {host}: no site rules (fine if the site publishes structured data; test with check-url)")
    return 0


def cmd_report(args) -> int:
    config = load_client(args.config)
    try:
        day = date.fromisoformat(args.date) if args.date else date.today()
    except ValueError:
        print(f"Invalid date {args.date!r}: use YYYY-MM-DD", file=sys.stderr)
        return 1
    client_dir = DATA_DIR / config.client
    db_path = client_dir / "pricemon.db"
    if not db_path.exists():
        print(f"No data for client {config.client}: run it first", file=sys.stderr)
        return 1
    store = Store(db_path)
    try:
        run_ids = [
            run["id"]
            for run in store.runs()
            if bool(run["dry_run"]) == args.dry_run
            and datetime.fromisoformat(run["started_at"]).astimezone().date() == day
        ]
        if not run_ids:
            kind = "dry run" if args.dry_run else "run"
            print(f"No {kind} found on {day.isoformat()}", file=sys.stderr)
            return 1
        paths = write_report(config, store, run_ids[-1], client_dir / "reports")
    finally:
        store.close()
    for path in paths:
        print(path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pricemon", description="Daily competitor price and stock monitoring")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="check every URL of one client")
    run.add_argument("config", type=Path, help="client config, e.g. clients/acme-shop.yaml")
    run.add_argument("--dry-run", action="store_true", help="do everything except send notifications")
    run.set_defaults(handler=cmd_run)

    check = commands.add_parser("check-url", help="test extraction on one product page")
    check.add_argument("url")
    check.add_argument("--browser", action="store_true", help="load the page in a headless browser")
    check.set_defaults(handler=cmd_check_url)

    validate = commands.add_parser("validate", help="check a client config without fetching anything")
    validate.add_argument("config", type=Path)
    validate.set_defaults(handler=cmd_validate)

    report = commands.add_parser("report", help="rebuild a day's report from stored data, without fetching")
    report.add_argument("config", type=Path)
    report.add_argument("--date", help="local date of the run, YYYY-MM-DD (default: today)")
    report.add_argument("--dry-run", action="store_true", help="rebuild the last dry run of that date instead of the real run")
    report.set_defaults(handler=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv(ENV_PATH)
    try:
        return args.handler(args)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 1
