"""The extraction chain: JSON-LD, then meta tags, then site rules."""

from selectolax.lexbor import LexborHTMLParser as HTMLParser

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
