"""Step 3: per-site CSS rules from sites/<domain>.yaml."""

from selectolax.lexbor import LexborHTMLParser as HTMLParser

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
