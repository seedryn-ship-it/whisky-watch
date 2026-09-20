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
        "longrow|nas|100-proof|y2025|700ml": (265.0, True),
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
    assert {"twe", "whiskybase"} <= set(by_id) and "winemoa" not in by_id
    for s in cfg.shops:
        assert s.listing_urls or s.search_url, s.id
    for sid in ("wio", "hedonism", "innout", "galore"):
        assert by_id[sid].type == "shopify" and by_id[sid].enabled and by_id[sid].listing_urls, sid
    assert "stillspirit" not in by_id  # 한국이 배송 국가 목록에 없음
    assert by_id["wio"].ship_base == 37.95 and by_id["galore"].currency == "NZD"
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


# ---------------------------------------------------------------- Nickolls & Perks (WooCommerce/WoodMart) + Crawl-delay


def test_woodmart_preset_uses_ex_vat_price_and_class_based_stock():
    cands = parsers.parse_listing(read("nickolls_producer.html"), "https://nickollsandperks.com/producer/springbank/",
                                  currency="GBP", preset="woodmart")
    by_url = {c.url.rsplit("/", 2)[-2]: c for c in cands}
    assert len(cands) == 5  # 상단 메뉴 링크는 카드가 아니므로 제외
    ten = by_url["springbank-10-year-old-103398931211"]
    assert ten.price == 47.50 and ten.in_stock is None  # VAT 제외 가격(£47.50) 사용, £57.00 아님
    assert "70cl" in ten.title and "46.00%" in ten.title  # 용량/도수가 제목에 붙는다
    society = next(c for c in cands if "Society" in c.title)
    assert society.price == 495.0  # 세금 제외 표기가 없는 상품은 표시가 그대로
    fifteen = by_url["springbank-15-year-old-105335001211"]
    assert fifteen.price == 69.96 and fifteen.in_stock is False  # '품절' 글자 없이 클래스(outofstock)만 있는 품절 상품
    assert by_url["springbank-12-cs-1"].price == 1665.83  # 천 단위 쉼표


def test_woodmart_items_analyze_with_volume_and_abv():
    cands = parsers.parse_listing(read("nickolls_producer.html"), "https://nickollsandperks.com/", currency="GBP",
                                  preset="woodmart")
    got = {}
    for c in cands:
        a = analyze(c.title, watchlist=WL, allowed_volumes_ml=[700, 750, 1000])
        if a:
            got[a.title] = a
    assert not any("Open Day" in t for t in got)  # 35cl 미니 병은 용량으로 제외
    cs = next(a for t, a in got.items() if "Cask Strength" in t)
    assert cs.volume_ml == 700 and not cs.volume_assumed and cs.abv == 55.3 and "a55" in cs.key


def test_crawl_delay_from_robots_is_honored_and_capped():
    import urllib.robotparser

    from whiskywatch.fetchers import RequestsFetcher

    f = RequestsFetcher("Mozilla/5.0 test", delay=(0.0, 0.0))
    for text, want in (("User-agent: *\nCrawl-delay: 10\n", 10.0), ("User-agent: *\nCrawl-delay: 45\n", 45.0),
                       ("User-agent: *\nDisallow: /x\n", 0.0)):
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(text.splitlines())
        f._robots["https://shop.example"] = rp
        assert f._wait_seconds("https://shop.example/a") == want
    # 10분(600초)처럼 30분 주기 모니터링과 맞지 않는 Crawl-delay 는 요청하지 않고 명확한 오류로 알린다
    rp = urllib.robotparser.RobotFileParser()
    rp.parse("User-agent: *\nCrawl-delay: 600\n".splitlines())
    f._robots["https://slow.example"] = rp
    with pytest.raises(FetchError, match="Crawl-delay"):
        f._wait_seconds("https://slow.example/a")


def test_shipped_config_has_nickolls_with_crawl_friendly_pages():
    cfg = load_config(ROOT / "config.yaml")
    n = next(s for s in cfg.shops if s.id == "nickolls")
    assert n.preset == "woodmart" and n.currency == "GBP" and n.strip_vat == 0 and n.enabled
    urls = [u["url"] if isinstance(u, dict) else u for u in n.listing_urls]
    assert any("/producer/springbank/page/{page}/" in u for u in urls) and any("/producer/kilkerran/" in u for u in urls)


# ---------------------------------------------------------------- '수동 확인' 그룹에서 추가한 4곳 (실제 config.yaml 의 selectors 로 검사)


def _shop(sid):
    return next(s for s in load_config(ROOT / "config.yaml").shops if s.id == sid)


def _parse(sid, fixture, url):
    s = _shop(sid)
    return s, parsers.parse_listing(read(fixture), url, currency=s.currency, selectors=s.selectors)


def _an(title):
    return analyze(title, watchlist=WL, allowed_volumes_ml=[700, 750, 1000])


def test_rvw_prices_with_split_decimals_and_cart_button_stock():
    s, c = _parse("rvw", "rvw_listing.html", "https://rarevintagewhisky.nl/en/Springbank")
    assert len(c) == 5  # 상단 메뉴 링크는 카드가 아님
    by = {x.title: x for x in c}
    peat = by["Springbank 29Y The Peat Bog 1990 Glenscoma 47.5%"]
    assert peat.price == 435.0 and peat.currency == "EUR" and peat.in_stock is None  # "€435,<span>00</span>"
    assert peat.url == "https://rarevintagewhisky.nl/en/Springbank-29Y-The-Peat-Bog-1990-Glenscoma-47.5"
    assert by["Springbank Local Barley 13Y 54.1% 2023"].in_stock is False  # 장바구니 버튼 없음 = 품절
    assert by["Springbank 15Y 2023 23/94 46.0%"].in_stock is None and by["Springbank 15Y 2023 23/94 46.0%"].price == 289.5
    assert s.skip_sold_out_history and s.strip_vat == 0


def test_age_written_as_29Y_and_100_proof_are_understood():
    a = _an("Springbank 29Y The Peat Bog 1990 Glenscoma 47.5%")
    assert a.age == 29 and a.key == "springbank|29|y1990|700ml"
    assert _an("Springbank 10Y 100 Proof 2006 57.0%").key == "springbank|10|100-proof|y2006|700ml"
    assert _an("Kilkerran 12Y 46.0%").age == 12
    assert _an("Sample 3cl Springbank 10Y Amontillado 55.0%") is None  # 3cl 샘플은 제외


def test_rvw_pagination_by_index_and_stop_at_empty_page():
    s = _shop("rvw")
    base = "https://rarevintagewhisky.nl/website/index.php?ProductCategory=32013969&Index={page}"
    page1 = read("rvw_listing.html")
    page2 = page1.replace("Springbank-29Y", "Springbank-30Y").replace("Springbank-Local", "Springbank-LB").replace(
        "Springbank-10Y", "Springbank-11Y").replace("Springbank-15Y", "Springbank-16Y").replace("Sample-3cl", "Sample-5cl")
    f = MapFetcher({base.replace("{page}", "1"): page1, base.replace("{page}", "2"): page2,
                    base.replace("{page}", "3"): "<html><body></body></html>"})
    shop = ShopConfig(id="rvw", name="rvw", currency="EUR", listing_urls=[{"url": base, "pages": 3}], selectors=s.selectors)
    cands = collect_shop(shop, Config(watchlist=WL, shops=[]), f)
    assert len(cands) == 10 and len(f.urls) == 3


def test_cadenhead_card_price_and_class_based_stock():
    s, c = _parse("cadenhead", "cadenhead_listing.html", "https://cadenhead.shop/distillery/springbank/")
    assert [x.price for x in c] == [94.17, 1250.0, 70.83]  # "1,250.00" 천 단위
    assert [x.in_stock for x in c] == [None, False, None]  # outofstock 클래스
    assert c[0].url == "https://www.cadenhead.shop/product/springbank-12yo-2011-55-9-abv-70cl-single-malt-whisky/"
    a = _an(c[0].title)
    assert a.age == 12 and a.volume_ml == 700 and a.abv == 55.9
    assert s.allow_empty


def test_cadenhead_empty_category_is_fine_but_other_shops_still_fail_when_empty():
    s = _shop("cadenhead")
    empty = read("cadenhead_empty.html")
    assert parsers.parse_listing(empty, "https://cadenhead.shop/distillery/springbank/", currency="GBP", selectors=s.selectors) == []
    # 한 회차 전체를 돌려도 cadenhead 가 0건이라는 이유로 오류/점검 알림이 나지 않는다
    urls = {"https://cadenhead.shop/distillery/springbank/page/1/": empty,
            "https://cadenhead.shop/distillery/hazelburn-springbank-distillery/": empty,
            "https://cadenhead.shop/distillery/kilkerran/": empty}
    f = MapFetcher(urls)
    cfg = Config(watchlist=WL, shops=[s])
    from whiskywatch.store import History, State
    import tempfile
    d = Path(tempfile.mkdtemp())
    sent = []

    class N:
        def send(self, t):
            sent.append(t)
            return True

    summary = run_once(cfg, History(d / "h.jsonl"), State(d / "s.json"), N(), _Pool(f), rates=RATES, dry_run=True)
    assert "cadenhead" not in summary.shops_failed and summary.shops_ok == 1
    s2 = ShopConfig(**{**s.__dict__, "allow_empty": False})
    summary = run_once(Config(watchlist=WL, shops=[s2]), History(d / "h2.jsonl"), State(d / "s2.json"), N(), _Pool(f), rates=RATES, dry_run=True)
    assert "cadenhead" in summary.shops_failed


class _Pool:
    def __init__(self, f):
        self.f = f

    def get(self, kind):
        return self.f


def test_dunkeld_search_results_price_stock_and_filters():
    s, c = _parse("dunkeld", "dunkeld_search.html", "https://www.dunkeldwhiskybox.co.uk/search?q=springbank")
    assert len(c) == 7
    by = {x.title: x for x in c}
    assert by["Springbank 100 Proof Aged 5 Years"].price == 51.0  # 숨김 텍스트 때문에 가격이 두 번 나와도 51
    assert by["Springbank 10 Year Old"].in_stock is False and by["Springbank 100 Proof Aged 5 Years"].in_stock is None
    assert by["Longrow Peated"].url == "https://www.dunkeldwhiskybox.co.uk/p/longrow-peated"
    keys = {x.title: (_an(x.title) or None) for x in c}
    assert keys["Glen Scotia 12 Year Old Single Malt Scotch Whisky / Thompson Bros"] is None
    assert keys["Kilkerran 12 Year Old"].key == "kilkerran|12|700ml"
    assert keys["Longrow Peated"].family_key.startswith("longrow")


def test_whiskyfass_german_volume_abv_and_thousands():
    s, c = _parse("whiskyfass", "whiskyfass_listing.html", "https://whiskyfass.de/springbank?af=50")
    assert [x.price for x in c] == [148.88, 1488.88, 248.88, 648.88, 33.88]  # "1.488,88 €"
    assert all(x.in_stock is None for x in c)  # 'Knapper Lagerbestand'(재고 적음)는 품절이 아님
    got = {}
    for x in c:
        a = _an(x.title)
        if a:
            got[a.title.split(" Inhalt")[0]] = a
    assert not any("Darkness" in t for t in got)  # 0,50 l 는 허용 용량이 아니라 제외
    assert not any("Blended Malt" in t for t in got)
    cs = next(a for t, a in got.items() if "Cask Strength" in t)
    assert cs.age == 12 and cs.volume_ml == 700 and not cs.volume_assumed and cs.abv == 55.5
    lb = next(a for t, a in got.items() if "Local Barley" in t)
    assert lb.age == 8 and lb.is_local_barley and lb.volume_ml == 700


def test_shipped_config_lists_the_four_manual_group_shops():
    by = {s.id: s for s in load_config(ROOT / "config.yaml").shops}
    for sid in ("cadenhead", "dunkeld", "whiskyfass", "rvw"):
        assert by[sid].enabled and by[sid].selectors.get("card") and by[sid].listing_urls, sid
        assert by[sid].strip_vat == 0 and by[sid].ship_base > 0  # 확인 전 세금은 빼지 않고(보수적) 배송비는 추정치를 둔다
    assert "/search?q=" in by["dunkeld"].listing_urls[0]


def test_independent_bottlings_get_their_own_baseline_key():
    ob = _an("Springbank 25 Jahre - Campbeltown Single Malt Scotch Whisky Inhalt: 0,70 l 46,0% vol.")
    ib = _an("Springbank 25 Jahre - Director's Special - Single Malts of Scotland - Campbeltown Inhalt: 0,70 l 55,5% vol.")
    assert ob.key == "springbank|25|700ml" and ib.key == "springbank|25|independent|700ml"
    assert _an("Springbank 35 Jahre - 1989/2024 - Signatory Vintage - Symington's Choice 0,70 l").key.startswith("springbank|35|independent")
    assert "independent" in _an("Springbank 31 Jahre - 1991/2022 - Hunter Laing - Old & Rare 0,70 l").tokens


def test_cadenhead_shop_marks_every_bottle_as_independent():
    s, c = _parse("cadenhead", "cadenhead_listing.html", "https://cadenhead.shop/distillery/springbank/")
    assert s.add_tokens == ["independent"]
    cfg = Config(watchlist=WL, shops=[s])
    obs = evaluate(s, c[0], cfg, RATES)
    assert "independent" in obs.analysis.tokens and obs.analysis.key == "springbank|12|independent|y2011|700ml"


# ---------------------------------------------------------------- 목표가(price_caps)


def test_shipped_price_caps_match_only_plain_standard_bottles():
    from whiskywatch.monitor import match_cap

    caps = load_config(ROOT / "config.yaml").price_caps
    assert {c["max_krw"] for c in caps} == {250000, 350000, 700000, 1000000}

    def cap(title):
        a = _an(title)
        c = match_cap(a, caps) if a else None
        return c["max_krw"] if c else None

    assert cap("Springbank 10 Year Old 70cl 46%") == 250000
    assert cap("Springbank 15 Year Old 70cl 46%") == 350000
    assert cap("Springbank 12 Year Old Cask Strength Batch 25 70cl 56.2%") == 350000
    assert cap("Springbank 18 Year Old 70cl 46%") == 700000
    assert cap("Springbank 21 Year Old 70cl 46%") == 1000000
    # 로컬발리·100프루프·독립병입·다른 증류소·다른 숙성은 목표가 규칙에 안 걸린다(기존 이력 기준 유지)
    assert cap("Springbank 10 Year Old Local Barley 70cl 55%") is None
    assert cap("Springbank 10Y 100 Proof 2006 57.0%") is None
    assert cap("Springbank 12 Jahre - 55,5 % - Cask Strength Edition 2025 Signatory 0,70 l") is None
    assert cap("Springbank 25 Year Old 70cl") is None and cap("Hazelburn 10 Year Old 70cl 46%") is None
    assert cap("Springbank 12 Year Old 70cl 46%") is None  # 일반 12년(캐스크 스트렝스 아님)은 목표가 없음


_CAP_HTML = '<html><body><div class="c"><a href="/p/1">{title}</a><span class="p">£{price}</span></div></body></html>'


def _cap_run(price, cap_krw, title="Springbank 15 Year Old 70cl 46%", stock_html=""):
    import tempfile

    shop = ShopConfig(id="t", name="T", currency="GBP", listing_urls=["https://t.example/list"],
                      selectors={"card": "div.c", "price": ".p"}, ship_base=0)
    caps = [{"label": "스프링뱅크 15년", "distillery": "springbank", "age": 15, "tokens": [], "max_krw": cap_krw}]
    cfg = Config(watchlist=WL, shops=[shop], price_caps=caps)
    f = MapFetcher({"https://t.example/list": _CAP_HTML.format(title=title, price=price) + stock_html})
    d = Path(tempfile.mkdtemp())
    sent = []

    class N:
        def send(self, t):
            sent.append(t)
            return True

    s = run_once(cfg, History(d / "h.jsonl"), State(d / "s.json"), N(), _Pool(f), rates=RATES)
    return s, sent


def test_price_cap_alerts_immediately_without_history_when_at_or_below_target():
    s, sent = _cap_run("100.00", 400_000)  # £100 -> 도착가 약 35만원(소액면세 + 주세/교육세)
    alerts = [m for m in sent if "도착가(추정)" in m]  # (첫 실행 안내 메시지는 제외)
    assert s.alerts_sent == 1 and len(alerts) == 1
    assert "내가 정한 목표가(스프링뱅크 15년)" in alerts[0] and "₩400,000 이하 조건 충족" in alerts[0]


def test_price_cap_does_not_alert_when_above_target_or_out_of_stock():
    s, sent = _cap_run("100.00", 300_000)
    assert s.alerts_sent == 0 and not [m for m in sent if "도착가(추정)" in m]
    s, sent = _cap_run("100.00", 400_000, title="Springbank 15 Year Old 70cl 46% Sold out")
    assert s.alerts_sent == 0  # 품절은 알림 없음
