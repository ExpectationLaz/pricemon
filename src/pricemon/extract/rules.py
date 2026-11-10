"""Step 3: per-site CSS rules from sites/<domain>.yaml."""

from ..config import Selector, SiteRules
from ..models import Partial
from .common import HTMLParser, normalize_currency
from .price import PriceError, count_numbers, parse_price


def _clean(text: str | None) -> str:
    """Collapse all whitespace runs to single spaces and trim."""
    return " ".join((text or "").split())


def _node_text(node) -> str:
    """Text of a node with a space between text nodes, whitespace-normalised."""
    return _clean(node.text(separator=" "))


def _select(tree: HTMLParser, selector: Selector) -> str | None:
    node = tree.css_first(selector.css)
    if node is None:
        return None
    value = _clean(node.attributes.get(selector.attr)) if selector.attr else _node_text(node)
    return value or None


def extract_rules(tree: HTMLParser, rules: SiteRules) -> Partial:
    partial = Partial()
    if rules.title:
        partial.title = _select(tree, rules.title)
    if rules.price:
        raw = _select(tree, rules.price)
        if raw:
            if count_numbers(raw) > 1:
                partial.note = (
                    f"price text '{raw}' has more than one number; use a narrower selector or an attribute"
                )
            else:
                try:
                    partial.price = parse_price(raw, rules.decimal_separator)
                except PriceError:
                    partial.note = f"cannot parse price text '{raw}'"
    if isinstance(rules.currency, str):
        partial.currency = normalize_currency(rules.currency)
    elif rules.currency is not None:
        partial.currency = normalize_currency(_select(tree, rules.currency))
    if rules.stock:
        node = tree.css_first(rules.stock.css)
        if node is not None:
            text = _node_text(node).lower()
            # Out-of-stock phrases are checked first: "not in stock" contains "in stock".
            if any(_clean(phrase).lower() in text for phrase in rules.stock.out_of_stock_text):
                partial.in_stock = False
            elif any(_clean(phrase).lower() in text for phrase in rules.stock.in_stock_text):
                partial.in_stock = True
    return partial
