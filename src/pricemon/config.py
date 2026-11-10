"""Loading and validation of client configs, site rules and settings."""

from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from selectolax.lexbor import LexborHTMLParser

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


def _check_css(css: str) -> str:
    try:
        LexborHTMLParser("").css(css)
    except Exception:
        raise ValueError(f"invalid CSS selector: {css}") from None
    return css


class Selector(_Strict):
    css: str
    attr: str | None = None

    _valid_css = field_validator("css")(_check_css)


class StockRule(_Strict):
    css: str
    in_stock_text: list[str] = []
    out_of_stock_text: list[str] = []

    _valid_css = field_validator("css")(_check_css)


class SiteRules(_Strict):
    domain: str
    fetcher: Literal["http", "browser"] = "http"
    min_interval_seconds: float | None = Field(default=None, ge=0)
    skip_structured_data: bool = False
    title: Selector | None = None
    price: Selector | None = None
    currency: str | Selector | None = None
    decimal_separator: Literal[".", ","] | None = None
    stock: StockRule | None = None


class Settings(_Strict):
    user_agent: str = "pricemon/0.1"
    min_interval_seconds: float = Field(default=5, ge=0)
    timeout_seconds: float = Field(default=20, gt=0)
    retries: int = Field(default=2, ge=0)
    respect_robots_txt: bool = True
    operator_failure_threshold: int = Field(default=3, ge=1)


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
