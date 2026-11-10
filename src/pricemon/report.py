"""The daily report: an Excel workbook and an optional CSV."""

import csv
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.utils import get_column_letter
from openpyxl.styles import Font

from .config import ClientConfig, hostname
from .models import OK
from .store import Store

PRICES_HEADER = [
    "Product", "SKU", "Own price", "Competitor", "Title", "Price", "Currency", "In stock",
    "Previous price", "Change %", "Last checked", "Status", "URL",
]
CHANGES_HEADER = ["Product", "Competitor", "Change", "Old price", "New price", "Change %", "Verify", "URL"]
PROBLEMS_HEADER = ["Product", "Competitor", "Status", "Reason", "Failed runs in a row", "URL"]

_CHANGE_LABELS = {
    "price_down": "price down",
    "price_up": "price up",
    "out_of_stock": "out of stock",
    "back_in_stock": "back in stock",
}
_STOCK_LABELS = {True: "yes", False: "no", None: "unknown"}
_TWO_PLACES = Decimal("0.01")
_PRICES_NUMBER_COLUMNS = ("Own price", "Price", "Previous price", "Change %")
_CHANGES_NUMBER_COLUMNS = ("Old price", "New price", "Change %")
_FORMULA_STARTS = ("=", "+", "-", "@", chr(9), chr(13))


def _clean(text: str) -> str:
    """Remove characters that are illegal in an xlsx file."""
    return ILLEGAL_CHARACTERS_RE.sub("", text)


def _csv_value(value):
    """A CSV cell: None is empty, text that could run as a formula gets a leading quote."""
    if value is None:
        return ""
    if isinstance(value, str):
        value = _clean(value)
        if value.startswith(_FORMULA_STARTS):
            return "'" + value
    return value


def _set_cell(cell, value) -> None:
    """Write a value; text is always stored as a string, never as a formula."""
    if isinstance(value, str):
        # openpyxl 3.1 has no set_explicit_value: assigning the value may mark "=..." as a
        # formula, so force the data type back to string afterwards.
        cell.value = _clean(value)
        cell.data_type = "s"
    else:
        cell.value = value


def _column_width(texts: list[str]) -> int:
    return min(60, max(8, max((len(text) for text in texts), default=0) + 2))


@dataclass
class ReportData:
    prices: list[list] = field(default_factory=list)
    changes: list[list] = field(default_factory=list)
    problems: list[list] = field(default_factory=list)


def _local_time(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")


def build_report(config: ClientConfig, store: Store, run_id: int) -> ReportData:
    products = {url: product for product in config.products for url in product.urls}
    data = ReportData()
    # A real run's report ignores dry runs; a dry run's report sees everything.
    include_dry_runs = bool(store.run(run_id)["dry_run"])
    for obs in store.run_observations(run_id):
        product = products.get(obs.url)
        sku = product.sku if product else None
        own_price = product.own_price if product else None
        host = hostname(obs.url)
        previous = store.last_ok(obs.url, before_run_id=run_id, include_dry_runs=include_dry_runs)

        if obs.status == OK:
            same_currency = previous is not None and previous.currency == obs.currency
            previous_price = previous.price if same_currency else None
            change = None
            if previous_price is not None:
                change = ((obs.price - previous_price) / previous_price * 100).quantize(_TWO_PLACES)
            data.prices.append([
                obs.product_name, sku, own_price, host, obs.title, obs.price, obs.currency,
                _STOCK_LABELS[obs.in_stock], previous_price, change, _local_time(obs.fetched_at),
                obs.status, obs.url,
            ])
            if previous is not None and not same_currency:
                data.problems.append([
                    obs.product_name, host, "currency_changed",
                    f"currency changed from {previous.currency} to {obs.currency}", 0, obs.url,
                ])
        else:
            # A failed page still shows its last known values.
            data.prices.append([
                obs.product_name, sku, own_price, host,
                previous.title if previous else None,
                previous.price if previous else None,
                previous.currency if previous else None,
                _STOCK_LABELS[previous.in_stock if previous else None],
                None, None,
                _local_time(previous.fetched_at) if previous else None,
                obs.status, obs.url,
            ])
            data.problems.append([
                obs.product_name, host, obs.status, obs.error or obs.status,
                store.consecutive_failures(obs.url, up_to_run_id=run_id, include_dry_runs=include_dry_runs),
                obs.url,
            ])

    data.prices.sort(key=lambda row: (row[0], row[3]))
    for event in store.run_events(run_id):
        data.changes.append([
            event.product_name, hostname(event.url), _CHANGE_LABELS[event.type], event.old_price,
            event.new_price, event.change_pct, "verify" if event.verify else None, event.url,
        ])
    return data


def write_report(config: ClientConfig, store: Store, run_id: int, out_dir: Path) -> list[Path]:
    """Write the report for a run. Returns the xlsx path first, then the csv path if written."""
    data = build_report(config, store, run_id)
    run = store.run(run_id)
    day = datetime.fromisoformat(run["started_at"]).astimezone().date().isoformat()
    stem = f"{day}-dry-run" if run["dry_run"] else day
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()
    sheets = (("Prices", PRICES_HEADER, data.prices, _PRICES_NUMBER_COLUMNS),
              ("Changes", CHANGES_HEADER, data.changes, _CHANGES_NUMBER_COLUMNS),
              ("Problems", PROBLEMS_HEADER, data.problems, ()))
    for index, (title, header, rows, number_columns) in enumerate(sheets):
        worksheet = workbook.active if index == 0 else workbook.create_sheet()
        worksheet.title = title
        for column, name in enumerate(header, start=1):
            worksheet.cell(row=1, column=column, value=name).font = Font(bold=True)
        worksheet.freeze_panes = "A2"
        for row_number, row in enumerate(rows, start=2):
            for column, value in enumerate(row, start=1):
                cell = worksheet.cell(row=row_number, column=column)
                _set_cell(cell, value)
                if header[column - 1] in number_columns:
                    cell.number_format = "0.00"
        for column, name in enumerate(header, start=1):
            texts = [name] + ["" if row[column - 1] is None else _clean(str(row[column - 1])) for row in rows]
            worksheet.column_dimensions[get_column_letter(column)].width = _column_width(texts)
    xlsx_path = out_dir / f"{stem}.xlsx"
    workbook.save(str(xlsx_path))
    paths = [xlsx_path]

    if "csv" in config.report.formats:
        csv_path = out_dir / f"{stem}.csv"
        # utf-8-sig so Excel opens the file with the right encoding.
        with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(PRICES_HEADER)
            writer.writerows([_csv_value(value) for value in row] for row in data.prices)
        paths.append(csv_path)
    return paths
