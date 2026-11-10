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


def test_invalid_css_selector_in_site_rules_is_a_config_error(tmp_path):
    sites = tmp_path / "sites"
    sites.mkdir()
    write(sites, "b.yaml", "domain: b.example\nprice: {css: 'h1['}\n")
    with pytest.raises(ConfigError, match="invalid CSS selector"):
        load_site_rules(sites)


def test_invalid_stock_selector_in_site_rules_is_a_config_error(tmp_path):
    sites = tmp_path / "sites"
    sites.mkdir()
    write(sites, "b.yaml", "domain: b.example\nstock: {css: 'div[', in_stock_text: [In stock]}\n")
    with pytest.raises(ConfigError, match="invalid CSS selector"):
        load_site_rules(sites)


@pytest.mark.parametrize(
    "line",
    ["retries: -1", "min_interval_seconds: -5", "timeout_seconds: 0", "operator_failure_threshold: 0"],
)
def test_settings_reject_out_of_range_values(tmp_path, line):
    with pytest.raises(ConfigError):
        load_settings(write(tmp_path, "settings.yaml", line + "\n"))


def test_site_rules_reject_a_negative_interval(tmp_path):
    sites = tmp_path / "sites"
    sites.mkdir()
    write(sites, "b.yaml", "domain: b.example\nmin_interval_seconds: -1\n")
    with pytest.raises(ConfigError, match="min_interval_seconds"):
        load_site_rules(sites)
