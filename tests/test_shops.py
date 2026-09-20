"""실제 샵(TWE / Whiskybase Shop / Winemoa)에서 확인한 마크업 구조를 축약한 픽스처 기반 테스트."""

from pathlib import Path

import pytest

from whiskywatch import parsers
from whiskywatch.config import Config, ShopConfig, load_config
from whiskywatch.fetchers import BlockedError, FetchError
from whiskywatch.monitor import collect_shop, evaluate, format_alert, run_once
from whiskywatch.normalize import analyze
from whiskywatch.notify import TelegramNotifier
from whiskywatch.store import Baseline, History, State

FX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).parent.parent
RATES = {"GBP": 1800.0, "EUR": 1500.0, "USD": 1400.0, "KRW": 1.0}
WL = ["springbank", "springbank local barley", "hazelburn", "longrow", "kilkerran"]


def read(n):
    return (FX / n).read_text(encoding="utf-8")


TWE_SEL = {
    "card": "li.product-grid__item", "link": "a.product-card",
    "title": "p.product-card__name, p.product-card__meta", "price": "p.product-card__price",
}


def test_twe_selectors_join_name_and_meta_and_ignore_menu_links():
    c = parsers.parse_listing(read("twe_brand.html"), "https://www.thewhiskyexchange.com/b/40/springbank", currency="GBP", selectors=TWE_SEL)
    assert len(c) == 3
    assert c[0].title == "Springbank 175th Anniversary 12 Year Old 70cl / 46%"
    assert c[0].price == 450 and c[0].currency == "GBP"
    assert c[0].url == "https://www.thewhiskyexchange.com/p/4681/springbank-175th-anniversary-12-year-old"
    assert c[1].price == 3750  # "£3,750" 천 단위 쉼표
    assert c[2].in_stock is False  # "Out of stock"
    a = analyze(c[0].title, watchlist=WL, allowed_volumes_ml=[700, 750, 1000])
    assert a.age == 12 and a.volume_ml == 700 and a.abv == 46


def test_lightspeed_preset_whiskybase():
    c = parsers.parse_listing(read("lightspeed_brand.html"), "https://shop.whiskybase.com/us/brands/springbank/", currency="EUR", preset="lightspeed")
    assert [x.price for x in c] == [675.0, 57.5]  # 두 번째는 취소선(old-price) 제외
    assert c[0].title == "Springbank 10-year-old - Local Barley - 20/188"
    a = analyze(c[0].title, watchlist=WL, allowed_volumes_ml=[700, 750, 1000])
    assert a.age == 10 and a.is_local_barley and a.volume_assumed


def test_winemoa_shopify_korean_titles_and_filters():
    cands = parsers.parse_shopify_products(read("winemoa_products.json"), "https://winemoa.de", "EUR")
    got = {}
    for c in cands:
        a = analyze(c.title, watchlist=WL, allowed_volumes_ml=[700, 750, 1000])
        if a:
            got[a.key] = (c.price, c.in_stock)
    assert got == {
        "springbank|10|local-barley|y2025|700ml": (590.0, True),
        "springbank|15|700ml": (315.0, True),
        "springbank|10|700ml": (185.0, False),
        "hazelburn|10|700ml": (195.0, True),
        "kilkerran|12|700ml": (115.0, True),
        "longrow|nas|y2025|700ml": (265.0, True),
    }  # 쿠폰, 블렌디드, 묶음(+), 무관한 위스키는 제외


def test_all_in_shop_adds_no_tax_or_vat_strip():
    shop = ShopConfig(id="w", name="Winemoa", currency="EUR", all_in=True, strip_vat=0.19, type="shopify",
                      listing_urls=["https://winemoa.de/products.json?page={page}"])
    cfg = Config(watchlist=WL, shops=[shop])
    cand = parsers.parse_shopify_products(read("winemoa_products.json"), "https://winemoa.de", "EUR")[1]  # 15년 315 EUR
    obs = evaluate(shop, cand, cfg, RATES)
    assert obs.per_bottle_krw == 315 * 1500  # 표시가 그대로 (VAT 제외/세금 가산 없음)
    assert obs.landed.tax_krw == 0
    msg = format_alert(obs, Baseline(600_000, 8, "k", "history"), -21.0, RATES)
    assert "포함가" in msg and "세금: 별도 없음" in msg


# ---------------------------------------------------------------- collect_shop


class MapFetcher:
    def __init__(self, pages):
        self.pages, self.urls = pages, []

    def get(self, url):
        self.urls.append(url)
        v = self.pages[url]
        if isinstance(v, Exception):
            raise v
        return v


def _twe_shop(urls, **kw):
    return ShopConfig(id="twe", name="TWE", currency="GBP", listing_urls=urls, selectors=TWE_SEL, **kw)


def test_listing_pagination_stops_when_no_new_products():
    page = read("twe_brand.html")
    other = page.replace("/p/4681/", "/p/7001/").replace("/p/999/", "/p/7002/").replace("/p/555/", "/p/7003/")
    base = "https://twe.example/b/40/springbank?pg={page}"
    f = MapFetcher({base.replace("{page}", "1"): page, base.replace("{page}", "2"): other, base.replace("{page}", "3"): other})
    cands = collect_shop(_twe_shop([{"url": base, "pages": 8}]), Config(watchlist=WL, shops=[]), f)
    assert len(cands) == 6
    assert len(f.urls) == 3  # 3쪽이 2쪽과 같은 내용 -> 4쪽 이후는 요청하지 않음


def test_listing_partial_failure_is_tolerated_but_total_failure_raises():
    ok, bad = "https://twe.example/b/1", "https://twe.example/b/2"
    cfg = Config(watchlist=WL, shops=[])
    f = MapFetcher({ok: read("twe_brand.html"), bad: FetchError("HTTP 404")})
    assert len(collect_shop(_twe_shop([ok, bad]), cfg, f)) == 3

    f = MapFetcher({ok: BlockedError("HTTP 403"), bad: FetchError("HTTP 404")})
    with pytest.raises(BlockedError):
        collect_shop(_twe_shop([ok, bad]), cfg, f)


def test_past_the_end_404_on_later_page_is_not_an_error():
    base = "https://twe.example/b/1?pg={page}"
    f = MapFetcher({base.replace("{page}", "1"): read("twe_brand.html"), base.replace("{page}", "2"): FetchError("HTTP 404")})
    assert len(collect_shop(_twe_shop([{"url": base, "pages": 5}]), Config(watchlist=WL, shops=[]), f)) == 3


def test_shopify_listing_from_products_json():
    shop = ShopConfig(id="w", name="W", currency="EUR", type="shopify", all_in=True,
                      listing_urls=[{"url": "https://winemoa.de/products.json?limit=250&page={page}", "pages": 4}])
    empty = '{"products": []}'
    f = MapFetcher({
        "https://winemoa.de/products.json?limit=250&page=1": read("winemoa_products.json"),
        "https://winemoa.de/products.json?limit=250&page=2": empty,
    })
    cands = collect_shop(shop, Config(watchlist=WL, shops=[]), f)
    assert len(cands) == 10 and cands[0].url == "https://winemoa.de/products/sb10-lb-2025"
    assert len(f.urls) == 2


# ---------------------------------------------------------------- 실제 config.yaml


def test_shipped_config_is_consistent():
    cfg = load_config(ROOT / "config.yaml")
    by_id = {s.id: s for s in cfg.shops}
    assert {"twe", "whiskybase", "winemoa"} <= set(by_id)
    for s in cfg.shops:
        assert s.listing_urls or s.search_url, s.id
    assert by_id["winemoa"].all_in and by_id["winemoa"].type == "shopify"
    assert by_id["twe"].selectors["card"] == "li.product-grid__item"
    # 검색 페이지를 robots.txt 로 막은 샵은 검색 URL 을 쓰지 않는다
    for sid in ("twe", "mom"):
        assert "/search" not in (by_id[sid].search_url or "")


# ---------------------------------------------------------------- Telegram 오류 사유


class _R:
    def __init__(self, code, body=None):
        self.status_code, self._b = code, body

    def json(self):
        if self._b is None:
            raise ValueError
        return self._b


def test_telegram_reports_reason_without_leaking_token():
    n = TelegramNotifier("SECRET-TOKEN", "1", http_post=lambda u, p: _R(401, {"ok": False, "description": "Unauthorized"}))
    assert not n.send("hi")
    assert n.last_error == "HTTP 401 Unauthorized" and "SECRET" not in n.last_error
    n = TelegramNotifier("t", "1", http_post=lambda u, p: _R(400, {"description": "Bad Request: chat not found"}))
    n.send("hi")
    assert "chat not found" in n.last_error
