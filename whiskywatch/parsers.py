"""HTML/JSON 에서 (상품명, URL, 가격, 재고)를 뽑는 파서.

우선순위: JSON-LD(schema.org Product/ItemList) -> 셀렉터 프리셋/사용자 지정 -> 범용 휴리스틱.
샵 마크업이 바뀌어도 JSON-LD 가 있으면 대부분 그대로 동작합니다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urljoin

import soupsieve
from bs4 import BeautifulSoup, Tag

CURRENCY_SYMBOLS = {"£": "GBP", "€": "EUR", "$": "USD"}
_PRICE_TOKEN = re.compile(
    r"(?:(?P<pre>[£€$])\s*(?P<n1>\d[\d.,\s]*)|(?P<n2>\d[\d.,\s]*)\s*(?P<post>[£€$]|EUR|GBP|USD))"
)
_SOLD_OUT = re.compile(
    r"sold\s*out|out\s*of\s*stock|ausverkauft|nicht\s+verf[uü]gbar|not\s+available|"
    r"currently\s+unavailable|notify\s+me|email\s+me\s+when|epuise|rupture",
    re.I,
)
_UNIT_PRICE = re.compile(
    r"per\s*(?:litre|liter|l\b)|/\s*l\b|pro\s*liter|grundpreis|unit\s*price|price\s*per|je\s*liter",
    re.I,
)
_OLD_CLASS = re.compile(r"old|was|regular|strike|compare|line-through|rrp|msrp|original", re.I)


@dataclass
class Candidate:
    title: str
    url: str
    price: float | None
    currency: str | None
    in_stock: bool | None
    source: str = ""


def to_number(raw: str) -> float | None:
    """'1.299,00' / '1,299.00' / '95.5' / '95,00' -> float"""
    s = re.sub(r"[^\d.,]", "", raw.replace("\xa0", " ").strip())
    if not s or not re.search(r"\d", s):
        return None
    if "." in s and "," in s:
        dec = "." if s.rfind(".") > s.rfind(",") else ","
        thou = "," if dec == "." else "."
        s = s.replace(thou, "").replace(dec, ".")
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        parts = s.split(sep)
        if len(parts) > 2:  # 1,299,000
            s = "".join(parts)
        elif len(parts[1]) == 3 and len(parts[0]) <= 3 and parts[0] != "0":
            s = parts[0] + parts[1]  # 1,299 -> 1299
        else:
            s = parts[0] + "." + parts[1]
    try:
        return float(s)
    except ValueError:
        return None


def find_prices(text: str) -> list[tuple[float, str | None]]:
    out = []
    for m in _PRICE_TOKEN.finditer(text):
        sym = m.group("pre") or m.group("post")
        num = m.group("n1") or m.group("n2")
        cur = CURRENCY_SYMBOLS.get(sym, sym)
        val = to_number(num.strip())
        if val is not None:
            out.append((val, cur))
    return out


# ----------------------------------------------------------------- JSON-LD

def _iter_jsonld(soup: BeautifulSoup):
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = (tag.string or tag.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:
                data = json.loads(re.sub(r"[\x00-\x1f]+", " ", raw))
            except json.JSONDecodeError:
                continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                if "@graph" in node:
                    stack.append(node["@graph"])


def _types(node: dict) -> set[str]:
    t = node.get("@type", [])
    return set(t if isinstance(t, list) else [t])


def _offer_info(offers) -> tuple[float | None, str | None, bool | None]:
    if offers is None:
        return None, None, None
    offers = offers if isinstance(offers, list) else [offers]
    best: tuple[float, str | None, bool | None] | None = None
    for off in offers:
        if not isinstance(off, dict):
            continue
        raw = off.get("price")
        if raw is None:
            raw = off.get("lowPrice")
        if raw is None and isinstance(off.get("priceSpecification"), dict):
            raw = off["priceSpecification"].get("price")
        price = to_number(str(raw)) if raw is not None else None
        if price is None:
            continue
        avail = str(off.get("availability", ""))
        stock = None
        if avail:
            stock = bool(re.search(r"InStock|LimitedAvailability|OnlineOnly", avail))
            if re.search(r"OutOfStock|SoldOut|Discontinued", avail):
                stock = False
        cur = off.get("priceCurrency")
        cand = (price, cur, stock)
        if best is None or (stock is not False and (best[2] is False or price < best[0])):
            best = cand
    return best if best else (None, None, None)


def _product_from_node(node: dict, base_url: str) -> Candidate | None:
    name = node.get("name")
    if not name:
        return None
    offers = node.get("offers")
    url = node.get("url")
    if not url:
        first = offers[0] if isinstance(offers, list) and offers else offers
        if isinstance(first, dict):
            url = first.get("url")
    price, cur, stock = _offer_info(offers)
    return Candidate(
        title=str(name).strip(),
        url=urljoin(base_url, url) if url else base_url,
        price=price,
        currency=cur,
        in_stock=stock,
        source="jsonld",
    )


def parse_jsonld(html: str, base_url: str) -> list[Candidate]:
    soup = BeautifulSoup(html, "lxml")
    out: list[Candidate] = []
    for node in _iter_jsonld(soup):
        types = _types(node)
        if "Product" in types:
            c = _product_from_node(node, base_url)
            if c:
                out.append(c)
        elif "ItemList" in types:
            for el in node.get("itemListElement", []) or []:
                item = el.get("item", el) if isinstance(el, dict) else None
                if isinstance(item, dict):
                    if "Product" in _types(item) or item.get("offers"):
                        c = _product_from_node(item, base_url)
                    elif item.get("name") and item.get("url"):
                        c = Candidate(item["name"], urljoin(base_url, item["url"]), None, None, None, "jsonld-list")
                    else:
                        c = None
                    if c:
                        out.append(c)
    return _dedupe(out)


def parse_meta_product(html: str, base_url: str) -> Candidate | None:
    """og:title + product:price:amount 메타태그 (Magento/Shopify 계열 상세 페이지)."""
    soup = BeautifulSoup(html, "lxml")

    def meta(*names: str) -> str | None:
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if tag and tag.get("content"):
                return tag["content"]
        return None

    title = meta("og:title") or (soup.title.string.strip() if soup.title and soup.title.string else None)
    amount = meta("product:price:amount", "og:price:amount")
    if not title or not amount:
        return None
    price = to_number(amount)
    avail = meta("product:availability", "og:availability") or ""
    stock = None if not avail else ("out" not in avail.lower())
    return Candidate(
        title=title.strip(),
        url=meta("og:url") or base_url,
        price=price,
        currency=meta("product:price:currency", "og:price:currency"),
        in_stock=stock,
        source="meta",
    )


# ----------------------------------------------------------------- cards

CARD_PRESETS: dict[str, dict[str, str]] = {
    # Magento 2 기본 테마
    "magento": {
        "card": "li.product-item, div.product-item",
        "title": "a.product-item-link",
        "link": "a.product-item-link",
        "price": "[data-price-amount], .price",
    },
    # Shopware 6 기본 테마
    "shopware": {
        "card": "div.product-box",
        "title": "a.product-name",
        "link": "a.product-name",
        "price": ".product-price",
    },
    # WooCommerce + WoodMart 테마 (Nickolls & Perks 에서 실제 마크업 확인). 세금 제외 가격(hub-duty-vat-price)을 우선 사용
    "woodmart": {
        "card": "div.wd-product",
        "title": ".wd-entities-title a, .hub-format-abv",
        "link": ".wd-entities-title a",
        "price": "span.hub-duty-vat-price .amount",  # 없으면(세금 제외 표기 없는 상품) 카드 안 첫 가격으로 대체
        "sold_out": ".outofstock",
    },
    # Lightspeed eCom (Whiskybase Shop 에서 실제 마크업 확인)
    "lightspeed": {
        "card": "div.product-block",
        "title": "h4 a.title",
        "link": "h4 a.title",
        "price": "div.product-block-price",
    },
}


def _strip_old_prices(card: Tag) -> BeautifulSoup:
    """세일 전 가격(취소선, old-price 등)을 제거한 카드 사본."""
    clone = BeautifulSoup(str(card), "lxml")
    doomed = list(clone.find_all(["del", "s", "strike"]))
    for el in clone.find_all(True):
        cls = " ".join(el.get("class", []) or [])
        if cls and _OLD_CLASS.search(cls) and "price" in cls.lower():
            doomed.append(el)
    for el in doomed:  # 부모가 먼저 제거된 자식은 건너뜀
        if not getattr(el, "decomposed", False):
            el.decompose()
    return clone


def _clean_price_text(card: Tag) -> str:
    """세일 전 가격, 리터당 가격 등을 제거한 카드 텍스트."""
    clone = _strip_old_prices(card)
    return " ".join(s for s in clone.stripped_strings if not _UNIT_PRICE.search(s))


def _card_price(card: Tag, price_sel: str | None, default_cur: str | None):
    if price_sel:
        clone = _strip_old_prices(card)
        els = [e for e in clone.select(price_sel) if not _UNIT_PRICE.search(e.get_text(" ", strip=True))]
        for el in els:
            amt = el.get("data-price-amount")
            if amt and to_number(amt) is not None:
                return to_number(amt), default_cur
        for el in els:
            # "€435,<span>00</span>" 처럼 소수부가 별도 태그이면 텍스트가 "€435, 00" 이 되므로 붙여 준다
            found = find_prices(re.sub(r"(\d)([.,])\s+(\d{2})(?!\d)", r"\1\2\3", el.get_text(" ", strip=True)))
            if found:
                return found[0]
    prices = find_prices(_clean_price_text(card))
    if prices:
        return prices[0]
    return None, default_cur


def _card_sold_out(card: Tag, sold_out_sel: str | None, in_stock_sel: str | None = None) -> bool:
    """품절 판정. sold_out 선택자(카드 자신 또는 내부 요소와 일치, 예: '.outofstock')가 있으면 우선, 없으면 카드 문구로 판단.
    품절 상품에 '품절' 글자가 없고 CSS 클래스만 다는 샵(WooCommerce 등)이 있어서 필요하다.
    in_stock 선택자: 재고가 있을 때만 카드 안에 있는 요소(예: 장바구니 버튼). 없으면 품절로 본다."""
    if in_stock_sel:
        try:
            if card.select_one(in_stock_sel) is None:
                return True
        except Exception:
            pass
    if sold_out_sel:
        try:
            if soupsieve.match(sold_out_sel, card) or card.select_one(sold_out_sel) is not None:
                return True
        except Exception:
            pass
    return bool(_SOLD_OUT.search(card.get_text(" ", strip=True)))


def parse_cards(
    html: str,
    base_url: str,
    default_currency: str | None = None,
    selectors: dict[str, str] | None = None,
) -> list[Candidate]:
    soup = BeautifulSoup(html, "lxml")
    sel = selectors or {}
    out: list[Candidate] = []

    if sel.get("card"):
        for card in soup.select(sel["card"]):
            # 카드 자체가 <a> 인 샵(예: htfw.com)이 있어서, 안에서 못 찾으면 카드 자신도 확인한다
            a = (
                card.select_one(sel.get("link", "a"))
                or card.find("a", href=True)
                or (card if card.name == "a" and card.get("href") else None)
            )
            if not a or not a.get("href"):
                continue
            # title 은 쉼표로 여러 요소를 지정할 수 있다 (예: 이름 + "70cl / 46%") -> 공백으로 이어 붙임
            parts = [e.get_text(" ", strip=True) for e in card.select(sel["title"])] if sel.get("title") else []
            title = " ".join(p for p in parts if p) or a.get_text(" ", strip=True)
            if not title:
                continue
            price, cur = _card_price(card, sel.get("price"), default_currency)
            out.append(
                Candidate(
                    title,
                    urljoin(base_url, a["href"]),
                    price,
                    cur or default_currency,
                    False if _card_sold_out(card, sel.get("sold_out"), sel.get("in_stock")) else None,
                    "selector",
                )
            )
        return _dedupe(out)

    # 범용 휴리스틱: 제목처럼 보이는 링크에서 위로 올라가며 가격이 있는 가장 가까운 컨테이너를 카드로 간주
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        href = urljoin(base_url, a["href"])
        if len(text) < 8 or href in seen or href.startswith(("javascript:", "mailto:")):
            continue
        node: Tag | None = a
        for _ in range(6):
            node = node.parent if node else None
            if node is None or node.name in ("body", "html"):
                node = None
                break
            others = {
                x["href"]
                for x in node.find_all("a", href=True)
                if len(x.get_text(strip=True)) >= 8 and urljoin(base_url, x["href"]) != href
            }
            if len(others) > 2:  # 너무 넓어짐: 다른 상품까지 포함
                node = None
                break
            if find_prices(_clean_price_text(node)):
                break
        if node is None:
            continue
        seen.add(href)
        price, cur = _card_price(node, None, default_currency)
        out.append(
            Candidate(
                text,
                href,
                price,
                cur or default_currency,
                False if _SOLD_OUT.search(node.get_text(" ", strip=True)) else None,
                "heuristic",
            )
        )
    return _dedupe(out)


# ----------------------------------------------------------------- shopify

def parse_shopify_products(text: str, base_url: str, currency: str | None) -> list[Candidate]:
    """https://<shop>/products.json?limit=250&page=N 응답."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    out = []
    for p in data.get("products", []):
        variants = p.get("variants") or []
        prices = [(to_number(str(v.get("price"))), v.get("available")) for v in variants]
        prices = [(pr, av) for pr, av in prices if pr is not None]
        if not prices:
            continue
        in_stock_prices = [pr for pr, av in prices if av]
        price = min(in_stock_prices) if in_stock_prices else min(pr for pr, _ in prices)
        out.append(
            Candidate(
                p.get("title", "").strip(),
                urljoin(base_url, f"/products/{p.get('handle', '')}"),
                price,
                currency,
                bool(in_stock_prices),
                "shopify",
            )
        )
    return out


# ----------------------------------------------------------------- entry points

def _dedupe(cands: list[Candidate]) -> list[Candidate]:
    seen: dict[str, Candidate] = {}
    for c in cands:
        prev = seen.get(c.url)
        if prev is None or (prev.price is None and c.price is not None):
            seen[c.url] = c
    return list(seen.values())


def parse_listing(
    html: str,
    base_url: str,
    *,
    currency: str | None = None,
    selectors: dict[str, str] | None = None,
    preset: str | None = None,
) -> list[Candidate]:
    """검색 결과 페이지 -> 후보 목록. JSON-LD 결과에 가격이 없으면 카드 파서와 병합."""
    ld = parse_jsonld(html, base_url)
    if ld and all(c.price is not None for c in ld):
        for c in ld:
            c.currency = c.currency or currency
        return ld

    sel = selectors or CARD_PRESETS.get(preset or "", None)
    cards = parse_cards(html, base_url, currency, sel)
    if not ld:
        return cards
    by_url = {c.url: c for c in cards}
    merged = []
    for c in ld:
        if c.price is None and c.url in by_url:
            c = by_url[c.url]
        merged.append(c)
    return _dedupe(merged)


def parse_product_page(html: str, base_url: str, currency: str | None = None) -> Candidate | None:
    prods = [c for c in parse_jsonld(html, base_url) if c.price is not None]
    cand = prods[0] if prods else parse_meta_product(html, base_url)
    if cand:
        cand.currency = cand.currency or currency
    return cand
