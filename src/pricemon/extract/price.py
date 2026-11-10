"""Turning price text into a Decimal."""

import re
from decimal import Decimal, InvalidOperation

# Characters that may act as a thousands separator between digit groups:
# space, no-break space, narrow no-break space, thin space. Not newline or tab.
_THOUSANDS_SPACE = r"[ \u00a0\u202f\u2009]"
# The first number in the text. A space joins digits only as a thousands
# separator in the integer part ("1 299,50"), never between two separate numbers.
_NUMBER = re.compile(
    r"\d{1,3}(?:" + _THOUSANDS_SPACE + r"\d{3})+(?!\d)(?:[.,]\d+)?|\d+(?:[.,]\d+)*"
)
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
        before, after = cleaned.split(separator)
        # A thousands group is 1-3 leading digits (not all zeros) and exactly three after.
        is_thousands = len(after) == 3 and 1 <= len(before) <= 3 and before.strip("0") != ""
        if is_thousands:
            return cleaned.replace(separator, "")
        return cleaned.replace(separator, ".")
    return cleaned


def count_numbers(text: str | None) -> int:
    """How many separate numbers the text contains."""
    return len(_NUMBER.findall(text or ""))


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
    cleaned = re.sub(_THOUSANDS_SPACE, "", match.group())
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
    if not result.is_finite() or result <= 0:
        raise PriceError(f"price is not a positive number: {value!r}")
    return result
