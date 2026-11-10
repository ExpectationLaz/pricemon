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
