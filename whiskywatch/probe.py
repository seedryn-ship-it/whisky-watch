"""후보 샵 점검(probe).

'실제 실행 환경'(GitHub 러너)에서 후보 샵마다 아래를 확인합니다. 샵당 요청은 3~6번뿐이고, robots.txt 가 막는 경로는 요청하지 않습니다.
  1) 접속이 되는가 (403/429 = 서버 IP 차단)
  2) 어떤 쇼핑몰 플랫폼인가 (Shopify 면 전체 상품 JSON 으로 가장 안정적으로 읽을 수 있음)
  3) 스프링뱅크 계열 상품이 실제로 잡히는가

결과는 config.yaml 에 샵을 추가할지 판단하는 자료입니다.
"""

from __future__ import annotations

import json
import random
import re
import time
from urllib import robotparser
from urllib.parse import urlparse

import requests

# ChatGPT 로 만든 목록 + 검색으로 찾은 곳. 한국 배송 여부는 이 점검으로 알 수 없으니 장바구니에서 직접 확인이 필요합니다.
DEFAULT_CANDIDATES = [
    "whiskyinternationalonline.com", "royalmilewhiskies.com", "whiskyshop.com", "lochfynewhiskies.com",
    "houseofmalt.co.uk", "hardtofindwhisky.co.uk", "nickollsandperks.com", "stillspirit.com",
    "fynemalts.com", "cadenhead.shop", "hedonism.co.uk", "bbr.com", "whiskysituation.com",
    "thegoodspiritsco.com", "robertgraham1874.com", "thegreenwellystop.co.uk", "dunkeldwhiskybox.co.uk",
    "luvians.com", "gauntleys.com", "whiskygalore.co.nz", "celticwhiskeyshop.com", "spiritfly.ch",
    "utopia-spirits.com", "billyswhiskybarrel.be", "whiskyfass.de", "inn-out-shop.com",
    "rarevintagewhisky.com", "rarevintagewhisky.nl", "musthavemalts.com", "dekanta.com", "whisky.de",
]

WATCH_RE = re.compile(r"springbank|hazelburn|longrow|kilkerran", re.I)
BLOCK_MARKERS = (
    "just a moment", "cf-chl", "attention required", "captcha", "security checkpoint",
    "access denied", "are you a robot", "px-captcha", "datadome",
)
PLATFORMS = [
    ("shopify", ("cdn.shopify.com", "shopify.theme", "myshopify.com")),
    ("magento", ("magento", "mage-init", "/static/version")),
    ("woocommerce", ("woocommerce", "wp-content/plugins/woocommerce")),
    ("shopware", ("shopware",)),
    ("lightspeed", ("lightspeed", "seoshop", "webshopapp")),
    ("bigcommerce", ("bigcommerce",)),
    ("prestashop", ("prestashop",)),
    ("squarespace", ("squarespace",)),
    ("wix", ("wixstatic", "wix.com")),
]
SHOPIFY_COLLECTION_HANDLES = ("springbank", "springbank-whisky", "springbank-distillery", "springbank-single-malt")


def _base(domain: str) -> str:
    if domain.startswith("http"):
        return domain.rstrip("/")
    return "https://" + domain.rstrip("/")


class Prober:
    def __init__(self, user_agent: str, timeout: float = 20, delay: tuple[float, float] = (1.5, 3.0), session=None):
        self.ua = user_agent
        self.timeout = timeout
        self.delay = delay
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": user_agent, "Accept-Language": "en;q=0.8"})

    def _sleep(self):
        if self.delay[1] > 0:
            time.sleep(random.uniform(*self.delay))

    def _get(self, url: str):
        """(status, text, error)"""
        try:
            r = self.s.get(url, timeout=self.timeout, allow_redirects=True)
            return r.status_code, r.text, ""
        except requests.RequestException as e:
            return 0, "", type(e).__name__

    def probe(self, domain: str) -> dict:
        base = _base(domain)
        out = {"domain": domain, "verdict": "", "detail": "", "platform": "?", "robots": "?", "watched": None,
               "sample": "", "handle": ""}

        # robots.txt
        rp = robotparser.RobotFileParser()
        st, txt, err = self._get(base + "/robots.txt")
        if st == 200 and "<html" not in txt[:300].lower():
            rp.parse(txt.splitlines())
            out["robots"] = "있음"
            allowed = lambda path: rp.can_fetch(self.ua, base + path)  # noqa: E731
        elif st in (401, 403, 429) or (st == 0 and err):
            out["verdict"], out["detail"] = ("차단" if st else "실패"), f"robots.txt 단계에서 {'HTTP ' + str(st) if st else err}"
            return out
        else:
            out["robots"] = "없음(모두 허용)"
            allowed = lambda path: True  # noqa: E731
        self._sleep()

        # 첫 화면
        if not allowed("/"):
            out["verdict"], out["detail"] = "수동", "robots.txt 가 첫 화면 접근도 허용하지 않음"
            return out
        st, html, err = self._get(base + "/")
        low = html.lower()
        if st == 0:
            out["verdict"], out["detail"] = "실패", err
            return out
        if st in (403, 429, 503):
            out["verdict"], out["detail"] = "차단", f"HTTP {st} (서버 IP/봇 차단 추정)"
            return out
        if st == 200 and any(m in low[:6000] for m in BLOCK_MARKERS):
            out["verdict"], out["detail"] = "차단", "봇 확인 화면(캡차/JS 검사) 추정"
            return out
        if st >= 400:
            out["verdict"], out["detail"] = "실패", f"HTTP {st}"
            return out
        for name, marks in PLATFORMS:
            if any(m in low for m in marks):
                out["platform"] = name
                break
        self._sleep()

        # Shopify: 전체 상품 JSON
        if out["platform"] == "shopify":
            if not allowed("/products.json"):
                out["verdict"], out["detail"] = "수동", "Shopify 이지만 robots.txt 가 products.json 을 허용하지 않음"
                return out
            products, note = self._shopify_products(base + "/products.json?limit=250&page=1")
            if products is None:
                out["verdict"], out["detail"] = "수동", f"Shopify 이지만 products.json 을 읽지 못함 ({note})"
                return out
            watched = [p for p in products if WATCH_RE.search(f"{p.get('title', '')} {p.get('vendor', '')}")]
            out["watched"] = len(watched)
            full = len(products) >= 250
            if watched:
                p = watched[0]
                price = (p.get("variants") or [{}])[0].get("price", "?")
                out["sample"] = f"{p.get('title', '')[:50]} / {price}"
            # 컬렉션(브랜드) 페이지가 있으면 전체 카탈로그 대신 그것만 읽으면 요청이 훨씬 적다
            for h in SHOPIFY_COLLECTION_HANDLES:
                path = f"/collections/{h}/products.json"
                if not allowed(path):
                    continue
                self._sleep()
                cp, _ = self._shopify_products(base + path + "?limit=250")
                if cp:
                    out["handle"] = h
                    out["watched"] = max(out["watched"], sum(1 for p in cp if WATCH_RE.search(p.get("title", ""))))
                    break
            if out["watched"] or out["handle"]:
                how = f"컬렉션 '{out['handle']}'" if out["handle"] else ("전체 상품 JSON (1페이지 250건 중)" if full else "전체 상품 JSON")
                out["verdict"], out["detail"] = "가능", f"Shopify - {how} 에서 스프링뱅크 계열 {out['watched']}건"
            else:
                out["verdict"], out["detail"] = "확인", "Shopify - 1페이지에는 스프링뱅크 계열 없음" + (" (2페이지 이후 확인 필요)" if full else "")
            return out

        # 그 밖의 플랫폼: 사람이 봐야 함
        search_ok = allowed("/search?q=springbank")
        out["verdict"] = "수동"
        out["detail"] = f"{out['platform']} 플랫폼 - 접속은 됨, 검색 주소 robots.txt {'허용' if search_ok else '차단'}. 브랜드 목록 페이지 구조 확인 필요"
        return out

    def _shopify_products(self, url: str):
        st, text, err = self._get(url)
        if st != 200:
            return None, f"HTTP {st}" if st else err
        try:
            data = json.loads(text)
            return list(data.get("products", [])), ""
        except ValueError:
            return None, "JSON 아님"


def format_line(r: dict) -> str:
    tag = {"가능": "[가능]", "확인": "[확인]", "수동": "[수동]", "차단": "[차단]", "실패": "[실패]"}.get(r["verdict"], "[?]")
    s = f"{tag} {r['domain']:<28} {r['detail']}"
    if r.get("sample"):
        s += f"\n         예: {r['sample']}"
    return s


def domain_of(url: str) -> str:
    return urlparse(url).netloc or url
