# Price Monitor Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `pricemon`, a command line engine that checks competitor product pages once a day for a client, stores price and stock history, and produces a spreadsheet report plus email and Telegram alerts.

**Architecture:** One run is a one-way pipeline: client config -> fetch -> extract -> store -> detect changes -> report + notify. Each stage is its own module that passes plain dataclasses; only `runner.py` knows the order. Extraction tries structured data first (JSON-LD, then meta tags) and falls back to per-site CSS rules, so most new clients need only a YAML file.

**Tech Stack:** Python 3.13, httpx, selectolax, PyYAML, pydantic v2, openpyxl, python-dotenv, argparse, SQLite (standard library), pytest. Playwright as an optional extra.

**Spec:** `docs/superpowers/specs/2026-11-10-price-monitor-engine-design.md`

## Global Constraints

- Python 3.13. The package is `pricemon`, source in `src/pricemon/`, tests in `tests/`.
- Runtime dependencies are exactly: httpx, selectolax, PyYAML, pydantic, openpyxl, python-dotenv. Playwright is only the optional extra `pricemon[browser]`. Do not add others.
- All commands run from the `price-monitor` folder. Use the project's own environment: `.venv/Scripts/python.exe`.
- Prices are `decimal.Decimal` in code and decimal text in SQLite. Never use floats for prices.
- Timestamps are UTC ISO 8601 strings (`datetime.now(timezone.utc).isoformat()`).
- Bot protection is never bypassed: no CAPTCHA solving, no retrying around a 403/429, no proxy or identity rotation. A blocked page gets status `blocked`.
- One failing page must never stop a run.
- Tests never touch the network. HTTP is mocked with `httpx.MockTransport`.
- Secrets (`.env`), `settings.yaml`, client configs (except `clients/example.yaml`) and `data/` are never committed. `.gitignore` already covers them.
- Commit messages end with a blank line followed by `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`.

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | Package metadata, dependencies, `pricemon` console script |
| `src/pricemon/__init__.py` | Version |
| `src/pricemon/__main__.py` | `python -m pricemon` |
| `src/pricemon/models.py` | Status constants and dataclasses passed between stages |
| `src/pricemon/config.py` | Load and validate client config, site rules, settings |
| `src/pricemon/extract/price.py` | Price text -> `Decimal` |
| `src/pricemon/extract/common.py` | Currency and availability normalisation |
| `src/pricemon/extract/jsonld.py` | schema.org Product from JSON-LD |
| `src/pricemon/extract/meta.py` | Product meta tags |
| `src/pricemon/extract/rules.py` | Per-site CSS rules |
| `src/pricemon/extract/__init__.py` | The extraction chain |
| `src/pricemon/fetch.py` | HTTP fetcher, rate limit, robots, block detection, browser fetcher |
| `src/pricemon/store.py` | SQLite access |
| `src/pricemon/detect.py` | Change detection (pure function) |
| `src/pricemon/runner.py` | Orchestrates one run |
| `src/pricemon/report.py` | Excel and CSV report |
| `src/pricemon/notify/__init__.py` | Digest text and channel dispatch |
| `src/pricemon/notify/email.py` | SMTP sender |
| `src/pricemon/notify/telegram.py` | Telegram sender |
| `src/pricemon/cli.py` | `run`, `check-url`, `report`, `validate` |
| `clients/example.yaml`, `settings.example.yaml`, `.env.example` | Examples |
| `sites/` | Per-domain rules files |
| `run-client.cmd`, `README.md` | Scheduling wrapper and operator guide |

---

### Task 1: Project skeleton, models and configuration

**Files:**
- Create: `pyproject.toml`, `src/pricemon/__init__.py`, `src/pricemon/models.py`, `src/pricemon/config.py`
- Create: `clients/example.yaml`, `settings.example.yaml`, `.env.example`, `sites/.gitkeep`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces (`pricemon.models`): constants `OK`, `FETCH_ERROR`, `BLOCKED`, `DISALLOWED`, `NOT_FOUND`, `EXTRACT_ERROR`; dataclasses `FetchResult`, `Partial`, `Extraction`, `Observation`, `Event`, `RunSummary` exactly as defined in Step 2.
- Produces (`pricemon.config`): `ConfigError`; models `ClientConfig`, `Product`, `Alerts`, `Notify`, `Report`, `SiteRules`, `Selector`, `StockRule`, `Settings`; functions `hostname(url: str) -> str`, `load_client(path) -> ClientConfig`, `load_settings(path) -> Settings`, `load_site_rules(sites_dir) -> dict[str, SiteRules]`, `rules_for(url: str, site_rules: dict[str, SiteRules]) -> SiteRules | None`.

- [ ] **Step 1: Create the package skeleton**

`pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "pricemon"
version = "0.1.0"
description = "Daily competitor price and stock monitoring"
requires-python = ">=3.13"
dependencies = [
    "httpx>=0.27",
    "selectolax>=0.3.21",
    "PyYAML>=6.0",
    "pydantic>=2.6",
    "openpyxl>=3.1",
    "python-dotenv>=1.0",
]

[project.optional-dependencies]
browser = ["playwright>=1.44"]
dev = ["pytest>=8.0"]

[project.scripts]
pricemon = "pricemon.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

`src/pricemon/__init__.py`:

```python
__version__ = "0.1.0"
```

`sites/.gitkeep`: an empty file.

- [ ] **Step 2: Write `src/pricemon/models.py`**

```python
"""Plain data objects passed between the stages of a run."""

from dataclasses import dataclass, field
from decimal import Decimal

# Observation and fetch statuses
OK = "ok"
FETCH_ERROR = "fetch_error"
BLOCKED = "blocked"
DISALLOWED = "disallowed"
NOT_FOUND = "not_found"
EXTRACT_ERROR = "extract_error"


@dataclass(frozen=True)
class FetchResult:
    status: str
    http_status: int | None = None
    html: str | None = None
    error: str | None = None


@dataclass
class Partial:
    """What one extraction step found. Any field may be missing."""

    title: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    in_stock: bool | None = None


@dataclass(frozen=True)
class Extraction:
    title: str | None
    price: Decimal
    currency: str | None
    in_stock: bool | None
    method: str  # the step that produced the price: jsonld, meta or rules
    sources: dict[str, str] = field(default_factory=dict)  # field name -> step


@dataclass(frozen=True)
class Observation:
    url: str
    product_name: str
    fetched_at: str
    status: str
    http_status: int | None = None
    title: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    in_stock: bool | None = None
    method: str | None = None
    error: str | None = None
    id: int | None = None


@dataclass(frozen=True)
class Event:
    url: str
    product_name: str
    type: str  # price_down, price_up, out_of_stock, back_in_stock
    old_price: Decimal | None
    new_price: Decimal | None
    change_pct: Decimal | None
    verify: bool
    alerted: bool
    currency: str | None = None


@dataclass
class RunSummary:
    client: str
    run_id: int
    dry_run: bool = False
    urls_total: int = 0
    urls_ok: int = 0
    urls_failed: int = 0
    events: list[Event] = field(default_factory=list)
    report_paths: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
```

- [ ] **Step 3: Write the example files**

`clients/example.yaml`:

```yaml
client: example-shop
products:
  - name: "Bosch GSR 12V drill"
    sku: "BOS-123"
    own_price: 129.00
    urls:
      - https://competitor-a.example/p/123
      - https://competitor-b.example/drill
alerts:
  events: [price_down, price_up, out_of_stock, back_in_stock]
  min_price_change_pct: 1.0
  verify_threshold_pct: 70
notify:
  email: [owner@example-shop.example]
  telegram_chat_id: "123456"
  send_when_no_changes: false
report:
  formats: [xlsx, csv]
```

`settings.example.yaml`:

```yaml
user_agent: "pricemon/0.1"
min_interval_seconds: 5
timeout_seconds: 20
retries: 2
respect_robots_txt: true
operator_failure_threshold: 3
```

`.env.example`:

```
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=
TELEGRAM_BOT_TOKEN=
OPERATOR_EMAIL=
OPERATOR_TELEGRAM_CHAT_ID=
```

- [ ] **Step 4: Create the environment and install**

Run:

```bash
py -3.13 -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

Expected: ends with `Successfully installed ... pricemon-0.1.0 ...`.

- [ ] **Step 5: Write the failing tests**

`tests/test_config.py`:

```python
from decimal import Decimal
from pathlib import Path

import pytest

from pricemon.config import (
    ConfigError,
    hostname,
    load_client,
    load_settings,
    load_site_rules,
    rules_for,
)

ROOT = Path(__file__).resolve().parents[1]

VALID = """\
client: acme-shop
products:
  - name: Drill
    sku: 12345
    own_price: 129.00
    urls:
      - https://competitor-a.example/p/123
      - https://www.competitor-b.example/drill
"""


def write(folder, name, text):
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_valid_client_with_defaults(tmp_path):
    cfg = load_client(write(tmp_path, "acme.yaml", VALID))
    assert cfg.client == "acme-shop"
    assert cfg.products[0].sku == "12345"
    assert cfg.products[0].own_price == Decimal("129")
    assert len(cfg.products[0].urls) == 2
    assert set(cfg.alerts.events) == {"price_down", "price_up", "out_of_stock", "back_in_stock"}
    assert cfg.alerts.min_price_change_pct == Decimal("1.0")
    assert cfg.alerts.verify_threshold_pct == Decimal("70")
    assert cfg.notify.email == []
    assert cfg.notify.telegram_chat_id is None
    assert cfg.notify.send_when_no_changes is False
    assert cfg.report.formats == ["xlsx", "csv"]


def test_unknown_key_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="colour"):
        load_client(write(tmp_path, "bad.yaml", VALID + "colour: blue\n"))


def test_duplicate_url_is_an_error(tmp_path):
    text = VALID + "  - name: Other\n    urls:\n      - https://competitor-a.example/p/123\n"
    with pytest.raises(ConfigError, match="more than once"):
        load_client(write(tmp_path, "dup.yaml", text))


def test_product_needs_at_least_one_url(tmp_path):
    text = "client: acme\nproducts:\n  - name: Drill\n    urls: []\n"
    with pytest.raises(ConfigError, match="urls"):
        load_client(write(tmp_path, "nourl.yaml", text))


def test_client_needs_at_least_one_product(tmp_path):
    with pytest.raises(ConfigError, match="products"):
        load_client(write(tmp_path, "noprod.yaml", "client: acme\nproducts: []\n"))


def test_client_name_must_be_lowercase_digits_hyphens(tmp_path):
    text = VALID.replace("acme-shop", "Acme Shop")
    with pytest.raises(ConfigError, match="client"):
        load_client(write(tmp_path, "name.yaml", text))


def test_url_must_be_http(tmp_path):
    text = VALID.replace("https://competitor-a.example/p/123", "ftp://competitor-a.example/p")
    with pytest.raises(ConfigError, match="not an http"):
        load_client(write(tmp_path, "ftp.yaml", text))


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="file not found"):
        load_client(tmp_path / "nope.yaml")


def test_invalid_yaml_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_client(write(tmp_path, "broken.yaml", "client: [unclosed\n"))


def test_example_client_config_is_valid():
    cfg = load_client(ROOT / "clients" / "example.yaml")
    assert cfg.client == "example-shop"
    assert cfg.notify.telegram_chat_id == "123456"


def test_settings_defaults_when_file_missing(tmp_path):
    settings = load_settings(tmp_path / "settings.yaml")
    assert settings.user_agent == "pricemon/0.1"
    assert settings.min_interval_seconds == 5
    assert settings.timeout_seconds == 20
    assert settings.retries == 2
    assert settings.respect_robots_txt is True
    assert settings.operator_failure_threshold == 3


def test_settings_file_overrides_some_keys(tmp_path):
    settings = load_settings(write(tmp_path, "settings.yaml", "retries: 0\n"))
    assert settings.retries == 0
    assert settings.min_interval_seconds == 5


def test_example_settings_file_is_valid():
    assert load_settings(ROOT / "settings.example.yaml").retries == 2


def test_hostname_strips_www_and_lowercases():
    assert hostname("https://WWW.Shop.Example/p/1?x=2") == "shop.example"
    assert hostname("https://shop.example.com/p") == "shop.example.com"


def test_site_rules_loaded_and_matched_by_hostname(tmp_path):
    sites = tmp_path / "sites"
    sites.mkdir()
    write(
        sites,
        "b.yaml",
        "domain: www.Competitor-B.example\n"
        "price: {css: span.price}\n"
        "currency: EUR\n"
        "decimal_separator: ','\n"
        "stock: {css: .stock, in_stock_text: [In stock], out_of_stock_text: [Sold out]}\n",
    )
    rules = load_site_rules(sites)
    found = rules_for("https://competitor-b.example/drill", rules)
    assert found.price.css == "span.price"
    assert found.price.attr is None
    assert found.currency == "EUR"
    assert found.fetcher == "http"
    assert found.skip_structured_data is False
    assert found.stock.out_of_stock_text == ["Sold out"]
    assert rules_for("https://shop.competitor-b.example/drill", rules) is None


def test_site_rules_missing_folder_is_empty(tmp_path):
    assert load_site_rules(tmp_path / "nope") == {}
```

- [ ] **Step 6: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'pricemon.config'`.

- [ ] **Step 7: Write `src/pricemon/config.py`**

```python
"""Loading and validation of client configs, site rules and settings."""

from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

EventType = Literal["price_down", "price_up", "out_of_stock", "back_in_stock"]


class ConfigError(Exception):
    """A config file is missing, unreadable or invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)


class Product(_Strict):
    name: str = Field(min_length=1)
    sku: str | None = None
    own_price: Decimal | None = None
    urls: list[str] = Field(min_length=1)

    @field_validator("urls")
    @classmethod
    def _http_urls(cls, urls: list[str]) -> list[str]:
        for url in urls:
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError(f"not an http(s) URL: {url}")
        return urls


class Alerts(_Strict):
    events: list[EventType] = ["price_down", "price_up", "out_of_stock", "back_in_stock"]
    min_price_change_pct: Decimal = Decimal("1.0")
    verify_threshold_pct: Decimal = Decimal("70")


class Notify(_Strict):
    email: list[str] = []
    telegram_chat_id: str | None = None
    send_when_no_changes: bool = False


class Report(_Strict):
    formats: list[Literal["xlsx", "csv"]] = ["xlsx", "csv"]


class ClientConfig(_Strict):
    client: str = Field(pattern=r"^[a-z0-9-]+$")
    products: list[Product] = Field(min_length=1)
    alerts: Alerts = Field(default_factory=Alerts)
    notify: Notify = Field(default_factory=Notify)
    report: Report = Field(default_factory=Report)

    @model_validator(mode="after")
    def _unique_urls(self) -> "ClientConfig":
        seen: set[str] = set()
        for product in self.products:
            for url in product.urls:
                if url in seen:
                    raise ValueError(f"URL listed more than once: {url}")
                seen.add(url)
        return self


class Selector(_Strict):
    css: str
    attr: str | None = None


class StockRule(_Strict):
    css: str
    in_stock_text: list[str] = []
    out_of_stock_text: list[str] = []


class SiteRules(_Strict):
    domain: str
    fetcher: Literal["http", "browser"] = "http"
    min_interval_seconds: float | None = None
    skip_structured_data: bool = False
    title: Selector | None = None
    price: Selector | None = None
    currency: str | Selector | None = None
    decimal_separator: Literal[".", ","] | None = None
    stock: StockRule | None = None


class Settings(_Strict):
    user_agent: str = "pricemon/0.1"
    min_interval_seconds: float = 5
    timeout_seconds: float = 20
    retries: int = 2
    respect_robots_txt: bool = True
    operator_failure_threshold: int = 3


def _strip_www(host: str) -> str:
    host = host.strip().lower()
    return host[4:] if host.startswith("www.") else host


def hostname(url: str) -> str:
    """Hostname of a URL, lowercased and without a leading 'www.'."""
    return _strip_www(urlsplit(url).hostname or "")


def _load_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"file not found: {path}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return data


def _validate(model, data: dict, path: Path):
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'config'}: {error['msg']}"
            for error in exc.errors()
        )
        raise ConfigError(f"{path}: {problems}") from None


def load_client(path) -> ClientConfig:
    path = Path(path)
    return _validate(ClientConfig, _load_yaml(path), path)


def load_settings(path) -> Settings:
    path = Path(path)
    if not path.exists():
        return Settings()
    return _validate(Settings, _load_yaml(path), path)


def load_site_rules(sites_dir) -> dict[str, SiteRules]:
    """All rules files in a folder, keyed by hostname without 'www.'."""
    sites_dir = Path(sites_dir)
    if not sites_dir.is_dir():
        return {}
    result: dict[str, SiteRules] = {}
    for path in sorted(sites_dir.glob("*.yaml")):
        rules = _validate(SiteRules, _load_yaml(path), path)
        result[_strip_www(rules.domain)] = rules
    return result


def rules_for(url: str, site_rules: dict[str, SiteRules]) -> SiteRules | None:
    return site_rules.get(hostname(url))
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py -q`
Expected: `16 passed`.

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml src clients/example.yaml settings.example.yaml .env.example sites/.gitkeep tests/test_config.py
git commit -m "feat: project skeleton, models and config loading"
```

---

### Task 2: Price parsing

**Files:**
- Create: `src/pricemon/extract/price.py`
- Test: `tests/test_price.py`

**Interfaces:**
- Consumes: nothing.
- Produces (`pricemon.extract.price`): `PriceError(ValueError)`; `parse_price(text: str | None, decimal_separator: str | None = None) -> Decimal` for human-formatted text; `parse_structured_price(value) -> Decimal` for values from JSON-LD and meta tags. Both raise `PriceError` on zero, negative or unparsable input.

Note: `src/pricemon/extract/__init__.py` does not exist yet. Create it as an empty file in this task so the package imports; Task 3 fills it.

- [ ] **Step 1: Write the failing tests**

`tests/test_price.py`:

```python
from decimal import Decimal

import pytest

from pricemon.extract.price import PriceError, parse_price, parse_structured_price


@pytest.mark.parametrize(
    "text, expected",
    [
        ("129.00", "129.00"),
        ("1.299,00 €", "1299.00"),
        ("$1,299.00", "1299.00"),
        ("1 299", "1299"),
        ("1 299,50 kr", "1299.50"),
        ("1.299", "1299"),
        ("12.50", "12.50"),
        ("12,5", "12.5"),
        ("1,299,000", "1299000"),
        ("€ 19,99", "19.99"),
        ("From 12,50 € to 15,00 €", "12.50"),
        ("Price: 45.", "45"),
    ],
)
def test_parse_price(text, expected):
    assert parse_price(text) == Decimal(expected)


def test_decimal_separator_override():
    assert parse_price("1.299", decimal_separator=".") == Decimal("1.299")
    assert parse_price("1,299", decimal_separator=",") == Decimal("1.299")
    assert parse_price("1.299,50", decimal_separator=",") == Decimal("1299.50")


@pytest.mark.parametrize("text", ["", None, "free", "Call us", "0", "0,00", "-5.00"])
def test_parse_price_rejects(text):
    with pytest.raises(PriceError):
        parse_price(text)


@pytest.mark.parametrize(
    "value, expected",
    [
        (129, "129"),
        (129.5, "129.5"),
        ("129.000", "129.000"),
        (" 24.99 ", "24.99"),
        ("1.299,00", "1299.00"),
    ],
)
def test_parse_structured_price(value, expected):
    assert parse_structured_price(value) == Decimal(expected)


@pytest.mark.parametrize("value", [0, "0.00", None, True, "", "n/a", [], -3])
def test_parse_structured_price_rejects(value):
    with pytest.raises(PriceError):
        parse_structured_price(value)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_price.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'pricemon.extract'`.

- [ ] **Step 3: Write the implementation**

Create `src/pricemon/extract/__init__.py` as an empty file.

`src/pricemon/extract/price.py`:

```python
"""Turning price text into a Decimal."""

import re
from decimal import Decimal, InvalidOperation

_NUMBER = re.compile(r"\d[\d.,\s]*")
_PLAIN = re.compile(r"\d+(\.\d+)?")


class PriceError(ValueError):
    """The text does not contain a usable price."""


def _normalise(cleaned: str, decimal_separator: str | None) -> str:
    """Rewrite a digits-and-separators string so '.' is the decimal point."""
    if decimal_separator in (".", ","):
        thousands = "," if decimal_separator == "." else "."
        return cleaned.replace(thousands, "").replace(decimal_separator, ".")
    dots, commas = cleaned.count("."), cleaned.count(",")
    if dots and commas:
        decimal = "." if cleaned.rfind(".") > cleaned.rfind(",") else ","
        thousands = "," if decimal == "." else "."
        return cleaned.replace(thousands, "").replace(decimal, ".")
    if dots or commas:
        separator = "." if dots else ","
        if cleaned.count(separator) > 1:
            return cleaned.replace(separator, "")
        digits_after = len(cleaned.split(separator)[1])
        if digits_after == 3:
            return cleaned.replace(separator, "")
        return cleaned.replace(separator, ".")
    return cleaned


def parse_price(text: str | None, decimal_separator: str | None = None) -> Decimal:
    """Parse human-formatted price text such as '1.299,00 €' or '$1,299.00'.

    The first number in the text is used.
    """
    text = text or ""
    match = _NUMBER.search(text)
    if match is None:
        raise PriceError(f"no number in {text!r}")
    if match.start() > 0 and text[match.start() - 1] in "-−":
        raise PriceError(f"negative price in {text!r}")
    cleaned = re.sub(r"\s", "", match.group()).rstrip(".,")
    try:
        value = Decimal(_normalise(cleaned, decimal_separator))
    except InvalidOperation:
        raise PriceError(f"cannot parse price {text!r}") from None
    if value <= 0:
        raise PriceError(f"price is not positive in {text!r}")
    return value


def parse_structured_price(value) -> Decimal:
    """Parse a price from structured data, where '.' is normally the decimal point."""
    if isinstance(value, bool):
        raise PriceError(f"not a price: {value!r}")
    if isinstance(value, (int, float)):
        result = Decimal(str(value))
    elif isinstance(value, str):
        stripped = value.strip()
        if _PLAIN.fullmatch(stripped):
            result = Decimal(stripped)
        else:
            return parse_price(stripped)
    else:
        raise PriceError(f"not a price: {value!r}")
    if result <= 0:
        raise PriceError(f"price is not positive: {value!r}")
    return result
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_price.py -q`
Expected: `33 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/pricemon/extract tests/test_price.py
git commit -m "feat: price text parsing"
```

---

### Task 3: Extraction chain

**Files:**
- Create: `src/pricemon/extract/common.py`, `src/pricemon/extract/jsonld.py`, `src/pricemon/extract/meta.py`, `src/pricemon/extract/rules.py`
- Modify: `src/pricemon/extract/__init__.py` (currently empty)
- Create: `tests/fixtures/jsonld_single.html`, `jsonld_offer_list.html`, `jsonld_aggregate.html`, `jsonld_graph.html`, `jsonld_no_stock.html`, `meta_tags.html`, `rules_only.html`, `no_price.html`
- Test: `tests/test_extract.py`

**Interfaces:**
- Consumes: `Partial`, `Extraction` from `pricemon.models`; `SiteRules`, `Selector`, `StockRule` from `pricemon.config`; `parse_price`, `parse_structured_price`, `PriceError` from `pricemon.extract.price`.
- Produces (`pricemon.extract`): `ExtractError(Exception)`; `extract(html: str, url: str, rules: SiteRules | None) -> Extraction`. Raises `ExtractError` when no step finds a price.

- [ ] **Step 1: Create the fixtures**

`tests/fixtures/jsonld_single.html`:

```html
<!doctype html>
<html><head><title>Bosch GSR 12V drill | Shop A</title>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "Product", "name": "Bosch GSR 12V drill",
 "offers": {"@type": "Offer", "price": "119.00", "priceCurrency": "EUR",
            "availability": "https://schema.org/InStock"}}
</script>
</head><body><h1>Bosch GSR 12V drill</h1></body></html>
```

`tests/fixtures/jsonld_offer_list.html`:

```html
<!doctype html>
<html><head><title>Paint roller</title>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "http://schema.org/Product", "name": "Paint roller",
 "offers": [
   {"@type": "Offer", "availability": "http://schema.org/PreOrder"},
   {"@type": "Offer", "priceSpecification": [{"price": "89.50", "priceCurrency": "usd"}],
    "availability": "http://schema.org/OutOfStock"},
   {"@type": "Offer", "price": "95.00", "priceCurrency": "USD"}
 ]}
</script>
</head><body></body></html>
```

`tests/fixtures/jsonld_aggregate.html`:

```html
<!doctype html>
<html><head><title>Kettle</title>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": ["Product"], "name": "Kettle",
 "offers": {"@type": "AggregateOffer", "lowPrice": "24.99", "highPrice": "39.99",
            "priceCurrency": "GBP"}}
</script>
</head><body></body></html>
```

`tests/fixtures/jsonld_graph.html`:

```html
<!doctype html>
<html><head><title>Lamp</title>
<script type="application/ld+json">{"this is": not valid json</script>
<script type="application/ld+json">
{"@context": "https://schema.org", "@graph": [
  {"@type": "WebSite", "name": "Shop"},
  {"@type": "Product", "name": "Lamp",
   "offers": [{"@type": "Offer", "price": 15, "priceCurrency": "EUR", "availability": "InStock"}]}
]}
</script>
</head><body></body></html>
```

`tests/fixtures/jsonld_no_stock.html`:

```html
<!doctype html>
<html><head><title>Garden hose</title>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "Product",
 "offers": {"@type": "Offer", "price": "50.00", "priceCurrency": "EUR"}}
</script>
</head><body>
<h1 class="product-title">Garden hose</h1>
<span class="price">55,00 €</span>
<div class="availability">In stock</div>
</body></html>
```

`tests/fixtures/meta_tags.html`:

```html
<!doctype html>
<html><head><title>Desk lamp</title>
<meta property="og:title" content="Desk lamp">
<meta property="product:price:amount" content="34.90">
<meta property="product:price:currency" content="EUR">
<meta property="product:availability" content="out of stock">
</head><body></body></html>
```

`tests/fixtures/rules_only.html`:

```html
<!doctype html>
<html><head><title>Cordless saw</title></head><body>
<h1 class="product-title"> Cordless saw </h1>
<span class="price" data-amount="1299.00">1.299,00 €</span>
<div class="availability">Sold out</div>
</body></html>
```

`tests/fixtures/no_price.html`:

```html
<!doctype html>
<html><head><title>About us</title></head><body><h1>About us</h1></body></html>
```

- [ ] **Step 2: Write the failing tests**

`tests/test_extract.py`:

```python
from decimal import Decimal
from pathlib import Path

import pytest

from pricemon.config import Selector, SiteRules, StockRule
from pricemon.extract import ExtractError, extract

FIXTURES = Path(__file__).parent / "fixtures"
URL = "https://shop.example/p/1"

RULES = SiteRules(
    domain="shop.example",
    title=Selector(css="h1.product-title"),
    price=Selector(css="span.price"),
    currency="EUR",
    stock=StockRule(css=".availability", in_stock_text=["In stock"], out_of_stock_text=["Sold out"]),
)


def load(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_jsonld_single_offer():
    ex = extract(load("jsonld_single.html"), URL, None)
    assert ex.title == "Bosch GSR 12V drill"
    assert ex.price == Decimal("119.00")
    assert ex.currency == "EUR"
    assert ex.in_stock is True
    assert ex.method == "jsonld"
    assert ex.sources == {"price": "jsonld", "title": "jsonld", "currency": "jsonld", "in_stock": "jsonld"}


def test_jsonld_offer_list_uses_first_offer_with_a_price():
    ex = extract(load("jsonld_offer_list.html"), URL, None)
    assert ex.price == Decimal("89.50")
    assert ex.currency == "USD"
    assert ex.in_stock is False
    assert ex.method == "jsonld"


def test_jsonld_aggregate_offer_uses_low_price():
    ex = extract(load("jsonld_aggregate.html"), URL, None)
    assert ex.price == Decimal("24.99")
    assert ex.currency == "GBP"
    assert ex.in_stock is None
    assert "in_stock" not in ex.sources


def test_jsonld_graph_and_broken_block_are_handled():
    ex = extract(load("jsonld_graph.html"), URL, None)
    assert ex.title == "Lamp"
    assert ex.price == Decimal("15")
    assert ex.in_stock is True


def test_meta_tags():
    ex = extract(load("meta_tags.html"), URL, None)
    assert ex.title == "Desk lamp"
    assert ex.price == Decimal("34.90")
    assert ex.currency == "EUR"
    assert ex.in_stock is False
    assert ex.method == "meta"


def test_site_rules():
    ex = extract(load("rules_only.html"), URL, RULES)
    assert ex.title == "Cordless saw"
    assert ex.price == Decimal("1299.00")
    assert ex.currency == "EUR"
    assert ex.in_stock is False
    assert ex.method == "rules"


def test_site_rules_can_read_an_attribute():
    rules = RULES.model_copy(update={"price": Selector(css="span.price", attr="data-amount")})
    ex = extract(load("rules_only.html"), URL, rules)
    assert ex.price == Decimal("1299.00")


def test_missing_fields_are_filled_from_later_steps():
    ex = extract(load("jsonld_no_stock.html"), URL, RULES)
    assert ex.price == Decimal("50.00")
    assert ex.method == "jsonld"
    assert ex.title == "Garden hose"
    assert ex.in_stock is True
    assert ex.sources == {"price": "jsonld", "currency": "jsonld", "title": "rules", "in_stock": "rules"}


def test_skip_structured_data_uses_rules_only():
    rules = RULES.model_copy(update={"skip_structured_data": True})
    ex = extract(load("jsonld_no_stock.html"), URL, rules)
    assert ex.price == Decimal("55.00")
    assert ex.method == "rules"


def test_decimal_separator_rule_is_applied():
    html = '<html><body><span class="price">1.299</span></body></html>'
    rules = SiteRules(domain="shop.example", price=Selector(css="span.price"), decimal_separator=".")
    assert extract(html, URL, rules).price == Decimal("1.299")


def test_currency_can_come_from_a_selector():
    html = '<html><body><span class="price">12,50</span><meta itemprop="cur" content="sek"></body></html>'
    rules = SiteRules(
        domain="shop.example",
        price=Selector(css="span.price"),
        currency=Selector(css='meta[itemprop="cur"]', attr="content"),
    )
    assert extract(html, URL, rules).currency == "SEK"


def test_stock_text_matching_prefers_out_of_stock():
    html = '<html><body><span class="price">10</span><div class="availability">Not in stock</div></body></html>'
    rules = RULES.model_copy(
        update={"stock": StockRule(css=".availability", in_stock_text=["in stock"], out_of_stock_text=["not in stock"])}
    )
    assert extract(html, URL, rules).in_stock is False


def test_no_price_anywhere_raises():
    with pytest.raises(ExtractError, match="no price found"):
        extract(load("no_price.html"), URL, None)


def test_unparsable_rules_price_raises():
    html = '<html><body><span class="price">Call us</span></body></html>'
    with pytest.raises(ExtractError, match="jsonld, meta, rules"):
        extract(html, URL, RULES)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_extract.py -q`
Expected: collection error, `ImportError: cannot import name 'ExtractError' from 'pricemon.extract'`.

- [ ] **Step 4: Write `src/pricemon/extract/common.py`**

```python
"""Normalisation shared by the extraction steps."""

import re

_IN_STOCK = {"instock", "limitedavailability", "onlineonly", "instoreonly"}
_OUT_OF_STOCK = {"outofstock", "soldout", "discontinued", "oos"}


def normalize_currency(value) -> str | None:
    """A three-letter currency code in upper case, or None."""
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    return code if len(code) == 3 and code.isalpha() else None


def parse_availability(value) -> bool | None:
    """Map a schema.org availability value to True, False or None (unknown).

    Accepts 'https://schema.org/InStock', 'InStock', 'out of stock' and similar.
    """
    if not isinstance(value, str):
        return None
    key = re.sub(r"[\s_\-]", "", value.rsplit("/", 1)[-1]).lower()
    if key in _IN_STOCK:
        return True
    if key in _OUT_OF_STOCK:
        return False
    return None
```

- [ ] **Step 5: Write `src/pricemon/extract/jsonld.py`**

```python
"""Step 1: schema.org Product data in JSON-LD blocks."""

import json

from selectolax.parser import HTMLParser

from ..models import Partial
from .common import normalize_currency, parse_availability
from .price import PriceError, parse_structured_price


def _nodes(data):
    """Yield every dict in a JSON-LD document, descending into lists and @graph."""
    if isinstance(data, list):
        for item in data:
            yield from _nodes(item)
    elif isinstance(data, dict):
        yield data
        yield from _nodes(data.get("@graph", []))


def _is_product(node: dict) -> bool:
    types = node.get("@type")
    types = types if isinstance(types, list) else [types]
    return any(str(item).rsplit("/", 1)[-1] == "Product" for item in types)


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _offer_price(offer: dict):
    """(raw price, raw currency) for one offer, or (None, None).

    Looks at the offer itself, then at its priceSpecification entries.
    An AggregateOffer has no 'price', so its 'lowPrice' is used.
    """
    candidates = [offer] + [s for s in _as_list(offer.get("priceSpecification")) if isinstance(s, dict)]
    for candidate in candidates:
        raw = candidate.get("price")
        if raw is None:
            raw = candidate.get("lowPrice")
        if raw is not None:
            return raw, candidate.get("priceCurrency") or offer.get("priceCurrency")
    return None, None


def _from_product(node: dict) -> Partial:
    name = node.get("name")
    partial = Partial(title=name.strip() or None if isinstance(name, str) else None)
    for offer in _as_list(node.get("offers")):
        if not isinstance(offer, dict):
            continue
        raw, currency = _offer_price(offer)
        if raw is None:
            continue
        try:
            partial.price = parse_structured_price(raw)
        except PriceError:
            continue
        partial.currency = normalize_currency(currency)
        partial.in_stock = parse_availability(offer.get("availability"))
        break
    return partial


def extract_jsonld(tree: HTMLParser) -> Partial:
    """The first Product with a usable price; otherwise whatever title was found."""
    fallback = Partial()
    for script in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.text(), strict=False)
        except (ValueError, TypeError):
            continue
        for node in _nodes(data):
            if not _is_product(node):
                continue
            partial = _from_product(node)
            if partial.price is not None:
                return partial
            if fallback.title is None:
                fallback = partial
    return fallback
```

- [ ] **Step 6: Write `src/pricemon/extract/meta.py`**

```python
"""Step 2: Open Graph style product meta tags."""

from selectolax.parser import HTMLParser

from ..models import Partial
from .common import normalize_currency, parse_availability
from .price import PriceError, parse_structured_price


def _content(tree: HTMLParser, *names: str) -> str | None:
    for name in names:
        for attribute in ("property", "name"):
            node = tree.css_first(f'meta[{attribute}="{name}"]')
            if node is not None:
                content = (node.attributes.get("content") or "").strip()
                if content:
                    return content
    return None


def extract_meta(tree: HTMLParser) -> Partial:
    partial = Partial()
    raw = _content(tree, "product:price:amount", "og:price:amount")
    if raw:
        try:
            partial.price = parse_structured_price(raw)
        except PriceError:
            pass
    partial.currency = normalize_currency(_content(tree, "product:price:currency", "og:price:currency"))
    partial.title = _content(tree, "og:title")
    partial.in_stock = parse_availability(_content(tree, "product:availability", "og:availability"))
    return partial
```

- [ ] **Step 7: Write `src/pricemon/extract/rules.py`**

```python
"""Step 3: per-site CSS rules from sites/<domain>.yaml."""

from selectolax.parser import HTMLParser

from ..config import Selector, SiteRules
from ..models import Partial
from .common import normalize_currency
from .price import PriceError, parse_price


def _select(tree: HTMLParser, selector: Selector) -> str | None:
    node = tree.css_first(selector.css)
    if node is None:
        return None
    value = node.attributes.get(selector.attr) if selector.attr else node.text(strip=True)
    value = (value or "").strip()
    return value or None


def extract_rules(tree: HTMLParser, rules: SiteRules) -> Partial:
    partial = Partial()
    if rules.title:
        partial.title = _select(tree, rules.title)
    if rules.price:
        raw = _select(tree, rules.price)
        if raw:
            try:
                partial.price = parse_price(raw, rules.decimal_separator)
            except PriceError:
                pass
    if isinstance(rules.currency, str):
        partial.currency = normalize_currency(rules.currency)
    elif rules.currency is not None:
        partial.currency = normalize_currency(_select(tree, rules.currency))
    if rules.stock:
        node = tree.css_first(rules.stock.css)
        if node is not None:
            text = node.text(strip=True).lower()
            # Out-of-stock phrases are checked first: "not in stock" contains "in stock".
            if any(phrase.lower() in text for phrase in rules.stock.out_of_stock_text):
                partial.in_stock = False
            elif any(phrase.lower() in text for phrase in rules.stock.in_stock_text):
                partial.in_stock = True
    return partial
```

- [ ] **Step 8: Write `src/pricemon/extract/__init__.py`**

```python
"""The extraction chain: JSON-LD, then meta tags, then site rules."""

from selectolax.parser import HTMLParser

from ..config import SiteRules
from ..models import Extraction, Partial
from .jsonld import extract_jsonld
from .meta import extract_meta
from .rules import extract_rules

_FILLABLE = ("title", "currency", "in_stock")


class ExtractError(Exception):
    """No step found a price on the page."""


def extract(html: str, url: str, rules: SiteRules | None) -> Extraction:
    """Extract title, price, currency and stock from a product page.

    The first step that yields a price wins. Fields it is missing are filled
    from the steps after it. `url` is accepted for error context and future use.
    """
    tree = HTMLParser(html)
    steps: list[tuple[str, Partial]] = []
    if not (rules and rules.skip_structured_data):
        steps.append(("jsonld", extract_jsonld(tree)))
        steps.append(("meta", extract_meta(tree)))
    if rules:
        steps.append(("rules", extract_rules(tree, rules)))

    winner_index = next((i for i, (_, partial) in enumerate(steps) if partial.price is not None), None)
    if winner_index is None:
        tried = ", ".join(name for name, _ in steps) or "nothing"
        raise ExtractError(f"no price found (tried: {tried})")

    method, winner = steps[winner_index]
    values = {name: getattr(winner, name) for name in _FILLABLE}
    sources = {"price": method}
    sources.update({name: method for name, value in values.items() if value is not None})
    for later_method, later in steps[winner_index + 1 :]:
        for name in _FILLABLE:
            if values[name] is None and getattr(later, name) is not None:
                values[name] = getattr(later, name)
                sources[name] = later_method

    return Extraction(
        title=values["title"],
        price=winner.price,
        currency=values["currency"],
        in_stock=values["in_stock"],
        method=method,
        sources=sources,
    )
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_extract.py -q`
Expected: `14 passed`.

- [ ] **Step 10: Run the whole suite and commit**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: `63 passed`.

```bash
git add src/pricemon/extract tests/fixtures tests/test_extract.py
git commit -m "feat: extraction chain (JSON-LD, meta tags, site rules)"
```

---

### Task 4: HTTP fetcher

**Files:**
- Create: `src/pricemon/fetch.py`
- Test: `tests/test_fetch.py`

**Interfaces:**
- Consumes: `Settings`, `SiteRules`, `hostname` from `pricemon.config`; `FetchResult` and status constants from `pricemon.models`.
- Produces (`pricemon.fetch`): `looks_like_challenge(html: str) -> bool`; `classify(status: int, html: str) -> FetchResult | None` (None means "may be retried"); `Fetcher(settings: Settings, client: httpx.Client | None = None, clock=time.monotonic, sleep=time.sleep)` with `fetch(url: str, rules: SiteRules | None = None) -> FetchResult`. A `Fetcher` instance is used for one run: it caches `robots.txt` per host and remembers the last request time per host.

The browser fetcher (`fetcher: browser`) is added in Task 10. In this task `fetch` always uses HTTP.

- [ ] **Step 1: Write the failing tests**

`tests/test_fetch.py`:

```python
import httpx
import pytest

from pricemon.config import Settings, SiteRules
from pricemon.fetch import Fetcher, looks_like_challenge
from pricemon.models import BLOCKED, DISALLOWED, FETCH_ERROR, NOT_FOUND, OK

PAGE = "<html><head><title>Drill</title></head><body>ok</body></html>"
CHALLENGE = "<html><head><title>Just a moment...</title></head><body></body></html>"


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def make(handler, **overrides):
    """A Fetcher on a mock transport and a fake clock. Robots checks are off unless enabled."""
    clock = FakeClock()
    settings = Settings(**{"respect_robots_txt": False, **overrides})
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return Fetcher(settings, client=client, clock=clock.now, sleep=clock.sleep), clock


def counting(responses):
    """A handler that returns the given (status, body) pairs in order, repeating the last."""
    calls = []

    def handler(request):
        calls.append(request.url.path)
        status, body = responses[min(len(calls), len(responses)) - 1]
        return httpx.Response(status, text=body)

    return handler, calls


def test_ok_page():
    handler, calls = counting([(200, PAGE)])
    fetcher, _ = make(handler)
    result = fetcher.fetch("https://a.example/p/1")
    assert (result.status, result.http_status, result.html, result.error) == (OK, 200, PAGE, None)
    assert calls == ["/p/1"]


@pytest.mark.parametrize("status", [404, 410])
def test_missing_page_is_not_found(status):
    handler, calls = counting([(status, "")])
    fetcher, _ = make(handler)
    result = fetcher.fetch("https://a.example/p/1")
    assert (result.status, result.http_status) == (NOT_FOUND, status)
    assert len(calls) == 1


@pytest.mark.parametrize("status", [403, 429])
def test_refused_request_is_blocked_and_not_retried(status):
    handler, calls = counting([(status, "")])
    fetcher, _ = make(handler)
    result = fetcher.fetch("https://a.example/p/1")
    assert (result.status, result.http_status, result.error) == (BLOCKED, status, f"HTTP {status}")
    assert len(calls) == 1


def test_other_client_error_is_fetch_error_and_not_retried():
    handler, calls = counting([(400, "")])
    fetcher, _ = make(handler)
    result = fetcher.fetch("https://a.example/p/1")
    assert (result.status, result.error) == (FETCH_ERROR, "HTTP 400")
    assert len(calls) == 1


def test_server_errors_are_retried_with_doubling_delay():
    handler, calls = counting([(500, ""), (502, ""), (200, PAGE)])
    fetcher, clock = make(handler, min_interval_seconds=0)
    result = fetcher.fetch("https://a.example/p/1")
    assert result.status == OK
    assert len(calls) == 3
    assert clock.sleeps == [2.0, 4.0]


def test_server_error_after_all_retries_is_fetch_error():
    handler, calls = counting([(500, "")])
    fetcher, _ = make(handler, min_interval_seconds=0)
    result = fetcher.fetch("https://a.example/p/1")
    assert (result.status, result.http_status, result.error) == (FETCH_ERROR, 500, "HTTP 500")
    assert len(calls) == 3


def test_network_error_is_retried_then_fetch_error():
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("boom", request=request)

    fetcher, _ = make(handler, min_interval_seconds=0, retries=1)
    result = fetcher.fetch("https://a.example/p/1")
    assert result.status == FETCH_ERROR
    assert "ConnectError" in result.error
    assert len(calls) == 2


@pytest.mark.parametrize("status", [200, 503])
def test_challenge_page_is_blocked_and_not_retried(status):
    handler, calls = counting([(status, CHALLENGE)])
    fetcher, _ = make(handler, min_interval_seconds=0)
    result = fetcher.fetch("https://a.example/p/1")
    assert (result.status, result.error) == (BLOCKED, "challenge page")
    assert len(calls) == 1


def test_challenge_detection_ignores_ordinary_captcha_widgets():
    page = '<html><head><title>Drill</title></head><body><div class="g-recaptcha"></div></body></html>'
    assert looks_like_challenge(page) is False
    assert looks_like_challenge(CHALLENGE) is True
    assert looks_like_challenge('<html><body><script src="https://geo.captcha-delivery.com/x"></script></body></html>') is True


def test_requests_to_one_host_are_spaced_out():
    handler, _ = counting([(200, PAGE)])
    fetcher, clock = make(handler, min_interval_seconds=5)
    fetcher.fetch("https://a.example/p/1")
    fetcher.fetch("https://www.a.example/p/2")
    assert clock.sleeps == [5.0]


def test_different_hosts_do_not_wait_for_each_other():
    handler, _ = counting([(200, PAGE)])
    fetcher, clock = make(handler, min_interval_seconds=5)
    fetcher.fetch("https://a.example/p/1")
    fetcher.fetch("https://b.example/p/1")
    assert clock.sleeps == []


def test_site_rules_can_override_the_interval():
    handler, _ = counting([(200, PAGE)])
    fetcher, clock = make(handler, min_interval_seconds=5)
    rules = SiteRules(domain="a.example", min_interval_seconds=1)
    fetcher.fetch("https://a.example/p/1", rules)
    fetcher.fetch("https://a.example/p/2", rules)
    assert clock.sleeps == [1.0]


def robots_site(robots_status, robots_body):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(robots_status, text=robots_body)
        return httpx.Response(200, text=PAGE)

    return handler, calls


def test_robots_disallow_is_respected_and_fetched_once():
    handler, calls = robots_site(200, "User-agent: *\nDisallow: /private/\n")
    fetcher, _ = make(handler, respect_robots_txt=True, min_interval_seconds=0)
    blocked = fetcher.fetch("https://a.example/private/x")
    allowed = fetcher.fetch("https://a.example/p/1")
    assert (blocked.status, blocked.error) == (DISALLOWED, "disallowed by robots.txt")
    assert allowed.status == OK
    assert calls == ["/robots.txt", "/p/1"]


def test_missing_robots_file_allows_everything():
    handler, calls = robots_site(404, "")
    fetcher, _ = make(handler, respect_robots_txt=True, min_interval_seconds=0)
    assert fetcher.fetch("https://a.example/private/x").status == OK
    assert calls == ["/robots.txt", "/private/x"]


def test_robots_is_ignored_when_disabled():
    handler, calls = robots_site(200, "User-agent: *\nDisallow: /\n")
    fetcher, _ = make(handler, respect_robots_txt=False)
    assert fetcher.fetch("https://a.example/p/1").status == OK
    assert calls == ["/p/1"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_fetch.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'pricemon.fetch'`.

- [ ] **Step 3: Write `src/pricemon/fetch.py`**

```python
"""Polite page fetching: rate limit, retries, robots.txt and block detection.

Bot protection is never bypassed. A refused or challenged request is reported
as `blocked` and left alone.
"""

import re
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .config import Settings, SiteRules, hostname
from .models import BLOCKED, DISALLOWED, FETCH_ERROR, NOT_FOUND, OK, FetchResult

# Page titles used by bot-protection interstitials.
_CHALLENGE_TITLES = (
    "just a moment",
    "attention required",
    "access denied",
    "pardon our interruption",
    "robot check",
    "are you a robot",
)
# Markers that only appear on block pages, not on ordinary pages with a CAPTCHA widget.
_CHALLENGE_MARKERS = ("captcha-delivery.com", "px-captcha")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_FIRST_RETRY_DELAY = 2.0


def looks_like_challenge(html: str) -> bool:
    """True when the page is a bot-protection challenge instead of real content."""
    match = _TITLE.search(html)
    title = match.group(1).strip().lower() if match else ""
    if any(title.startswith(prefix) for prefix in _CHALLENGE_TITLES):
        return True
    lowered = html.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


def classify(status: int, html: str) -> FetchResult | None:
    """Turn a response into a result. None means the request may be retried."""
    if looks_like_challenge(html):
        return FetchResult(BLOCKED, status, error="challenge page")
    if status in (403, 429):
        return FetchResult(BLOCKED, status, error=f"HTTP {status}")
    if status in (404, 410):
        return FetchResult(NOT_FOUND, status, error=f"HTTP {status}")
    if status >= 500:
        return None
    if status >= 400:
        return FetchResult(FETCH_ERROR, status, error=f"HTTP {status}")
    return FetchResult(OK, status, html)


class Fetcher:
    """Fetches pages for one run."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None, clock=time.monotonic, sleep=time.sleep):
        self.settings = settings
        self.client = client or httpx.Client(
            headers={"User-Agent": settings.user_agent},
            timeout=settings.timeout_seconds,
            follow_redirects=True,
        )
        self._clock = clock
        self._sleep = sleep
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}

    def fetch(self, url: str, rules: SiteRules | None = None) -> FetchResult:
        host = hostname(url)
        interval = self.settings.min_interval_seconds
        if rules is not None and rules.min_interval_seconds is not None:
            interval = rules.min_interval_seconds
        if self.settings.respect_robots_txt and not self._allowed(url, host, interval):
            return FetchResult(DISALLOWED, error="disallowed by robots.txt")
        return self._fetch_http(url, host, interval)

    def _wait(self, host: str, interval: float) -> None:
        """Sleep until `interval` seconds have passed since the last request to this host."""
        last = self._last_request.get(host)
        if last is not None:
            remaining = interval - (self._clock() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request[host] = self._clock()

    def _allowed(self, url: str, host: str, interval: float) -> bool:
        if host not in self._robots:
            self._robots[host] = self._load_robots(url, host, interval)
        parser = self._robots[host]
        return True if parser is None else parser.can_fetch(self.settings.user_agent, url)

    def _load_robots(self, url: str, host: str, interval: float) -> RobotFileParser | None:
        """The parsed robots.txt for a host, or None when there is none to obey."""
        parts = urlsplit(url)
        self._wait(host, interval)
        try:
            response = self.client.get(f"{parts.scheme}://{parts.netloc}/robots.txt")
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        return parser

    def _fetch_http(self, url: str, host: str, interval: float) -> FetchResult:
        attempts = self.settings.retries + 1
        delay = _FIRST_RETRY_DELAY
        error = "no attempt made"
        http_status = None
        for attempt in range(attempts):
            self._wait(host, interval)
            try:
                response = self.client.get(url)
            except httpx.HTTPError as exc:
                error = f"{type(exc).__name__}: {exc}"
                http_status = None
            else:
                result = classify(response.status_code, response.text)
                if result is not None:
                    return result
                error = f"HTTP {response.status_code}"
                http_status = response.status_code
            if attempt < attempts - 1:
                self._sleep(delay)
                delay *= 2
        return FetchResult(FETCH_ERROR, http_status, error=error)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_fetch.py -q`
Expected: `18 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/pricemon/fetch.py tests/test_fetch.py
git commit -m "feat: HTTP fetcher with rate limit, retries, robots and block detection"
```

---

### Task 5: SQLite store

**Files:**
- Create: `src/pricemon/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `Observation`, `Event`, `OK` from `pricemon.models`.
- Produces (`pricemon.store`): class `Store(path)` with
  - `close() -> None`
  - `start_run(started_at: str, dry_run: bool = False) -> int`
  - `finish_run(run_id: int, finished_at: str, urls_total: int, urls_ok: int, urls_failed: int) -> None`
  - `run(run_id: int) -> dict` and `runs() -> list[dict]` (keys: `id`, `started_at`, `finished_at`, `urls_total`, `urls_ok`, `urls_failed`, `dry_run`), oldest first
  - `add_observation(run_id: int, obs: Observation) -> Observation` (returns a copy with `id` set)
  - `last_ok(url: str, before_run_id: int | None = None) -> Observation | None`
  - `consecutive_failures(url: str, up_to_run_id: int | None = None) -> int`
  - `add_event(run_id: int, observation_id: int, event: Event) -> None`
  - `run_observations(run_id: int) -> list[Observation]`
  - `run_events(run_id: int) -> list[Event]` (each event's `currency` comes from its observation)

- [ ] **Step 1: Write the failing tests**

`tests/test_store.py`:

```python
from decimal import Decimal

import pytest

from pricemon.models import FETCH_ERROR, OK, Event, Observation
from pricemon.store import Store

A = "https://a.example/p/1"
B = "https://b.example/p/1"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "acme" / "pricemon.db")
    yield s
    s.close()


def ok(url, price, in_stock=True, currency="EUR"):
    return Observation(
        url=url, product_name="Drill", fetched_at="2026-11-10T07:00:00+00:00", status=OK,
        http_status=200, title="Drill", price=Decimal(price), currency=currency,
        in_stock=in_stock, method="jsonld",
    )


def failed(url, error="HTTP 500"):
    return Observation(
        url=url, product_name="Drill", fetched_at="2026-11-10T07:00:00+00:00",
        status=FETCH_ERROR, http_status=500, error=error,
    )


def test_store_creates_its_folder_and_tracks_runs(store):
    run_id = store.start_run("2026-11-10T07:00:00+00:00", dry_run=True)
    store.finish_run(run_id, "2026-11-10T07:01:00+00:00", urls_total=3, urls_ok=2, urls_failed=1)
    run = store.run(run_id)
    assert run["started_at"] == "2026-11-10T07:00:00+00:00"
    assert run["finished_at"] == "2026-11-10T07:01:00+00:00"
    assert (run["urls_total"], run["urls_ok"], run["urls_failed"], run["dry_run"]) == (3, 2, 1, 1)
    second = store.start_run("2026-11-11T07:00:00+00:00")
    assert [r["id"] for r in store.runs()] == [run_id, second]
    assert store.run(second)["dry_run"] == 0


def test_observation_round_trip_keeps_types(store):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")
    saved = store.add_observation(run_id, ok(A, "119.90", in_stock=False))
    unknown = store.add_observation(run_id, ok(B, "5", in_stock=None, currency=None))
    assert saved.id is not None and unknown.id != saved.id
    rows = store.run_observations(run_id)
    assert rows[0] == saved
    assert rows[0].price == Decimal("119.90") and str(rows[0].price) == "119.90"
    assert rows[0].in_stock is False
    assert rows[1].in_stock is None and rows[1].currency is None


def test_failed_observation_round_trip(store):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")
    saved = store.add_observation(run_id, failed(A))
    row = store.run_observations(run_id)[0]
    assert row == saved
    assert (row.status, row.price, row.error) == (FETCH_ERROR, None, "HTTP 500")


def test_last_ok_skips_failures_and_respects_before_run_id(store):
    assert store.last_ok(A) is None
    run1 = store.start_run("2026-11-10T07:00:00+00:00")
    store.add_observation(run1, ok(A, "119"))
    run2 = store.start_run("2026-11-11T07:00:00+00:00")
    store.add_observation(run2, failed(A))
    run3 = store.start_run("2026-11-12T07:00:00+00:00")
    store.add_observation(run3, ok(A, "109"))
    assert store.last_ok(A).price == Decimal("109")
    assert store.last_ok(A, before_run_id=run3).price == Decimal("119")
    assert store.last_ok(A, before_run_id=run1) is None
    assert store.last_ok(B) is None


def test_consecutive_failures_counts_back_to_the_last_ok(store):
    run1 = store.start_run("2026-11-10T07:00:00+00:00")
    store.add_observation(run1, ok(A, "119"))
    assert store.consecutive_failures(A) == 0
    run2 = store.start_run("2026-11-11T07:00:00+00:00")
    store.add_observation(run2, failed(A))
    run3 = store.start_run("2026-11-12T07:00:00+00:00")
    store.add_observation(run3, failed(A))
    assert store.consecutive_failures(A) == 2
    assert store.consecutive_failures(A, up_to_run_id=run2) == 1
    assert store.consecutive_failures(A, up_to_run_id=run1) == 0
    assert store.consecutive_failures(B) == 0


def test_event_round_trip_takes_currency_from_the_observation(store):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")
    obs = store.add_observation(run_id, ok(A, "109"))
    event = Event(
        url=A, product_name="Drill", type="price_down", old_price=Decimal("119"),
        new_price=Decimal("109"), change_pct=Decimal("-8.40"), verify=False, alerted=True,
        currency="EUR",
    )
    stock = Event(
        url=A, product_name="Drill", type="out_of_stock", old_price=Decimal("109"),
        new_price=Decimal("109"), change_pct=None, verify=False, alerted=False, currency="EUR",
    )
    store.add_event(run_id, obs.id, event)
    store.add_event(run_id, obs.id, stock)
    assert store.run_events(run_id) == [event, stock]
    assert store.run_events(run_id + 1) == []


def test_data_survives_reopening(tmp_path):
    path = tmp_path / "acme" / "pricemon.db"
    first = Store(path)
    run_id = first.start_run("2026-11-10T07:00:00+00:00")
    first.add_observation(run_id, ok(A, "119"))
    first.close()
    second = Store(path)
    assert second.last_ok(A).price == Decimal("119")
    second.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_store.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'pricemon.store'`.

- [ ] **Step 3: Write `src/pricemon/store.py`**

```python
"""SQLite storage for one client: runs, observations and events.

Prices are stored as decimal text, never as floats.
"""

import sqlite3
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from .models import OK, Event, Observation

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    urls_total INTEGER NOT NULL DEFAULT 0,
    urls_ok INTEGER NOT NULL DEFAULT 0,
    urls_failed INTEGER NOT NULL DEFAULT 0,
    dry_run INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    url TEXT NOT NULL,
    product_name TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    status TEXT NOT NULL,
    http_status INTEGER,
    title TEXT,
    price TEXT,
    currency TEXT,
    in_stock INTEGER,
    method TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_observations_url ON observations(url, fetched_at);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    observation_id INTEGER NOT NULL REFERENCES observations(id),
    url TEXT NOT NULL,
    product_name TEXT NOT NULL,
    type TEXT NOT NULL,
    old_price TEXT,
    new_price TEXT,
    change_pct TEXT,
    verify INTEGER NOT NULL DEFAULT 0,
    alerted INTEGER NOT NULL DEFAULT 0
);
"""


def _decimal(value) -> Decimal | None:
    return None if value is None else Decimal(value)


def _text(value) -> str | None:
    return None if value is None else str(value)


def _flag(value) -> bool | None:
    return None if value is None else bool(value)


def _observation(row: sqlite3.Row) -> Observation:
    return Observation(
        url=row["url"],
        product_name=row["product_name"],
        fetched_at=row["fetched_at"],
        status=row["status"],
        http_status=row["http_status"],
        title=row["title"],
        price=_decimal(row["price"]),
        currency=row["currency"],
        in_stock=_flag(row["in_stock"]),
        method=row["method"],
        error=row["error"],
        id=row["id"],
    )


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)

    def close(self) -> None:
        self.db.close()

    # runs

    def start_run(self, started_at: str, dry_run: bool = False) -> int:
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO runs (started_at, dry_run) VALUES (?, ?)", (started_at, int(dry_run))
            )
        return cursor.lastrowid

    def finish_run(self, run_id: int, finished_at: str, urls_total: int, urls_ok: int, urls_failed: int) -> None:
        with self.db:
            self.db.execute(
                "UPDATE runs SET finished_at = ?, urls_total = ?, urls_ok = ?, urls_failed = ? WHERE id = ?",
                (finished_at, urls_total, urls_ok, urls_failed, run_id),
            )

    def run(self, run_id: int) -> dict:
        row = self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"no run with id {run_id}")
        return dict(row)

    def runs(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM runs ORDER BY id")]

    # observations

    def add_observation(self, run_id: int, obs: Observation) -> Observation:
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO observations (run_id, url, product_name, fetched_at, status, http_status,"
                " title, price, currency, in_stock, method, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, obs.url, obs.product_name, obs.fetched_at, obs.status, obs.http_status,
                    obs.title, _text(obs.price), obs.currency,
                    None if obs.in_stock is None else int(obs.in_stock),
                    obs.method, obs.error,
                ),
            )
        return replace(obs, id=cursor.lastrowid)

    def last_ok(self, url: str, before_run_id: int | None = None) -> Observation | None:
        """The most recent successful observation of a URL, optionally before a given run."""
        sql = "SELECT * FROM observations WHERE url = ? AND status = ?"
        params: list = [url, OK]
        if before_run_id is not None:
            sql += " AND run_id < ?"
            params.append(before_run_id)
        row = self.db.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()
        return None if row is None else _observation(row)

    def consecutive_failures(self, url: str, up_to_run_id: int | None = None) -> int:
        """How many runs in a row a URL has failed, counting back from the latest (or given) run."""
        sql = "SELECT status FROM observations WHERE url = ?"
        params: list = [url]
        if up_to_run_id is not None:
            sql += " AND run_id <= ?"
            params.append(up_to_run_id)
        count = 0
        for row in self.db.execute(sql + " ORDER BY id DESC", params):
            if row["status"] == OK:
                break
            count += 1
        return count

    def run_observations(self, run_id: int) -> list[Observation]:
        rows = self.db.execute("SELECT * FROM observations WHERE run_id = ? ORDER BY id", (run_id,))
        return [_observation(row) for row in rows]

    # events

    def add_event(self, run_id: int, observation_id: int, event: Event) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO events (run_id, observation_id, url, product_name, type, old_price,"
                " new_price, change_pct, verify, alerted) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, observation_id, event.url, event.product_name, event.type,
                    _text(event.old_price), _text(event.new_price), _text(event.change_pct),
                    int(event.verify), int(event.alerted),
                ),
            )

    def run_events(self, run_id: int) -> list[Event]:
        rows = self.db.execute(
            "SELECT e.*, o.currency AS currency FROM events e"
            " JOIN observations o ON o.id = e.observation_id"
            " WHERE e.run_id = ? ORDER BY e.id",
            (run_id,),
        )
        return [
            Event(
                url=row["url"],
                product_name=row["product_name"],
                type=row["type"],
                old_price=_decimal(row["old_price"]),
                new_price=_decimal(row["new_price"]),
                change_pct=_decimal(row["change_pct"]),
                verify=bool(row["verify"]),
                alerted=bool(row["alerted"]),
                currency=row["currency"],
            )
            for row in rows
        ]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_store.py -q`
Expected: `7 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/pricemon/store.py tests/test_store.py
git commit -m "feat: SQLite store for runs, observations and events"
```

---

### Task 6: Change detector

**Files:**
- Create: `src/pricemon/detect.py`
- Test: `tests/test_detect.py`

**Interfaces:**
- Consumes: `Observation`, `Event` from `pricemon.models`; `Alerts` from `pricemon.config`.
- Produces (`pricemon.detect`): `detect(previous: Observation | None, new: Observation, alerts: Alerts) -> list[Event]`. A pure function with no I/O. `previous` is the last `ok` observation of the same URL, `new` is an `ok` observation.

Rules, from the spec:
- No previous observation: no events.
- Price events only when both currencies are equal (two `None` currencies count as equal).
- `change_pct` is stored rounded to 2 decimals; thresholds are compared against the unrounded value.
- `verify` is true when `abs(change) >= alerts.verify_threshold_pct`.
- A price event is `alerted` when its type is in `alerts.events`, `verify` is false, and `abs(change) >= alerts.min_price_change_pct`.
- Stock: `True -> False` is `out_of_stock`, `False -> True` is `back_in_stock`; any transition involving `None` gives no event. A stock event is `alerted` when its type is in `alerts.events`.

- [ ] **Step 1: Write the failing tests**

`tests/test_detect.py`:

```python
from decimal import Decimal

from pricemon.config import Alerts
from pricemon.detect import detect
from pricemon.models import OK, Observation

URL = "https://a.example/p/1"
ALERTS = Alerts()


def obs(price, in_stock=True, currency="EUR"):
    return Observation(
        url=URL, product_name="Drill", fetched_at="2026-11-10T07:00:00+00:00", status=OK,
        price=Decimal(price), currency=currency, in_stock=in_stock,
    )


def test_first_observation_is_a_baseline():
    assert detect(None, obs("119"), ALERTS) == []


def test_no_change_gives_no_events():
    assert detect(obs("119.00"), obs("119"), ALERTS) == []


def test_price_down():
    (event,) = detect(obs("129.00"), obs("119.00"), ALERTS)
    assert event.type == "price_down"
    assert (event.url, event.product_name, event.currency) == (URL, "Drill", "EUR")
    assert (event.old_price, event.new_price) == (Decimal("129.00"), Decimal("119.00"))
    assert event.change_pct == Decimal("-7.75")
    assert (event.verify, event.alerted) == (False, True)


def test_price_up():
    (event,) = detect(obs("100"), obs("110"), ALERTS)
    assert (event.type, event.change_pct, event.alerted) == ("price_up", Decimal("10.00"), True)


def test_small_change_is_recorded_but_not_alerted():
    (event,) = detect(obs("100.00"), obs("100.50"), ALERTS)
    assert (event.type, event.change_pct, event.verify, event.alerted) == ("price_up", Decimal("0.50"), False, False)


def test_change_at_the_minimum_is_alerted():
    (event,) = detect(obs("100"), obs("101"), ALERTS)
    assert event.alerted is True


def test_large_change_is_marked_verify_and_not_alerted():
    (event,) = detect(obs("100"), obs("30"), ALERTS)
    assert (event.type, event.change_pct, event.verify, event.alerted) == ("price_down", Decimal("-70.00"), True, False)


def test_event_type_not_selected_is_not_alerted():
    alerts = Alerts(events=["price_down"])
    (event,) = detect(obs("100"), obs("110"), alerts)
    assert (event.type, event.alerted) == ("price_up", False)


def test_currency_change_gives_no_price_event():
    assert detect(obs("100", currency="EUR"), obs("110", currency="USD"), ALERTS) == []


def test_two_missing_currencies_count_as_the_same():
    (event,) = detect(obs("100", currency=None), obs("110", currency=None), ALERTS)
    assert (event.type, event.currency) == ("price_up", None)


def test_out_of_stock_and_back_in_stock():
    (out,) = detect(obs("100", in_stock=True), obs("100", in_stock=False), ALERTS)
    assert (out.type, out.change_pct, out.verify, out.alerted) == ("out_of_stock", None, False, True)
    assert (out.old_price, out.new_price) == (Decimal("100"), Decimal("100"))
    (back,) = detect(obs("100", in_stock=False), obs("100", in_stock=True), ALERTS)
    assert back.type == "back_in_stock"


def test_unknown_stock_gives_no_stock_event():
    assert detect(obs("100", in_stock=None), obs("100", in_stock=False), ALERTS) == []
    assert detect(obs("100", in_stock=True), obs("100", in_stock=None), ALERTS) == []


def test_price_and_stock_can_change_together():
    events = detect(obs("100", in_stock=True), obs("90", in_stock=False), ALERTS)
    assert [e.type for e in events] == ["price_down", "out_of_stock"]


def test_stock_event_not_selected_is_not_alerted():
    alerts = Alerts(events=["price_down", "price_up"])
    (event,) = detect(obs("100", in_stock=True), obs("100", in_stock=False), alerts)
    assert event.alerted is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_detect.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'pricemon.detect'`.

- [ ] **Step 3: Write `src/pricemon/detect.py`**

```python
"""Change detection: compare a new observation with the previous good one."""

from decimal import Decimal

from .config import Alerts
from .models import Event, Observation

_TWO_PLACES = Decimal("0.01")


def detect(previous: Observation | None, new: Observation, alerts: Alerts) -> list[Event]:
    """Events for one URL. Pure: no I/O. The first observation is a baseline."""
    if previous is None:
        return []
    events: list[Event] = []

    comparable = previous.price is not None and new.price is not None and previous.currency == new.currency
    if comparable and new.price != previous.price:
        change = (new.price - previous.price) / previous.price * 100
        kind = "price_down" if new.price < previous.price else "price_up"
        verify = abs(change) >= alerts.verify_threshold_pct
        alerted = kind in alerts.events and not verify and abs(change) >= alerts.min_price_change_pct
        events.append(
            Event(
                url=new.url,
                product_name=new.product_name,
                type=kind,
                old_price=previous.price,
                new_price=new.price,
                change_pct=change.quantize(_TWO_PLACES),
                verify=verify,
                alerted=alerted,
                currency=new.currency,
            )
        )

    stock_kind = None
    if previous.in_stock is True and new.in_stock is False:
        stock_kind = "out_of_stock"
    elif previous.in_stock is False and new.in_stock is True:
        stock_kind = "back_in_stock"
    if stock_kind is not None:
        events.append(
            Event(
                url=new.url,
                product_name=new.product_name,
                type=stock_kind,
                old_price=previous.price,
                new_price=new.price,
                change_pct=None,
                verify=False,
                alerted=stock_kind in alerts.events,
                currency=new.currency,
            )
        )
    return events
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_detect.py -q`
Expected: `14 passed`.

- [ ] **Step 5: Run the whole suite and commit**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: `102 passed`.

```bash
git add src/pricemon/detect.py tests/test_detect.py
git commit -m "feat: price and stock change detection"
```

---

### Task 7: Runner and command line (`run`, `check-url`, `validate`)

**Files:**
- Create: `src/pricemon/runner.py`, `src/pricemon/cli.py`, `src/pricemon/__main__.py`
- Create: `tests/helpers.py`
- Test: `tests/test_runner.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `ClientConfig`, `SiteRules`, `ConfigError`, `hostname`, `rules_for`, `load_client`, `load_settings`, `load_site_rules` from `pricemon.config`; `Fetcher` from `pricemon.fetch`; `extract`, `ExtractError` from `pricemon.extract`; `Store` from `pricemon.store`; `detect` from `pricemon.detect`; models from `pricemon.models`.
- Produces (`pricemon.runner`): `utc_now() -> datetime`; `order_round_robin(items: list[tuple[str, str]]) -> list[tuple[str, str]]` where each item is `(url, product_name)`; `observe(url, product_name, fetcher, rules, now=utc_now) -> Observation`; `run_client(config: ClientConfig, *, site_rules: dict[str, SiteRules], data_dir: Path, fetcher: Fetcher, dry_run: bool = False, now=utc_now) -> RunSummary`. The database is `data_dir / config.client / "pricemon.db"`.
- Produces (`pricemon.cli`): `main(argv: list[str] | None = None) -> int`; `build_fetcher(settings: Settings) -> Fetcher` (tests replace it); module constants `SITES_DIR = Path("sites")`, `DATA_DIR = Path("data")`, `SETTINGS_PATH = Path("settings.yaml")`, all relative to the working directory.
- Produces (`tests/helpers.py`): `page(price, availability="InStock", name="Drill") -> str`, `Site`, `make_fetcher(site) -> Fetcher`, used by later test files.

Reports and notifications are wired into `run_client` in Tasks 8 and 9.

- [ ] **Step 1: Write the shared test helpers**

`tests/helpers.py`:

```python
"""Shared helpers for tests that need a fake website."""

import json

import httpx

from pricemon.config import Settings
from pricemon.fetch import Fetcher


def page(price, availability="InStock", name="Drill"):
    """A product page with JSON-LD, priced in EUR."""
    data = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": name,
        "offers": {
            "@type": "Offer",
            "price": price,
            "priceCurrency": "EUR",
            "availability": f"https://schema.org/{availability}",
        },
    }
    return f'<html><head><script type="application/ld+json">{json.dumps(data)}</script></head><body></body></html>'


class Site:
    """A fake web: maps full URLs to (status, body). Unknown URLs return 404."""

    def __init__(self, pages=None):
        self.pages = dict(pages or {})
        self.requested = []

    def handler(self, request):
        self.requested.append(str(request.url))
        status, body = self.pages.get(str(request.url), (404, ""))
        return httpx.Response(status, text=body)


def make_fetcher(site):
    """A Fetcher that talks to a Site, with no waiting, no retries and no robots checks."""
    settings = Settings(respect_robots_txt=False, min_interval_seconds=0, retries=0)
    client = httpx.Client(transport=httpx.MockTransport(site.handler))
    return Fetcher(settings, client=client, sleep=lambda seconds: None)
```

- [ ] **Step 2: Write the failing runner tests**

`tests/test_runner.py`:

```python
from decimal import Decimal

from helpers import Site, make_fetcher, page

from pricemon.config import ClientConfig
from pricemon.models import EXTRACT_ERROR, FETCH_ERROR, OK
from pricemon.runner import observe, order_round_robin, run_client
from pricemon.store import Store

A = "https://a.example/p/drill"
B = "https://b.example/p/drill"
C = "https://a.example/p/saw"

CONFIG = ClientConfig.model_validate(
    {"client": "acme", "products": [{"name": "Drill", "urls": [A, B]}, {"name": "Saw", "urls": [C]}]}
)


def healthy_site():
    return Site({A: (200, page("119.00")), B: (200, page("125.00")), C: (200, page("80.00", name="Saw"))})


def run(site, tmp_path, **kwargs):
    return run_client(CONFIG, site_rules={}, data_dir=tmp_path / "data", fetcher=make_fetcher(site), **kwargs)


def open_store(tmp_path):
    return Store(tmp_path / "data" / "acme" / "pricemon.db")


def test_round_robin_interleaves_hosts():
    items = [(A, "Drill"), (C, "Saw"), (B, "Drill")]
    assert order_round_robin(items) == [(A, "Drill"), (B, "Drill"), (C, "Saw")]
    assert order_round_robin([]) == []


def test_first_run_is_a_baseline(tmp_path):
    summary = run(healthy_site(), tmp_path)
    assert (summary.client, summary.run_id, summary.dry_run) == ("acme", 1, False)
    assert (summary.urls_total, summary.urls_ok, summary.urls_failed) == (3, 3, 0)
    assert summary.events == []
    store = open_store(tmp_path)
    observations = store.run_observations(1)
    stored_run = store.run(1)
    store.close()
    assert [o.url for o in observations] == [A, B, C]
    assert all(o.status == OK and o.method == "jsonld" for o in observations)
    assert (observations[0].price, observations[0].product_name) == (Decimal("119.00"), "Drill")
    assert observations[2].product_name == "Saw"
    assert stored_run["finished_at"] is not None
    assert (stored_run["urls_total"], stored_run["urls_ok"], stored_run["dry_run"]) == (3, 3, 0)


def test_second_run_detects_changes_and_survives_failures(tmp_path):
    site = healthy_site()
    run(site, tmp_path)
    site.pages[A] = (200, page("109.00"))
    site.pages[B] = (500, "")
    site.pages[C] = (200, "<html><body><h1>Saw</h1></body></html>")
    summary = run(site, tmp_path)
    assert (summary.urls_total, summary.urls_ok, summary.urls_failed) == (3, 1, 2)
    assert [(e.type, e.url, e.alerted) for e in summary.events] == [("price_down", A, True)]
    store = open_store(tmp_path)
    observations = {o.url: o for o in store.run_observations(2)}
    events = store.run_events(2)
    store.close()
    assert {url: o.status for url, o in observations.items()} == {A: OK, B: FETCH_ERROR, C: EXTRACT_ERROR}
    assert observations[B].error == "HTTP 500"
    assert "no price found" in observations[C].error
    assert [(e.type, e.old_price, e.new_price) for e in events] == [
        ("price_down", Decimal("119.00"), Decimal("109.00"))
    ]


def test_recovery_compares_with_the_last_good_price(tmp_path):
    site = healthy_site()
    run(site, tmp_path)
    site.pages[A] = (500, "")
    assert run(site, tmp_path).events == []
    site.pages[A] = (200, page("109.00"))
    summary = run(site, tmp_path)
    assert [(e.type, e.old_price) for e in summary.events] == [("price_down", Decimal("119.00"))]


def test_stock_change_is_detected(tmp_path):
    site = healthy_site()
    run(site, tmp_path)
    site.pages[B] = (200, page("125.00", availability="OutOfStock"))
    summary = run(site, tmp_path)
    assert [(e.type, e.url) for e in summary.events] == [("out_of_stock", B)]


def test_dry_run_is_recorded(tmp_path):
    summary = run(healthy_site(), tmp_path, dry_run=True)
    assert summary.dry_run is True
    store = open_store(tmp_path)
    assert store.run(1)["dry_run"] == 1
    store.close()


def test_extractor_crash_is_one_failed_page(tmp_path, monkeypatch):
    def boom(html, url, rules):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr("pricemon.runner.extract", boom)
    summary = run(healthy_site(), tmp_path)
    assert (summary.urls_ok, summary.urls_failed) == (0, 3)
    store = open_store(tmp_path)
    observations = store.run_observations(1)
    store.close()
    assert all(o.status == EXTRACT_ERROR and "parser exploded" in o.error for o in observations)


def test_fetcher_crash_is_one_failed_page():
    class BrokenFetcher:
        def fetch(self, url, rules=None):
            raise RuntimeError("socket exploded")

    obs = observe(A, "Drill", BrokenFetcher(), None)
    assert obs.status == FETCH_ERROR
    assert "socket exploded" in obs.error
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_runner.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'pricemon.runner'`.

- [ ] **Step 4: Write `src/pricemon/runner.py`**

```python
"""Orchestrates one run for one client. The only module that knows the stage order."""

from datetime import datetime, timezone
from pathlib import Path

from .config import ClientConfig, SiteRules, hostname, rules_for
from .detect import detect
from .extract import extract
from .fetch import Fetcher
from .models import EXTRACT_ERROR, FETCH_ERROR, OK, Observation, RunSummary
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
            previous = store.last_ok(url)
            saved = store.add_observation(run_id, observation)
            summary.urls_total += 1
            if saved.status != OK:
                summary.urls_failed += 1
                continue
            summary.urls_ok += 1
            for event in detect(previous, saved, config.alerts):
                store.add_event(run_id, saved.id, event)
                summary.events.append(event)
        store.finish_run(run_id, now().isoformat(), summary.urls_total, summary.urls_ok, summary.urls_failed)
        return summary
    finally:
        store.close()
```

- [ ] **Step 5: Run the runner tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_runner.py -q`
Expected: `8 passed`.

- [ ] **Step 6: Write the failing CLI tests**

`tests/test_cli.py`:

```python
import pytest
from helpers import Site, make_fetcher, page

from pricemon import cli
from pricemon.models import BLOCKED, OK, FetchResult

A = "https://a.example/p/drill"
B = "https://b.example/p/drill"

CONFIG = f"""\
client: acme-shop
products:
  - name: Drill
    urls:
      - {A}
      - {B}
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    """An empty project folder as the working directory, holding one client config."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "clients").mkdir()
    (tmp_path / "clients" / "acme-shop.yaml").write_text(CONFIG, encoding="utf-8")
    return tmp_path


class StubFetcher:
    def __init__(self, result):
        self.result = result
        self.rules = None

    def fetch(self, url, rules=None):
        self.rules = rules
        return self.result


def use_stub(monkeypatch, result):
    stub = StubFetcher(result)
    monkeypatch.setattr(cli, "build_fetcher", lambda settings: stub)
    return stub


def test_validate_lists_sites_and_their_rules(project, capsys):
    (project / "sites").mkdir()
    (project / "sites" / "b.yaml").write_text("domain: b.example\nprice: {css: .price}\n", encoding="utf-8")
    assert cli.main(["validate", "clients/acme-shop.yaml"]) == 0
    out = capsys.readouterr().out
    assert "OK: acme-shop: 1 product(s), 2 URL(s), 2 site(s)" in out
    assert "a.example: no site rules" in out
    assert "b.example: site rules found" in out


def test_invalid_config_exits_1(project, capsys):
    (project / "clients" / "bad.yaml").write_text("client: acme\n", encoding="utf-8")
    assert cli.main(["validate", "clients/bad.yaml"]) == 1
    assert "Config error" in capsys.readouterr().err


def test_check_url_prints_fields_and_their_sources(project, capsys, monkeypatch):
    use_stub(monkeypatch, FetchResult(OK, 200, page("119.00")))
    assert cli.main(["check-url", A]) == 0
    out = capsys.readouterr().out
    assert f"URL:        {A}" in out
    assert "Site rules: none" in out
    assert "Fetch:      ok (HTTP 200)" in out
    assert "Title:      Drill  [jsonld]" in out
    assert "Price:      119.00  [jsonld]" in out
    assert "Currency:   EUR  [jsonld]" in out
    assert "In stock:   yes  [jsonld]" in out
    assert "Verdict:    works" in out


def test_check_url_blocked_site_is_unsupported(project, capsys, monkeypatch):
    use_stub(monkeypatch, FetchResult(BLOCKED, 403, error="HTTP 403"))
    assert cli.main(["check-url", A]) == 0
    out = capsys.readouterr().out
    assert "Fetch:      blocked (HTTP 403)" in out
    assert "Error:      HTTP 403" in out
    assert "unsupported" in out


def test_check_url_without_price_asks_for_site_rules(project, capsys, monkeypatch):
    use_stub(monkeypatch, FetchResult(OK, 200, "<html><body><h1>Drill</h1></body></html>"))
    assert cli.main(["check-url", A]) == 0
    out = capsys.readouterr().out
    assert "no price found" in out
    assert "sites/a.example.yaml" in out


def test_check_url_browser_flag_selects_the_browser_fetcher(project, capsys, monkeypatch):
    stub = use_stub(monkeypatch, FetchResult(OK, 200, page("119.00")))
    assert cli.main(["check-url", A, "--browser"]) == 0
    assert stub.rules.fetcher == "browser"
    assert stub.rules.domain == "a.example"


def test_run_dry_run_writes_the_database_and_prints_a_summary(project, capsys, monkeypatch):
    site = Site({A: (200, page("119.00")), B: (200, page("125.00"))})
    monkeypatch.setattr(cli, "build_fetcher", lambda settings: make_fetcher(site))
    assert cli.main(["run", "clients/acme-shop.yaml", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "acme-shop: run 1 (dry run, nothing sent)" in out
    assert "URLs:   2 total, 2 ok, 0 failed" in out
    assert "Events: 0 (0 alerted, 0 to verify)" in out
    assert (project / "data" / "acme-shop" / "pricemon.db").exists()


def test_run_with_failed_pages_still_exits_0(project, capsys, monkeypatch):
    site = Site({A: (200, page("119.00"))})
    monkeypatch.setattr(cli, "build_fetcher", lambda settings: make_fetcher(site))
    assert cli.main(["run", "clients/acme-shop.yaml", "--dry-run"]) == 0
    assert "2 total, 1 ok, 1 failed" in capsys.readouterr().out


def test_fatal_error_exits_1(project, capsys, monkeypatch):
    def broken(settings):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "build_fetcher", broken)
    assert cli.main(["run", "clients/acme-shop.yaml"]) == 1
    assert "Fatal error: boom" in capsys.readouterr().err
```

- [ ] **Step 7: Run the CLI tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cli.py -q`
Expected: collection error, `ImportError: cannot import name 'cli' from 'pricemon'`.

- [ ] **Step 8: Write `src/pricemon/cli.py` and `src/pricemon/__main__.py`**

`src/pricemon/cli.py`:

```python
"""Command line interface: run, check-url, validate."""

import argparse
import sys
from pathlib import Path

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
from .runner import run_client

# Relative to the working directory, which is the project folder.
SITES_DIR = Path("sites")
DATA_DIR = Path("data")
SETTINGS_PATH = Path("settings.yaml")

_FETCH_VERDICTS = {
    BLOCKED: "unsupported: the site blocks automated requests",
    DISALLOWED: "unsupported: robots.txt disallows this page",
    NOT_FOUND: "page not found: check the URL",
    FETCH_ERROR: "could not fetch: try again later, or try --browser if the page needs JavaScript",
}
_STOCK_LABELS = {True: "yes", False: "no", None: "unknown"}


def build_fetcher(settings: Settings) -> Fetcher:
    return Fetcher(settings)


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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 1
```

`src/pricemon/__main__.py`:

```python
from .cli import main

raise SystemExit(main())
```

- [ ] **Step 9: Run the CLI tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cli.py -q`
Expected: `9 passed`.

- [ ] **Step 10: Check the installed command**

Run: `.venv/Scripts/pricemon.exe validate clients/example.yaml`
Expected output:

```
OK: example-shop: 1 product(s), 2 URL(s), 2 site(s)
  competitor-a.example: no site rules (fine if the site publishes structured data; test with check-url)
  competitor-b.example: no site rules (fine if the site publishes structured data; test with check-url)
```

- [ ] **Step 11: Run the whole suite and commit**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: `119 passed`.

```bash
git add src/pricemon/runner.py src/pricemon/cli.py src/pricemon/__main__.py tests/helpers.py tests/test_runner.py tests/test_cli.py
git commit -m "feat: runner and CLI (run, check-url, validate)"
```

---

### Task 8: Reporter and the `report` command

**Files:**
- Create: `src/pricemon/report.py`
- Modify: `src/pricemon/runner.py` (write the report at the end of `run_client`)
- Modify: `src/pricemon/cli.py` (add the `report` command)
- Test: `tests/test_report.py`; add tests to `tests/test_runner.py` and `tests/test_cli.py`

**Interfaces:**
- Consumes: `ClientConfig`, `hostname` from `pricemon.config`; `Store` from `pricemon.store`; `OK` from `pricemon.models`.
- Produces (`pricemon.report`): `PRICES_HEADER`, `CHANGES_HEADER`, `PROBLEMS_HEADER` (lists of column names); `ReportData(prices, changes, problems)` (three lists of rows); `build_report(config: ClientConfig, store: Store, run_id: int) -> ReportData`; `write_report(config: ClientConfig, store: Store, run_id: int, out_dir: Path) -> list[Path]`. The first returned path is always the `.xlsx`; a `.csv` follows when `"csv"` is in `config.report.formats`. Files are named `<local date of the run>.xlsx` / `.csv` and overwrite an existing file of the same day.
- After this task `RunSummary.report_paths` is filled by `run_client` (xlsx first), and reports are written to `data_dir / config.client / "reports"`.

- [ ] **Step 1: Write the failing report tests**

`tests/test_report.py`:

```python
import csv
from decimal import Decimal

import pytest
from openpyxl import load_workbook

from pricemon.config import ClientConfig
from pricemon.detect import detect
from pricemon.models import FETCH_ERROR, OK, Observation
from pricemon.report import CHANGES_HEADER, PRICES_HEADER, PROBLEMS_HEADER, write_report
from pricemon.store import Store

A = "https://www.a.example/p/drill"
B = "https://b.example/p/drill"

CONFIG = ClientConfig.model_validate(
    {"client": "acme", "products": [{"name": "Drill", "sku": "BOS-1", "own_price": "129.00", "urls": [A, B]}]}
)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "acme" / "pricemon.db")
    yield s
    s.close()


def ok(url, price, currency="EUR", in_stock=True):
    return Observation(
        url=url, product_name="Drill", fetched_at="2026-11-10T07:00:05+00:00", status=OK,
        http_status=200, title="Drill 12V", price=Decimal(price), currency=currency,
        in_stock=in_stock, method="jsonld",
    )


def failed(url):
    return Observation(
        url=url, product_name="Drill", fetched_at="2026-11-10T07:00:05+00:00",
        status=FETCH_ERROR, http_status=500, error="HTTP 500",
    )


def add(store, run_id, observation):
    """Store an observation and its events, the way the runner does."""
    previous = store.last_ok(observation.url)
    saved = store.add_observation(run_id, observation)
    if saved.status == OK:
        for event in detect(previous, saved, CONFIG.alerts):
            store.add_event(run_id, saved.id, event)


def two_runs(store):
    run1 = store.start_run("2026-11-09T07:00:00+00:00")
    add(store, run1, ok(A, "119.00"))
    add(store, run1, ok(B, "50.00", in_stock=False))
    run2 = store.start_run("2026-11-10T07:00:00+00:00")
    add(store, run2, ok(A, "109.00"))
    add(store, run2, failed(B))
    return run2


def sheet(path, name):
    """(header, list of row dicts) for one sheet."""
    rows = [list(row) for row in load_workbook(path)[name].iter_rows(values_only=True)]
    return rows[0], [dict(zip(rows[0], row)) for row in rows[1:]]


def test_workbook_has_three_sheets_with_headers(store, tmp_path):
    paths = write_report(CONFIG, store, two_runs(store), tmp_path / "reports")
    assert paths[0].suffix == ".xlsx" and paths[0].exists()
    assert load_workbook(paths[0]).sheetnames == ["Prices", "Changes", "Problems"]
    assert sheet(paths[0], "Prices")[0] == PRICES_HEADER
    assert sheet(paths[0], "Changes")[0] == CHANGES_HEADER
    assert sheet(paths[0], "Problems")[0] == PROBLEMS_HEADER


def test_prices_sheet(store, tmp_path):
    paths = write_report(CONFIG, store, two_runs(store), tmp_path / "reports")
    _, rows = sheet(paths[0], "Prices")
    by_url = {row["URL"]: row for row in rows}
    assert len(rows) == 2

    a = by_url[A]
    assert (a["Product"], a["SKU"], a["Own price"], a["Competitor"]) == ("Drill", "BOS-1", 129, "a.example")
    assert (a["Title"], a["Price"], a["Currency"], a["In stock"]) == ("Drill 12V", 109, "EUR", "yes")
    assert a["Previous price"] == 119
    assert a["Change %"] == pytest.approx(-8.4)
    assert a["Status"] == "ok"
    assert a["Last checked"] is not None

    b = by_url[B]  # failed this run: shows the last known values
    assert (b["Price"], b["Currency"], b["In stock"]) == (50, "EUR", "no")
    assert (b["Previous price"], b["Change %"]) == (None, None)
    assert b["Status"] == "fetch_error"


def test_changes_and_problems_sheets(store, tmp_path):
    paths = write_report(CONFIG, store, two_runs(store), tmp_path / "reports")
    _, changes = sheet(paths[0], "Changes")
    assert len(changes) == 1
    change = changes[0]
    assert (change["Product"], change["Competitor"], change["Change"]) == ("Drill", "a.example", "price down")
    assert (change["Old price"], change["New price"], change["Verify"], change["URL"]) == (119, 109, None, A)
    assert change["Change %"] == pytest.approx(-8.4)

    _, problems = sheet(paths[0], "Problems")
    assert len(problems) == 1
    problem = problems[0]
    assert (problem["Product"], problem["Competitor"], problem["Status"]) == ("Drill", "b.example", "fetch_error")
    assert (problem["Reason"], problem["Failed runs in a row"], problem["URL"]) == ("HTTP 500", 1, B)


def test_large_change_is_marked_verify(store, tmp_path):
    run1 = store.start_run("2026-11-09T07:00:00+00:00")
    add(store, run1, ok(A, "100.00"))
    run2 = store.start_run("2026-11-10T07:00:00+00:00")
    add(store, run2, ok(A, "10.00"))
    _, changes = sheet(write_report(CONFIG, store, run2, tmp_path / "reports")[0], "Changes")
    assert changes[0]["Verify"] == "verify"


def test_currency_change_is_a_problem_not_a_price_change(store, tmp_path):
    run1 = store.start_run("2026-11-09T07:00:00+00:00")
    add(store, run1, ok(A, "100.00", currency="EUR"))
    run2 = store.start_run("2026-11-10T07:00:00+00:00")
    add(store, run2, ok(A, "110.00", currency="USD"))
    path = write_report(CONFIG, store, run2, tmp_path / "reports")[0]
    _, prices = sheet(path, "Prices")
    assert (prices[0]["Price"], prices[0]["Previous price"], prices[0]["Change %"]) == (110, None, None)
    _, changes = sheet(path, "Changes")
    assert changes == []
    _, problems = sheet(path, "Problems")
    assert (problems[0]["Status"], problems[0]["Reason"]) == (
        "currency_changed", "currency changed from EUR to USD",
    )


def test_url_that_never_worked(store, tmp_path):
    run1 = store.start_run("2026-11-10T07:00:00+00:00")
    add(store, run1, failed(B))
    _, prices = sheet(write_report(CONFIG, store, run1, tmp_path / "reports")[0], "Prices")
    row = prices[0]
    assert (row["Price"], row["In stock"], row["Last checked"], row["Status"]) == (None, "unknown", None, "fetch_error")


def test_csv_matches_the_prices_sheet(store, tmp_path):
    paths = write_report(CONFIG, store, two_runs(store), tmp_path / "reports")
    assert [p.suffix for p in paths] == [".xlsx", ".csv"]
    assert paths[0].stem == paths[1].stem
    with paths[1].open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == PRICES_HEADER
    assert len(rows) == 3
    a = dict(zip(rows[0], next(row for row in rows[1:] if row[-1] == A)))
    assert (a["Price"], a["Previous price"], a["Change %"]) == ("109.00", "119.00", "-8.40")
    b = dict(zip(rows[0], next(row for row in rows[1:] if row[-1] == B)))
    assert b["Previous price"] == ""


def test_csv_is_optional(store, tmp_path):
    config = CONFIG.model_copy(update={"report": CONFIG.report.model_copy(update={"formats": ["xlsx"]})})
    paths = write_report(config, store, two_runs(store), tmp_path / "reports")
    assert [p.suffix for p in paths] == [".xlsx"]


def test_same_day_report_is_overwritten(store, tmp_path):
    run2 = two_runs(store)
    first = write_report(CONFIG, store, run2, tmp_path / "reports")
    second = write_report(CONFIG, store, run2, tmp_path / "reports")
    assert first == second
    assert len(list((tmp_path / "reports").iterdir())) == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_report.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'pricemon.report'`.

- [ ] **Step 3: Write `src/pricemon/report.py`**

```python
"""The daily report: an Excel workbook and an optional CSV."""

import csv
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
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
    for obs in store.run_observations(run_id):
        product = products.get(obs.url)
        sku = product.sku if product else None
        own_price = product.own_price if product else None
        host = hostname(obs.url)
        previous = store.last_ok(obs.url, before_run_id=run_id)

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
                store.consecutive_failures(obs.url, up_to_run_id=run_id), obs.url,
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
    day = datetime.fromisoformat(store.run(run_id)["started_at"]).astimezone().date().isoformat()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()
    sheets = (("Prices", PRICES_HEADER, data.prices), ("Changes", CHANGES_HEADER, data.changes),
              ("Problems", PROBLEMS_HEADER, data.problems))
    for index, (title, header, rows) in enumerate(sheets):
        worksheet = workbook.active if index == 0 else workbook.create_sheet()
        worksheet.title = title
        worksheet.append(header)
        for cell in worksheet[1]:
            cell.font = Font(bold=True)
        worksheet.freeze_panes = "A2"
        for row in rows:
            worksheet.append(row)
    xlsx_path = out_dir / f"{day}.xlsx"
    workbook.save(str(xlsx_path))
    paths = [xlsx_path]

    if "csv" in config.report.formats:
        csv_path = out_dir / f"{day}.csv"
        # utf-8-sig so Excel opens the file with the right encoding.
        with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(PRICES_HEADER)
            writer.writerows(["" if value is None else value for value in row] for row in data.prices)
        paths.append(csv_path)
    return paths
```

- [ ] **Step 4: Run the report tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_report.py -q`
Expected: `9 passed`.

- [ ] **Step 5: Write the failing tests for the runner and the `report` command**

Append to `tests/test_runner.py`:

```python
def test_run_writes_the_report(tmp_path):
    summary = run(healthy_site(), tmp_path)
    assert [path.rsplit(".", 1)[1] for path in summary.report_paths] == ["xlsx", "csv"]
    reports = tmp_path / "data" / "acme" / "reports"
    assert sorted(p.suffix for p in reports.iterdir()) == [".csv", ".xlsx"]
```

Append to `tests/test_cli.py`:

```python
def test_run_prints_the_report_path(project, capsys, monkeypatch):
    site = Site({A: (200, page("119.00")), B: (200, page("125.00"))})
    monkeypatch.setattr(cli, "build_fetcher", lambda settings: make_fetcher(site))
    assert cli.main(["run", "clients/acme-shop.yaml", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Report: " in out and ".xlsx" in out


def test_report_command_rebuilds_todays_report(project, capsys, monkeypatch):
    site = Site({A: (200, page("119.00")), B: (200, page("125.00"))})
    monkeypatch.setattr(cli, "build_fetcher", lambda settings: make_fetcher(site))
    cli.main(["run", "clients/acme-shop.yaml", "--dry-run"])
    for old in (project / "data" / "acme-shop" / "reports").iterdir():
        old.unlink()
    requests_before = len(site.requested)
    capsys.readouterr()
    assert cli.main(["report", "clients/acme-shop.yaml"]) == 0
    out = capsys.readouterr().out
    assert ".xlsx" in out
    assert len(list((project / "data" / "acme-shop" / "reports").iterdir())) == 2
    assert len(site.requested) == requests_before  # nothing was fetched


def test_report_command_without_a_run_on_that_date(project, capsys, monkeypatch):
    site = Site({A: (200, page("119.00")), B: (200, page("125.00"))})
    monkeypatch.setattr(cli, "build_fetcher", lambda settings: make_fetcher(site))
    cli.main(["run", "clients/acme-shop.yaml", "--dry-run"])
    capsys.readouterr()
    assert cli.main(["report", "clients/acme-shop.yaml", "--date", "2000-01-01"]) == 1
    assert "No run found on 2000-01-01" in capsys.readouterr().err


def test_report_command_without_any_data(project, capsys):
    assert cli.main(["report", "clients/acme-shop.yaml"]) == 1
    assert "No data for client acme-shop" in capsys.readouterr().err
```

- [ ] **Step 6: Run them to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_runner.py tests/test_cli.py -q`
Expected: 5 failures. `test_run_writes_the_report` fails on an empty `report_paths`; the `report` command tests fail with `SystemExit: 2` (argparse does not know `report`).

- [ ] **Step 7: Write the report from the runner**

In `src/pricemon/runner.py`, add the import next to the other package imports:

```python
from .report import write_report
```

and replace these two lines at the end of the `try` block in `run_client`:

```python
        store.finish_run(run_id, now().isoformat(), summary.urls_total, summary.urls_ok, summary.urls_failed)
        return summary
```

with:

```python
        store.finish_run(run_id, now().isoformat(), summary.urls_total, summary.urls_ok, summary.urls_failed)
        report_paths = write_report(config, store, run_id, client_dir / "reports")
        summary.report_paths = [str(path) for path in report_paths]
        return summary
```

- [ ] **Step 8: Add the `report` command**

In `src/pricemon/cli.py`, add these imports:

```python
from datetime import date, datetime
```

```python
from .report import write_report
from .store import Store
```

Add this function after `cmd_validate`:

```python
def cmd_report(args) -> int:
    config = load_client(args.config)
    day = date.fromisoformat(args.date) if args.date else date.today()
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
            if datetime.fromisoformat(run["started_at"]).astimezone().date() == day
        ]
        if not run_ids:
            print(f"No run found on {day.isoformat()}", file=sys.stderr)
            return 1
        paths = write_report(config, store, run_ids[-1], client_dir / "reports")
    finally:
        store.close()
    for path in paths:
        print(path)
    return 0
```

In `build_parser`, add before `return parser`:

```python
    report = commands.add_parser("report", help="rebuild a day's report from stored data, without fetching")
    report.add_argument("config", type=Path)
    report.add_argument("--date", help="local date of the run, YYYY-MM-DD (default: today)")
    report.set_defaults(handler=cmd_report)
```

Change the module docstring to `"""Command line interface: run, check-url, report, validate."""`.

- [ ] **Step 9: Run the whole suite to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: `133 passed`.

- [ ] **Step 10: Commit**

```bash
git add src/pricemon/report.py src/pricemon/runner.py src/pricemon/cli.py tests/test_report.py tests/test_runner.py tests/test_cli.py
git commit -m "feat: Excel and CSV report, report command"
```

---

### Task 9: Notifier, operator alerts and end-to-end test

**Files:**
- Create: `src/pricemon/notify/__init__.py`, `src/pricemon/notify/email.py`, `src/pricemon/notify/telegram.py`
- Modify: `src/pricemon/runner.py` (call the notifier), `src/pricemon/cli.py` (load `.env`, build the notifier)
- Modify: `tests/helpers.py` (add `ENV` and `Outbox`)
- Test: `tests/test_notify.py`, `tests/test_email.py`, `tests/test_telegram.py`, `tests/test_e2e.py`; add tests to `tests/test_runner.py` and `tests/test_cli.py`

**Interfaces:**
- Consumes: `ClientConfig`, `Settings`, `hostname` from `pricemon.config`; `Event`, `OK` from `pricemon.models`; `Store` from `pricemon.store`.
- Produces (`pricemon.notify.email`): `build_message(sender: str, to: list[str], subject: str, body: str, attachment: Path | None) -> EmailMessage`; `send_email(env: Mapping[str, str], to: list[str], subject: str, body: str, attachment: Path | None = None) -> None`.
- Produces (`pricemon.notify.telegram`): `TelegramError(Exception)`; `send_telegram(env: Mapping[str, str], chat_id: str, text: str, attachment: Path | None = None, client: httpx.Client | None = None) -> None`.
- Produces (`pricemon.notify`): `event_line(event: Event) -> str`; `client_digest(config: ClientConfig, events: list[Event]) -> str | None`; `operator_digest(config: ClientConfig, store: Store, run_id: int, events: list[Event], threshold: int) -> str | None`; `Notifier(env: Mapping[str, str], settings: Settings, send_email=send_email, send_telegram=send_telegram)` with `send(config: ClientConfig, store: Store, run_id: int, events: list[Event], report_path: Path | None) -> list[str]` returning warnings.
- After this task: `run_client(..., notifier=None, ...)` calls `notifier.send(...)` unless `dry_run`; `pricemon.cli.build_notifier(settings: Settings) -> Notifier`; `pricemon.cli.ENV_PATH = Path(".env")`.

Security rule for this task: the Telegram API URL contains the bot token, so no error message, warning or log line may include that URL.

- [ ] **Step 1: Add shared notification helpers for tests**

Append to `tests/helpers.py`:

```python
ENV = {
    "SMTP_HOST": "smtp.example.com",
    "SMTP_PORT": "587",
    "SMTP_USER": "monitor",
    "SMTP_PASSWORD": "secret",
    "SMTP_FROM": "monitor@example.com",
    "TELEGRAM_BOT_TOKEN": "TOKEN",
    "OPERATOR_EMAIL": "op@example.com",
    "OPERATOR_TELEGRAM_CHAT_ID": "999",
}


class Outbox:
    """Fake senders that record what would have been sent."""

    def __init__(self):
        self.emails = []  # (to, subject, body, attachment)
        self.telegrams = []  # (chat_id, text, attachment)

    def email(self, env, to, subject, body, attachment=None):
        self.emails.append((to, subject, body, attachment))

    def telegram(self, env, chat_id, text, attachment=None):
        self.telegrams.append((chat_id, text, attachment))
```

- [ ] **Step 2: Write the failing sender tests**

`tests/test_email.py`:

```python
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
    connections = []

    def recorder(kind):
        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                self.kind, self.host, self.port, self.calls = kind, host, port, []
                connections.append(self)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def starttls(self):
                self.calls.append("starttls")

            def login(self, user, password):
                self.calls.append(("login", user, password))

            def send_message(self, message):
                self.calls.append(("send", message["To"], message["Subject"]))

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
```

`tests/test_telegram.py`:

```python
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


def test_long_text_is_truncated():
    seen = []

    def handler(request):
        seen.append(request.content)
        return httpx.Response(200, json={"ok": True})

    send_telegram(ENV, "123", "z" * 5000, client=client_for(handler))
    assert seen[0].count(b"z") == 4000
    assert b"truncated" in seen[0]
```

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_email.py tests/test_telegram.py -q`
Expected: collection errors, `ModuleNotFoundError: No module named 'pricemon.notify'`.

- [ ] **Step 4: Write the senders**

Create `src/pricemon/notify/__init__.py` as an empty file for now (Step 7 fills it).

`src/pricemon/notify/email.py`:

```python
"""Email through SMTP."""

import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Mapping

_XLSX_SUBTYPE = "vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def build_message(sender: str, to: list[str], subject: str, body: str, attachment: Path | None) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    message.set_content(body)
    if attachment is not None:
        attachment = Path(attachment)
        message.add_attachment(
            attachment.read_bytes(), maintype="application", subtype=_XLSX_SUBTYPE, filename=attachment.name
        )
    return message


def send_email(env: Mapping[str, str], to: list[str], subject: str, body: str, attachment: Path | None = None) -> None:
    """Send one email. Port 465 uses SSL from the start; any other port uses STARTTLS."""
    message = build_message(env["SMTP_FROM"], to, subject, body, attachment)
    port = int(env.get("SMTP_PORT") or 587)
    if port == 465:
        with smtplib.SMTP_SSL(env["SMTP_HOST"], port, timeout=30) as smtp:
            smtp.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
            smtp.send_message(message)
    else:
        with smtplib.SMTP(env["SMTP_HOST"], port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
            smtp.send_message(message)
```

`src/pricemon/notify/telegram.py`:

```python
"""Telegram through the Bot API.

The API URL contains the bot token, so errors raised here never include the URL.
"""

from pathlib import Path
from typing import Mapping

import httpx

_API = "https://api.telegram.org"
_MAX_TEXT = 4000  # Telegram's limit is 4096 characters


class TelegramError(Exception):
    """A Bot API call failed. The message is safe to log."""


def _post(client: httpx.Client, base: str, method: str, **kwargs) -> None:
    try:
        response = client.post(f"{base}/{method}", **kwargs)
    except httpx.HTTPError as exc:
        raise TelegramError(f"{method} failed: {type(exc).__name__}") from None
    if response.status_code != 200:
        raise TelegramError(f"{method} returned HTTP {response.status_code}")


def send_telegram(
    env: Mapping[str, str],
    chat_id: str,
    text: str,
    attachment: Path | None = None,
    client: httpx.Client | None = None,
) -> None:
    """Send a text message, followed by the attachment as a document if given."""
    base = f"{_API}/bot{env['TELEGRAM_BOT_TOKEN']}"
    if len(text) > _MAX_TEXT:
        text = text[:_MAX_TEXT] + "\n... (truncated, see the report)"
    owns_client = client is None
    client = client or httpx.Client(timeout=60)
    try:
        _post(client, base, "sendMessage", data={"chat_id": chat_id, "text": text})
        if attachment is not None:
            attachment = Path(attachment)
            with attachment.open("rb") as handle:
                _post(client, base, "sendDocument", data={"chat_id": chat_id}, files={"document": (attachment.name, handle)})
    finally:
        if owns_client:
            client.close()
```

- [ ] **Step 5: Run the sender tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_email.py tests/test_telegram.py -q`
Expected: `9 passed`.

- [ ] **Step 6: Write the failing digest and notifier tests**

`tests/test_notify.py`:

```python
from decimal import Decimal

import pytest

from helpers import ENV, Outbox

from pricemon.config import ClientConfig, Settings
from pricemon.models import FETCH_ERROR, Event, Observation
from pricemon.notify import Notifier, client_digest, event_line, operator_digest
from pricemon.store import Store

A = "https://www.competitor-a.example/p/1"
DOWN = "Drill at competitor-a.example: 129.00 to 119.00 EUR, down 7.8%"
OUT = "Drill at competitor-a.example: out of stock"


def config(**notify):
    return ClientConfig.model_validate(
        {"client": "acme-shop", "products": [{"name": "Drill", "urls": [A]}], "notify": notify}
    )


def price_event(kind="price_down", old="129.00", new="119.00", pct="-7.75", verify=False, alerted=True, currency="EUR"):
    return Event(
        url=A, product_name="Drill", type=kind, old_price=Decimal(old), new_price=Decimal(new),
        change_pct=Decimal(pct), verify=verify, alerted=alerted, currency=currency,
    )


def stock_event(kind="out_of_stock", alerted=True):
    return Event(
        url=A, product_name="Drill", type=kind, old_price=Decimal("119.00"), new_price=Decimal("119.00"),
        change_pct=None, verify=False, alerted=alerted, currency="EUR",
    )


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "acme-shop" / "pricemon.db")
    yield s
    s.close()


def failing_run(store, day):
    run_id = store.start_run(f"2026-11-{day:02d}T07:00:00+00:00")
    store.add_observation(
        run_id,
        Observation(
            url=A, product_name="Drill", fetched_at=f"2026-11-{day:02d}T07:00:05+00:00",
            status=FETCH_ERROR, http_status=500, error="HTTP 500",
        ),
    )
    return run_id


def make_notifier(env, outbox, threshold=3):
    settings = Settings(operator_failure_threshold=threshold)
    return Notifier(env, settings, send_email=outbox.email, send_telegram=outbox.telegram)


# digest text


def test_event_lines():
    assert event_line(price_event()) == DOWN
    up = price_event("price_up", old="100", new="110.5", pct="10.50", currency=None)
    assert event_line(up) == "Drill at competitor-a.example: 100.00 to 110.50, up 10.5%"
    assert event_line(stock_event()) == OUT
    assert event_line(stock_event("back_in_stock")) == "Drill at competitor-a.example: back in stock"


def test_client_digest_lists_only_alerted_events():
    events = [price_event(), stock_event(), price_event("price_up", alerted=False)]
    assert client_digest(config(), events).splitlines() == ["acme-shop: 2 changes today", "", DOWN, OUT]


def test_client_digest_singular():
    assert client_digest(config(), [price_event()]).splitlines()[0] == "acme-shop: 1 change today"


def test_client_digest_without_alerted_events():
    quiet = [price_event(alerted=False)]
    assert client_digest(config(), quiet) is None
    assert client_digest(config(send_when_no_changes=True), quiet) == "acme-shop: no changes today"


def test_operator_digest_fires_once_when_the_threshold_is_crossed(store):
    cfg = config()
    first, second, third = failing_run(store, 1), failing_run(store, 2), failing_run(store, 3)
    assert operator_digest(cfg, store, first, [], 3) is None
    assert operator_digest(cfg, store, second, [], 3) is None
    assert operator_digest(cfg, store, third, [], 3).splitlines() == [
        "acme-shop: 1 item(s) need attention",
        "",
        f"FAILING {A}: fetch_error (HTTP 500), 3 failed run(s) in a row",
    ]
    fourth = failing_run(store, 4)
    assert operator_digest(cfg, store, fourth, [], 3) is None


def test_operator_digest_includes_verify_events(store):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")
    suspicious = price_event(new="12.90", pct="-90.00", verify=True, alerted=False)
    text = operator_digest(config(), store, run_id, [suspicious, price_event()], 3)
    assert text.splitlines() == [
        "acme-shop: 1 item(s) need attention",
        "",
        "VERIFY Drill at competitor-a.example: 129.00 to 12.90 EUR, down 90.0%",
    ]


# sending


def test_client_gets_one_digest_per_channel_with_the_report(store, tmp_path):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")
    report = tmp_path / "2026-11-10.xlsx"
    outbox = Outbox()
    cfg = config(email=["owner@acme.example"], telegram_chat_id="123")
    warnings = make_notifier(ENV, outbox).send(cfg, store, run_id, [price_event(), stock_event()], report)
    assert warnings == []
    assert outbox.emails == [
        (["owner@acme.example"], "Price monitor: acme-shop", f"acme-shop: 2 changes today\n\n{DOWN}\n{OUT}", report)
    ]
    assert outbox.telegrams == [("123", f"acme-shop: 2 changes today\n\n{DOWN}\n{OUT}", report)]


def test_nothing_is_sent_without_changes(store, tmp_path):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")
    outbox = Outbox()
    cfg = config(email=["owner@acme.example"])
    assert make_notifier(ENV, outbox).send(cfg, store, run_id, [], tmp_path / "r.xlsx") == []
    assert outbox.emails == [] and outbox.telegrams == []


def test_daily_report_is_sent_without_changes_when_asked(store, tmp_path):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")
    outbox = Outbox()
    cfg = config(email=["owner@acme.example"], send_when_no_changes=True)
    make_notifier(ENV, outbox).send(cfg, store, run_id, [], tmp_path / "r.xlsx")
    assert outbox.emails[0][2] == "acme-shop: no changes today"


def test_missing_smtp_settings_skip_email_with_a_warning(store, tmp_path):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")
    outbox = Outbox()
    env = {key: value for key, value in ENV.items() if key != "SMTP_PASSWORD"}
    cfg = config(email=["owner@acme.example"], telegram_chat_id="123")
    warnings = make_notifier(env, outbox).send(cfg, store, run_id, [price_event()], tmp_path / "r.xlsx")
    assert warnings == ["email skipped: SMTP settings missing in .env"]
    assert outbox.emails == []
    assert len(outbox.telegrams) == 1


def test_missing_bot_token_skips_telegram_with_a_warning(store, tmp_path):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")
    outbox = Outbox()
    env = {**ENV, "TELEGRAM_BOT_TOKEN": ""}
    cfg = config(email=["owner@acme.example"], telegram_chat_id="123")
    warnings = make_notifier(env, outbox).send(cfg, store, run_id, [price_event()], tmp_path / "r.xlsx")
    assert warnings == ["telegram skipped: TELEGRAM_BOT_TOKEN missing in .env"]
    assert outbox.telegrams == []
    assert len(outbox.emails) == 1


def test_a_failing_channel_becomes_a_warning_and_the_other_still_sends(store, tmp_path):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")
    outbox = Outbox()

    def broken_email(env, to, subject, body, attachment=None):
        raise OSError("connection refused")

    notifier = Notifier(ENV, Settings(), send_email=broken_email, send_telegram=outbox.telegram)
    cfg = config(email=["owner@acme.example"], telegram_chat_id="123")
    warnings = notifier.send(cfg, store, run_id, [price_event()], tmp_path / "r.xlsx")
    assert warnings == ["email failed: OSError: connection refused"]
    assert len(outbox.telegrams) == 1


def test_events_without_a_client_channel_give_a_warning(store, tmp_path):
    run_id = store.start_run("2026-11-10T07:00:00+00:00")
    outbox = Outbox()
    warnings = make_notifier(ENV, outbox).send(config(), store, run_id, [price_event()], tmp_path / "r.xlsx")
    assert warnings == ["client digest not sent: no email or telegram_chat_id in the client config"]


def test_operator_alert_goes_to_the_operator_without_the_report(store, tmp_path):
    run_id = failing_run(store, 1)
    outbox = Outbox()
    warnings = make_notifier(ENV, outbox, threshold=1).send(config(), store, run_id, [], tmp_path / "r.xlsx")
    assert warnings == []
    expected = f"acme-shop: 1 item(s) need attention\n\nFAILING {A}: fetch_error (HTTP 500), 1 failed run(s) in a row"
    assert outbox.emails == [(["op@example.com"], "Price monitor operator alert: acme-shop", expected, None)]
    assert outbox.telegrams == [("999", expected, None)]


def test_operator_alert_without_operator_contacts_gives_a_warning(store, tmp_path):
    run_id = failing_run(store, 1)
    outbox = Outbox()
    env = {key: value for key, value in ENV.items() if not key.startswith("OPERATOR_")}
    warnings = make_notifier(env, outbox, threshold=1).send(config(), store, run_id, [], tmp_path / "r.xlsx")
    assert warnings == ["operator alert not sent: set OPERATOR_EMAIL or OPERATOR_TELEGRAM_CHAT_ID in .env"]
    assert outbox.emails == [] and outbox.telegrams == []
```

- [ ] **Step 7: Run them to verify they fail, then write `src/pricemon/notify/__init__.py`**

Run: `.venv/Scripts/python.exe -m pytest tests/test_notify.py -q`
Expected: collection error, `ImportError: cannot import name 'Notifier' from 'pricemon.notify'`.

`src/pricemon/notify/__init__.py`:

```python
"""Digest text and dispatch to the configured channels.

One digest per run per channel, never one message per event.
"""

from pathlib import Path
from typing import Mapping

from ..config import ClientConfig, Settings, hostname
from ..models import OK, Event
from ..store import Store
from .email import send_email
from .telegram import send_telegram

_SMTP_KEYS = ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM")
_STOCK_TEXT = {"out_of_stock": "out of stock", "back_in_stock": "back in stock"}


def event_line(event: Event) -> str:
    """One line of digest text, e.g. 'Drill at shop.example: 129.00 to 119.00 EUR, down 7.8%'."""
    where = f"{event.product_name} at {hostname(event.url)}"
    if event.type in _STOCK_TEXT:
        return f"{where}: {_STOCK_TEXT[event.type]}"
    direction = "down" if event.type == "price_down" else "up"
    currency = f" {event.currency}" if event.currency else ""
    return (
        f"{where}: {event.old_price:.2f} to {event.new_price:.2f}{currency}, "
        f"{direction} {abs(event.change_pct):.1f}%"
    )


def client_digest(config: ClientConfig, events: list[Event]) -> str | None:
    """The message for the client, or None when nothing should be sent."""
    alerted = [event for event in events if event.alerted]
    if not alerted:
        return f"{config.client}: no changes today" if config.notify.send_when_no_changes else None
    noun = "change" if len(alerted) == 1 else "changes"
    lines = [f"{config.client}: {len(alerted)} {noun} today", ""]
    lines.extend(event_line(event) for event in alerted)
    return "\n".join(lines)


def operator_digest(
    config: ClientConfig, store: Store, run_id: int, events: list[Event], threshold: int
) -> str | None:
    """The message for the operator, or None when nothing needs attention.

    A failing URL is reported in the run where its failure streak reaches the
    threshold, and not again until it has recovered and failed again.
    """
    lines = []
    for obs in store.run_observations(run_id):
        if obs.status == OK:
            continue
        if store.consecutive_failures(obs.url, up_to_run_id=run_id) == threshold:
            lines.append(
                f"FAILING {obs.url}: {obs.status} ({obs.error or 'no detail'}), "
                f"{threshold} failed run(s) in a row"
            )
    lines.extend(f"VERIFY {event_line(event)}" for event in events if event.verify)
    if not lines:
        return None
    return "\n".join([f"{config.client}: {len(lines)} item(s) need attention", "", *lines])


class Notifier:
    def __init__(self, env: Mapping[str, str], settings: Settings, send_email=send_email, send_telegram=send_telegram):
        self.env = env
        self.threshold = settings.operator_failure_threshold
        self._send_email = send_email
        self._send_telegram = send_telegram

    def send(
        self, config: ClientConfig, store: Store, run_id: int, events: list[Event], report_path: Path | None
    ) -> list[str]:
        """Send the client and operator digests for a run. Returns warnings; never raises for a channel failure."""
        warnings: list[str] = []

        body = client_digest(config, events)
        if body is not None:
            subject = f"Price monitor: {config.client}"
            if config.notify.email:
                self._email(config.notify.email, subject, body, report_path, warnings)
            if config.notify.telegram_chat_id:
                self._telegram(config.notify.telegram_chat_id, body, report_path, warnings)
            if not config.notify.email and not config.notify.telegram_chat_id:
                warnings.append("client digest not sent: no email or telegram_chat_id in the client config")

        alert = operator_digest(config, store, run_id, events, self.threshold)
        if alert is not None:
            subject = f"Price monitor operator alert: {config.client}"
            operator_email = self.env.get("OPERATOR_EMAIL")
            operator_chat = self.env.get("OPERATOR_TELEGRAM_CHAT_ID")
            if operator_email:
                self._email([operator_email], subject, alert, None, warnings)
            if operator_chat:
                self._telegram(operator_chat, alert, None, warnings)
            if not operator_email and not operator_chat:
                warnings.append("operator alert not sent: set OPERATOR_EMAIL or OPERATOR_TELEGRAM_CHAT_ID in .env")
        return warnings

    def _email(self, to: list[str], subject: str, body: str, attachment: Path | None, warnings: list[str]) -> None:
        if any(not self.env.get(key) for key in _SMTP_KEYS):
            warnings.append("email skipped: SMTP settings missing in .env")
            return
        try:
            self._send_email(self.env, to, subject, body, attachment)
        except Exception as exc:
            warnings.append(f"email failed: {type(exc).__name__}: {exc}")

    def _telegram(self, chat_id: str, text: str, attachment: Path | None, warnings: list[str]) -> None:
        if not self.env.get("TELEGRAM_BOT_TOKEN"):
            warnings.append("telegram skipped: TELEGRAM_BOT_TOKEN missing in .env")
            return
        try:
            self._send_telegram(self.env, chat_id, text, attachment)
        except Exception as exc:
            warnings.append(f"telegram failed: {type(exc).__name__}: {exc}")
```

- [ ] **Step 8: Run the notify tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_notify.py tests/test_email.py tests/test_telegram.py -q`
Expected: `24 passed`.

- [ ] **Step 9: Write the failing wiring and end-to-end tests**

Append to `tests/test_runner.py`:

```python
class RecordingNotifier:
    def __init__(self, warnings=(), error=None):
        self.calls = []
        self.warnings = list(warnings)
        self.error = error

    def send(self, config, store, run_id, events, report_path):
        if self.error:
            raise self.error
        self.calls.append((config.client, run_id, list(events), str(report_path)))
        return self.warnings


def test_notifier_gets_the_events_and_the_xlsx_report(tmp_path):
    site = healthy_site()
    notifier = RecordingNotifier(warnings=["email skipped: SMTP settings missing in .env"])
    run(site, tmp_path, notifier=notifier)
    site.pages[A] = (200, page("109.00"))
    summary = run(site, tmp_path, notifier=notifier)
    assert len(notifier.calls) == 2
    client, run_id, events, report_path = notifier.calls[1]
    assert (client, run_id) == ("acme", 2)
    assert [e.type for e in events] == ["price_down"]
    assert report_path.endswith(".xlsx") and report_path == summary.report_paths[0]
    assert summary.warnings == ["email skipped: SMTP settings missing in .env"]


def test_dry_run_does_not_notify(tmp_path):
    notifier = RecordingNotifier()
    run(healthy_site(), tmp_path, notifier=notifier, dry_run=True)
    assert notifier.calls == []


def test_notifier_crash_is_a_warning_not_a_failed_run(tmp_path):
    notifier = RecordingNotifier(error=RuntimeError("digest exploded"))
    summary = run(healthy_site(), tmp_path, notifier=notifier)
    assert summary.urls_ok == 3
    assert summary.warnings == ["notification failed: RuntimeError: digest exploded"]
    assert len(summary.report_paths) == 2
```

Append to `tests/test_cli.py`:

```python
def test_run_notifies_unless_dry_run(project, capsys, monkeypatch):
    site = Site({A: (200, page("119.00")), B: (200, page("125.00"))})
    calls = []

    class FakeNotifier:
        def send(self, config, store, run_id, events, report_path):
            calls.append(run_id)
            return ["telegram skipped: TELEGRAM_BOT_TOKEN missing in .env"]

    monkeypatch.setattr(cli, "build_fetcher", lambda settings: make_fetcher(site))
    monkeypatch.setattr(cli, "build_notifier", lambda settings: FakeNotifier())
    assert cli.main(["run", "clients/acme-shop.yaml", "--dry-run"]) == 0
    assert calls == []
    capsys.readouterr()
    assert cli.main(["run", "clients/acme-shop.yaml"]) == 0
    assert calls == [2]
    assert "Warning: telegram skipped: TELEGRAM_BOT_TOKEN missing in .env" in capsys.readouterr().out


def test_dotenv_file_is_loaded(project, monkeypatch):
    import os

    (project / ".env").write_text("PRICEMON_TEST_MARKER=loaded\n", encoding="utf-8")
    try:
        assert cli.main(["validate", "clients/acme-shop.yaml"]) == 0
        assert os.environ.get("PRICEMON_TEST_MARKER") == "loaded"
    finally:
        os.environ.pop("PRICEMON_TEST_MARKER", None)
```

`tests/test_e2e.py`:

```python
"""Two full runs through the real pipeline, with a fake website and fake senders."""

from decimal import Decimal
from pathlib import Path

from helpers import ENV, Outbox, Site, make_fetcher, page
from openpyxl import load_workbook

from pricemon.config import ClientConfig, Settings
from pricemon.models import BLOCKED, OK
from pricemon.notify import Notifier
from pricemon.runner import run_client
from pricemon.store import Store

A = "https://a.example/p/drill"
B = "https://b.example/p/drill"

CONFIG = ClientConfig.model_validate(
    {
        "client": "acme",
        "products": [{"name": "Drill", "own_price": "129.00", "urls": [A, B]}],
        "notify": {"email": ["owner@acme.example"], "telegram_chat_id": "123"},
    }
)


def test_two_runs_end_to_end(tmp_path):
    site = Site({A: (200, page("119.00")), B: (200, page("125.00"))})
    outbox = Outbox()
    notifier = Notifier(
        ENV, Settings(operator_failure_threshold=1), send_email=outbox.email, send_telegram=outbox.telegram
    )

    def run():
        return run_client(
            CONFIG, site_rules={}, data_dir=tmp_path / "data", fetcher=make_fetcher(site), notifier=notifier
        )

    # Run 1: baseline. Nothing to report to anyone.
    first = run()
    assert (first.urls_ok, first.urls_failed, first.events, first.warnings) == (2, 0, [], [])
    assert outbox.emails == [] and outbox.telegrams == []

    # Run 2: A drops in price, B starts refusing requests.
    site.pages[A] = (200, page("109.00"))
    site.pages[B] = (403, "")
    second = run()
    assert (second.urls_ok, second.urls_failed, second.warnings) == (1, 1, [])

    # The client hears about the price drop, with the report attached.
    client_email, operator_email = outbox.emails
    assert client_email[0] == ["owner@acme.example"]
    assert client_email[2] == "acme: 1 change today\n\nDrill at a.example: 119.00 to 109.00 EUR, down 8.4%"
    assert Path(client_email[3]).suffix == ".xlsx" and Path(client_email[3]).exists()

    # The operator hears about the blocked page, without the report.
    assert operator_email[0] == ["op@example.com"]
    assert f"FAILING {B}: blocked (HTTP 403), 1 failed run(s) in a row" in operator_email[2]
    assert operator_email[3] is None
    assert [chat for chat, _, _ in outbox.telegrams] == ["123", "999"]

    # The database holds both runs.
    store = Store(tmp_path / "data" / "acme" / "pricemon.db")
    statuses = {o.url: o.status for o in store.run_observations(2)}
    events = store.run_events(2)
    store.close()
    assert statuses == {A: OK, B: BLOCKED}
    assert [(e.type, e.old_price, e.new_price, e.alerted) for e in events] == [
        ("price_down", Decimal("119.00"), Decimal("109.00"), True)
    ]

    # The report shows the change, and the blocked page with its last known price.
    workbook = load_workbook(second.report_paths[0])
    assert workbook["Changes"].max_row == 2
    assert workbook["Problems"].max_row == 2
    prices = {row[-1]: row for row in workbook["Prices"].iter_rows(min_row=2, values_only=True)}
    assert prices[A][5] == 109 and prices[A][8] == 119
    assert prices[B][5] == 125 and prices[B][11] == "blocked"
```

- [ ] **Step 10: Run them to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_runner.py tests/test_cli.py tests/test_e2e.py -q`
Expected: 6 failures. The runner and end-to-end tests fail with `TypeError: run_client() got an unexpected keyword argument 'notifier'`; `test_run_notifies_unless_dry_run` fails with `AttributeError` (no `build_notifier` in `pricemon.cli`); `test_dotenv_file_is_loaded` fails on the missing environment variable.

- [ ] **Step 11: Call the notifier from the runner**

In `src/pricemon/runner.py`, add `notifier=None,` to the keyword parameters of `run_client`, between `fetcher` and `dry_run`:

```python
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
```

and replace these three lines at the end of the `try` block:

```python
        report_paths = write_report(config, store, run_id, client_dir / "reports")
        summary.report_paths = [str(path) for path in report_paths]
        return summary
```

with:

```python
        report_paths = write_report(config, store, run_id, client_dir / "reports")
        summary.report_paths = [str(path) for path in report_paths]
        if notifier is not None and not dry_run:
            try:
                summary.warnings.extend(notifier.send(config, store, run_id, summary.events, report_paths[0]))
            except Exception as exc:  # a notification problem must not fail the run
                summary.warnings.append(f"notification failed: {type(exc).__name__}: {exc}")
        return summary
```

- [ ] **Step 12: Load `.env` and build the notifier in the CLI**

In `src/pricemon/cli.py`, add these imports:

```python
import os
```

```python
from dotenv import load_dotenv
```

```python
from .notify import Notifier
```

Add next to the other path constants:

```python
ENV_PATH = Path(".env")
```

Add after `build_fetcher`:

```python
def build_notifier(settings: Settings) -> Notifier:
    return Notifier(os.environ, settings)
```

In `cmd_run`, pass the notifier to `run_client`:

```python
    summary = run_client(
        config,
        site_rules=load_site_rules(SITES_DIR),
        data_dir=DATA_DIR,
        fetcher=build_fetcher(settings),
        notifier=build_notifier(settings),
        dry_run=args.dry_run,
    )
```

In `main`, load the secrets before handling the command:

```python
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
```

- [ ] **Step 13: Run the whole suite to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: `163 passed`.

- [ ] **Step 14: Commit**

```bash
git add src/pricemon/notify src/pricemon/runner.py src/pricemon/cli.py tests
git commit -m "feat: email and Telegram digests, operator alerts"
```

---

### Task 10: Browser fetcher (optional extra)

**Files:**
- Modify: `src/pricemon/fetch.py`
- Test: add tests to `tests/test_fetch.py`

**Interfaces:**
- Consumes: `classify`, `Fetcher`, `FetchResult` from `pricemon.fetch` (Task 4).
- Produces: `Fetcher.fetch(url, rules)` loads the page in headless Chromium when `rules.fetcher == "browser"`. Playwright is imported lazily, so the package still works without it. There are no retries in the browser path. `robots.txt` and the per-host rate limit apply as for HTTP.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fetch.py`:

```python
import sys
import types

BROWSER_RULES = SiteRules(domain="a.example", fetcher="browser")


def install_fake_playwright(monkeypatch, status=200, html=PAGE, launch_error=None):
    """Put a stand-in for playwright.sync_api into sys.modules. Returns the list of visited URLs."""
    visited = []

    class FakePage:
        def goto(self, url, timeout=None, wait_until=None):
            visited.append((url, timeout, wait_until))
            return types.SimpleNamespace(status=status)

        def content(self):
            return html

    class FakeBrowser:
        def new_page(self, user_agent=None):
            return FakePage()

        def close(self):
            visited.append("closed")

    class FakeChromium:
        def launch(self):
            if launch_error:
                raise launch_error
            return FakeBrowser()

    class FakeManager:
        def __enter__(self):
            return types.SimpleNamespace(chromium=FakeChromium())

        def __exit__(self, *exc):
            return False

    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: FakeManager()
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    return visited


def no_http(request):
    raise AssertionError("the browser fetcher must not use the HTTP client")


def test_browser_fetcher_returns_rendered_html(monkeypatch):
    visited = install_fake_playwright(monkeypatch)
    fetcher, _ = make(no_http, timeout_seconds=20)
    result = fetcher.fetch("https://a.example/p/1", BROWSER_RULES)
    assert (result.status, result.http_status, result.html) == (OK, 200, PAGE)
    assert visited == [("https://a.example/p/1", 20000, "networkidle"), "closed"]


def test_browser_fetcher_reports_a_blocked_page(monkeypatch):
    install_fake_playwright(monkeypatch, status=403, html="")
    fetcher, _ = make(no_http)
    result = fetcher.fetch("https://a.example/p/1", BROWSER_RULES)
    assert (result.status, result.error) == (BLOCKED, "HTTP 403")


def test_browser_failure_is_a_fetch_error(monkeypatch):
    install_fake_playwright(monkeypatch, launch_error=RuntimeError("chromium is not installed"))
    fetcher, _ = make(no_http)
    result = fetcher.fetch("https://a.example/p/1", BROWSER_RULES)
    assert result.status == FETCH_ERROR
    assert result.error == "browser: RuntimeError: chromium is not installed"


def test_browser_fetcher_without_playwright_explains_how_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)  # makes the import fail
    fetcher, _ = make(no_http)
    result = fetcher.fetch("https://a.example/p/1", BROWSER_RULES)
    assert result.status == FETCH_ERROR
    assert 'pip install "pricemon[browser]"' in result.error
    assert "playwright install chromium" in result.error
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_fetch.py -q`
Expected: 4 failures, each with `AssertionError: the browser fetcher must not use the HTTP client`, because `fetch` still takes the HTTP path.

- [ ] **Step 3: Add the browser path to `src/pricemon/fetch.py`**

In `Fetcher.fetch`, replace the last line:

```python
        return self._fetch_http(url, host, interval)
```

with:

```python
        if rules is not None and rules.fetcher == "browser":
            self._wait(host, interval)
            return self._fetch_browser(url)
        return self._fetch_http(url, host, interval)
```

Add this method to `Fetcher`, after `_fetch_http`:

```python
    def _fetch_browser(self, url: str) -> FetchResult:
        """Load the page in headless Chromium and return the rendered HTML. No retries."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return FetchResult(
                FETCH_ERROR,
                error='the browser fetcher needs Playwright: run pip install "pricemon[browser]" '
                "and then playwright install chromium",
            )
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    page = browser.new_page(user_agent=self.settings.user_agent)
                    response = page.goto(
                        url, timeout=self.settings.timeout_seconds * 1000, wait_until="networkidle"
                    )
                    html = page.content()
                    status = response.status if response is not None else 200
                finally:
                    browser.close()
        except Exception as exc:
            return FetchResult(FETCH_ERROR, error=f"browser: {type(exc).__name__}: {exc}")
        result = classify(status, html)
        if result is None:  # a 5xx response; the browser path does not retry
            return FetchResult(FETCH_ERROR, status, error=f"HTTP {status}")
        return result
```

- [ ] **Step 4: Run the whole suite to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: `167 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/pricemon/fetch.py tests/test_fetch.py
git commit -m "feat: optional headless browser fetcher"
```

---

### Task 11: Operator guide, scheduling wrapper and real-site check

**Files:**
- Create: `README.md`, `run-client.cmd`

**Interfaces:**
- Consumes: the finished `pricemon` command.
- Produces: `run-client.cmd <client>`, which runs `pricemon run clients\<client>.yaml` from the project folder and appends the output to `data\<client>-run.log`. Windows Task Scheduler calls this file.

- [ ] **Step 1: Write `run-client.cmd`**

```bat
@echo off
rem Runs one client and appends the output to data\<client>-run.log.
rem Usage: run-client.cmd acme-shop
if "%~1"=="" (
    echo Usage: run-client.cmd CLIENT_NAME
    exit /b 1
)
cd /d "%~dp0"
if not exist data mkdir data
echo ==== %DATE% %TIME% ==== >> "data\%~1-run.log"
".venv\Scripts\pricemon.exe" run "clients\%~1.yaml" >> "data\%~1-run.log" 2>&1
exit /b %ERRORLEVEL%
```

- [ ] **Step 2: Write `README.md`**

````markdown
# pricemon

Checks competitor product pages once a day for a client. Each run stores the
prices and stock status, writes an Excel and CSV report, and sends the client a
digest of what changed by email and Telegram.

## Setup

```bash
py -3.13 -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

Copy `.env.example` to `.env` and fill in the SMTP account, the Telegram bot
token and your own operator contacts. Copy `settings.example.yaml` to
`settings.yaml` only if you want to change a default. Neither file is committed.

Run the tests with `.venv/Scripts/python.exe -m pytest -q`.

## Quoting a job: will this site work?

```bash
.venv/Scripts/pricemon.exe check-url https://shop.example/product/123
```

The verdict line tells you which case you are in:

| Verdict | Meaning |
|---|---|
| `works` | The site publishes structured data. No extra work. |
| `needs a site rules file` | Write `sites/<domain>.yaml` (below), about 15 minutes per site. |
| `unsupported` | The site blocks automated requests or disallows the page. Decline it. |

If the price only appears after JavaScript runs, try `check-url <url> --browser`.
That needs the optional browser:

```bash
.venv/Scripts/python.exe -m pip install -e ".[browser]"
.venv/Scripts/playwright.exe install chromium
```

## Adding a client

1. Copy `clients/example.yaml` to `clients/<client>.yaml` and fill in the
   products and competitor URLs. The client name may use lowercase letters,
   digits and hyphens.
2. Check it: `.venv/Scripts/pricemon.exe validate clients/<client>.yaml`
3. Run `check-url` on one URL per competitor site. Write a site rules file for
   each site that needs one.
4. Do a first run without sending anything:
   `.venv/Scripts/pricemon.exe run clients/<client>.yaml --dry-run`
   Open the report in `data/<client>/reports/` and check the Problems sheet.
5. Schedule it (below).

The first run is a baseline: it records prices and sends no alerts. Alerts
start with the second run.

## Site rules

`sites/<anything>.yaml`, one file per hostname. `www.` is ignored;
`shop.example.com` and `example.com` are different sites.

```yaml
domain: competitor-b.example
fetcher: http                  # or browser
min_interval_seconds: 5        # optional
skip_structured_data: false    # true if the site's own structured data is wrong
title:    { css: "h1.product-title" }
price:    { css: "span.price" }             # add attr: "content" to read an attribute
currency: EUR                               # or { css: "...", attr: "..." }
decimal_separator: ","                      # optional
stock:
  css: ".availability"
  in_stock_text: ["In stock"]
  out_of_stock_text: ["Sold out"]
```

Find the selectors with the browser's "Inspect" tool, then confirm with
`check-url`.

## Scheduling

One Windows Task Scheduler task per client. Replace the path and client name:

```bat
schtasks /Create /TN "pricemon-acme-shop" /SC DAILY /ST 07:00 /TR "\"C:\path\to\price-monitor\run-client.cmd\" acme-shop"
```

Each run appends its summary to `data\<client>-run.log`. To remove the task:

```bat
schtasks /Delete /TN "pricemon-acme-shop"
```

The PC must be on at the scheduled time.

## Commands

| Command | What it does |
|---|---|
| `pricemon run <client.yaml> [--dry-run]` | Check every URL, store results, write the report, send digests. `--dry-run` sends nothing. |
| `pricemon check-url <url> [--browser]` | Test one page without storing anything. |
| `pricemon report <client.yaml> [--date YYYY-MM-DD]` | Rebuild a day's report from stored data. |
| `pricemon validate <client.yaml>` | Check a config without fetching. |

`run` exits with 0 when the run completed, even if some pages failed, and with
1 for an invalid config or a fatal error.

## What gets sent

- **Client:** one digest per run and channel when something changed, with the
  Excel report attached. Set `send_when_no_changes: true` to send it daily.
- **You:** an alert when a page has failed `operator_failure_threshold` runs in
  a row (default 3), and when a price moved by `verify_threshold_pct` or more
  (default 70%). Such a jump usually means the site changed its layout, so the
  client is not alerted until you have checked it.

## Limits

- Public product pages only: no logins and no personal data.
- `robots.txt` is respected.
- Bot protection is never bypassed. A blocked site is reported as unsupported.
- Check a site's terms of use before taking a job that targets it.
````

- [ ] **Step 3: Run the whole suite one more time**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: `167 passed`.

- [ ] **Step 4: Check the wrapper script**

Run (PowerShell or cmd): `.\run-client.cmd`
Expected: prints `Usage: run-client.cmd CLIENT_NAME` and exits with code 1.

- [ ] **Step 5: Check against real product pages**

This step uses the network and needs the user. Ask the user for two or three real product page URLs from shops of the kind they want to sell to, then for each one run:

```bash
.venv/Scripts/pricemon.exe check-url <url>
```

Expected: a `Verdict:` line for each. Report the verdicts to the user. For a page whose verdict is `works`, confirm with the user that the printed price matches what the page shows in a browser. Do not try to get around a `blocked` or `disallowed` verdict.

If a real page exposes an extraction bug, save its HTML as a new file in `tests/fixtures/`, add a test for it in `tests/test_extract.py`, and fix the extractor with a failing test first.

- [ ] **Step 6: Commit**

```bash
git add README.md run-client.cmd
git commit -m "docs: operator guide and scheduling wrapper"
```
