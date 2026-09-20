from pathlib import Path

import pytest

from whiskywatch import parsers
from whiskywatch.parsers import to_number

FX = Path(__file__).parent / "fixtures"


def read(name):
    return (FX / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("€ 1.299,00", 1299.0), ("1,299.00", 1299.0), ("95.5", 95.5), ("95,00", 95.0),
        ("£119.95", 119.95), ("1.299", 1299.0), ("1,299,000", 1299000.0), ("0,70", 0.7),
    ],
)
def test_to_number(raw, expected):
    assert to_number(raw) == expected


def test_jsonld_itemlist():
    cands = parsers.parse_listing(read("jsonld_listing.html"), "https://shop.example/search?q=springbank", currency="GBP")
    by_title = {c.title: c for c in cands}
    lb = by_title["Springbank 10 Year Old Local Barley 2026 Release 70cl"]
    assert lb.price == 119.95 and lb.currency == "GBP" and lb.in_stock is True
    assert lb.url == "https://shop.example/p/springbank-10-local-barley"
    assert by_title["Springbank 15 Year Old 46% 70cl"].in_stock is False
    assert by_title["Longrow Red 11 Year Old Cabernet Sauvignon 70cl"].price == 92.5
    assert len(cands) == 6


def test_magento_preset_uses_special_price_not_old_price():
    cands = parsers.parse_listing(
        read("magento_listing.html"), "https://shop.whiskybase.example/us/search", currency="EUR", preset="magento"
    )
    lb = next(c for c in cands if "Local Barley" in c.title)
    assert lb.price == 129.50 and lb.url.endswith("springbank-10-year-old-local-barley.html")
    hz = next(c for c in cands if "Hazelburn" in c.title)
    assert hz.price == 99.0 and hz.in_stock is False


def test_heuristic_cards_skip_strikethrough_and_per_litre_prices():
    cands = parsers.parse_listing(read("heuristic_listing.html"), "https://shop.example/search", currency="EUR")
    k = next(c for c in cands if "Kilkerran" in c.title)
    assert k.price == 69.90  # 취소선 79,90 / 리터당 99,86 제외
    s = next(c for c in cands if "Springbank 18" in c.title)
    assert s.price == 1049.0  # 리터당 1.498,57 제외
    assert k.currency == "EUR"


def test_shopify_products_json():
    cands = parsers.parse_shopify_products(read("shopify_products.json"), "https://shop.example", "EUR")
    assert [c.price for c in cands] == [109.0, 88.0, 42.0]
    assert [c.in_stock for c in cands] == [True, False, True]
    assert cands[0].url == "https://shop.example/products/springbank-12-cs-b24"


def test_product_page_meta_fallback():
    c = parsers.parse_product_page(read("product_page.html"), "https://shop.example/p/hazelburn-10", "GBP")
    assert c.title.startswith("Hazelburn 10") and c.price == 76.25 and c.currency == "GBP"


def test_garbage_html_returns_empty():
    assert parsers.parse_listing("<html><body>nothing</body></html>", "https://x.example", currency="GBP") == []
