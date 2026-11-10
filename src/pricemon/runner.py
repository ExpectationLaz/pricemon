"""Orchestrates one run for one client. The only module that knows the stage order."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .config import ClientConfig, SiteRules, hostname, rules_for
from .detect import detect
from .extract import extract
from .fetch import Fetcher
from .models import EXTRACT_ERROR, FETCH_ERROR, OK, VERIFY, Observation, RunSummary
from .report import write_report
from .store import Store


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def order_round_robin(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Reorder (url, product_name) items so consecutive requests hit different hosts.

    Rate-limit waits for one host then overlap with requests to the others.
    """
    queues: dict[str, list[tuple[str, str]]] = {}
    for item in items:
        queues.setdefault(hostname(item[0]), []).append(item)
    ordered: list[tuple[str, str]] = []
    while queues:
        for host in list(queues):
            ordered.append(queues[host].pop(0))
            if not queues[host]:
                del queues[host]
    return ordered


def observe(url: str, product_name: str, fetcher, rules: SiteRules | None, now=utc_now) -> Observation:
    """Fetch and extract one URL. Never raises: every failure becomes an observation."""
    fetched_at = now().isoformat()
    try:
        result = fetcher.fetch(url, rules)
    except Exception as exc:
        return Observation(
            url=url, product_name=product_name, fetched_at=fetched_at, status=FETCH_ERROR,
            error=f"unexpected error: {type(exc).__name__}: {exc}",
        )
    if result.status != OK:
        return Observation(
            url=url, product_name=product_name, fetched_at=fetched_at, status=result.status,
            http_status=result.http_status, error=result.error,
        )
    try:
        found = extract(result.html, url, rules)
    except Exception as exc:  # ExtractError, or a parser crash on a strange page
        return Observation(
            url=url, product_name=product_name, fetched_at=fetched_at, status=EXTRACT_ERROR,
            http_status=result.http_status, error=str(exc),
        )
    return Observation(
        url=url, product_name=product_name, fetched_at=fetched_at, status=OK,
        http_status=result.http_status, title=found.title, price=found.price,
        currency=found.currency, in_stock=found.in_stock, method=found.method,
    )


def run_client(
    config: ClientConfig,
    *,
    site_rules: dict[str, SiteRules],
    data_dir: Path,
    fetcher: Fetcher,
    notifier=None,
    dry_run: bool = False,
    now=utc_now,
) -> RunSummary:
    client_dir = Path(data_dir) / config.client
    store = Store(client_dir / "pricemon.db")
    try:
        run_id = store.start_run(now().isoformat(), dry_run)
        summary = RunSummary(client=config.client, run_id=run_id, dry_run=dry_run)
        items = order_round_robin([(url, product.name) for product in config.products for url in product.urls])
        for url, product_name in items:
            observation = observe(url, product_name, fetcher, rules_for(url, site_rules), now)
            # A real run ignores dry runs; a dry run sees everything, as a preview.
            previous = store.last_ok(url, include_dry_runs=dry_run)
            events = detect(previous, observation, config.alerts) if observation.status == OK else []
            suspect = next((event for event in events if event.verify), None)
            if suspect is not None:
                # A price that jumped this far is probably a layout change. Hold it: it is
                # never a baseline, and the client is told nothing until the operator accepts it.
                observation = replace(
                    observation,
                    status=VERIFY,
                    error=f"price changed by {suspect.change_pct}% ({suspect.old_price:.2f} to "
                    f"{suspect.new_price:.2f}): held for verification",
                )
                events = [replace(event, alerted=False) for event in events]
            saved = store.add_observation(run_id, observation)
            summary.urls_total += 1
            if saved.status == OK:
                summary.urls_ok += 1
            else:
                summary.urls_failed += 1
            for event in events:
                store.add_event(run_id, saved.id, event)
                summary.events.append(event)
        store.finish_run(run_id, now().isoformat(), summary.urls_total, summary.urls_ok, summary.urls_failed)
        try:
            report_paths = write_report(config, store, run_id, client_dir / "reports")
            summary.report_paths = [str(path) for path in report_paths]
        except Exception as exc:  # e.g. the client has the file open in Excel
            summary.warnings.append(f"report not written: {type(exc).__name__}: {exc}")
        if notifier is not None and not dry_run:
            report_path = Path(summary.report_paths[0]) if summary.report_paths else None
            try:
                summary.warnings.extend(notifier.send(config, store, run_id, summary.events, report_path))
            except Exception as exc:  # a notification problem must not fail the run
                summary.warnings.append(f"notification failed: {type(exc).__name__}: {exc}")
            # Alerts that no channel took stay queued; the next real run sends them again.
            summary.undelivered = len(store.undelivered_events(run_id))
        return summary
    finally:
        store.close()
