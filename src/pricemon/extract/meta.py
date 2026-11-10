"""Step 2: Open Graph style product meta tags."""

from ..models import Partial
from .common import HTMLParser, normalize_currency, parse_availability
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
