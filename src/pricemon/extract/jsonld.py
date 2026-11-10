"""Step 1: schema.org Product data in JSON-LD blocks."""

import json

from ..models import Partial
from .common import HTMLParser, normalize_currency, parse_availability
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
