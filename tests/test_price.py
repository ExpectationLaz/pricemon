from decimal import Decimal

import pytest

from pricemon.extract.price import PriceError, parse_price, parse_structured_price


@pytest.mark.parametrize(
    "text, expected",
    [
        ("129.00", "129.00"),
        ("1.299,00 €", "1299.00"),
        ("$1,299.00", "1299.00"),
        ("1 299", "1299"),
        ("1 299,50 kr", "1299.50"),
        ("1.299", "1299"),
        ("12.50", "12.50"),
        ("12,5", "12.5"),
        ("1,299,000", "1299000"),
        ("€ 19,99", "19.99"),
        ("From 12,50 € to 15,00 €", "12.50"),
        ("Price: 45.", "45"),
    ],
)
def test_parse_price(text, expected):
    assert parse_price(text) == Decimal(expected)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("12,50 15,00", "12.50"),
        ("Was 20.00 15.00", "20.00"),
        ("12.99 2 left", "12.99"),
        ("10 20", "10"),
        ("12.99 200 left", "12.99"),
        ("1 299 000,50", "1299000.50"),
        ("0.125", "0.125"),
        ("0,125", "0.125"),
        ("1234.567", "1234.567"),
        ("1234,567", "1234.567"),
    ],
)
def test_parse_price_does_not_merge_or_misread_numbers(text, expected):
    assert parse_price(text) == Decimal(expected)


def test_decimal_separator_override():
    assert parse_price("1.299", decimal_separator=".") == Decimal("1.299")
    assert parse_price("1,299", decimal_separator=",") == Decimal("1.299")
    assert parse_price("1.299,50", decimal_separator=",") == Decimal("1299.50")


@pytest.mark.parametrize("text", ["", None, "free", "Call us", "0", "0,00", "-5.00"])
def test_parse_price_rejects(text):
    with pytest.raises(PriceError):
        parse_price(text)


@pytest.mark.parametrize(
    "value, expected",
    [
        (129, "129"),
        (129.5, "129.5"),
        ("129.000", "129.000"),
        (" 24.99 ", "24.99"),
        ("1.299,00", "1299.00"),
    ],
)
def test_parse_structured_price(value, expected):
    assert parse_structured_price(value) == Decimal(expected)


@pytest.mark.parametrize("value", [0, "0.00", None, True, "", "n/a", [], -3, float("nan"), float("inf")])
def test_parse_structured_price_rejects(value):
    with pytest.raises(PriceError):
        parse_structured_price(value)
