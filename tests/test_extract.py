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
