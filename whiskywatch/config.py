"""config.yaml 로딩. 알 수 없는 키는 무시하고, 없는 키는 기본값을 사용."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .normalize import DEFAULT_DISTILLERIES, DEFAULT_EXCLUDE, DEFAULT_RARE_WORDS
from .tax import TaxConfig


@dataclass
class ShopConfig:
    id: str
    name: str
    currency: str
    search_url: str = ""  # {q}, {page} 자리표시자. robots.txt 가 검색을 막는 샵은 listing_urls 를 쓴다
    listing_urls: list = field(default_factory=list)  # 브랜드/증류소 목록 페이지. 문자열 또는 {url, pages}
    all_in: bool = False  # 관세·주세·교육세·부가세·배송이 표시가에 이미 포함된 샵(예: Winemoa)
    type: str = "html"  # html | shopify
    enabled: bool = True
    fetch: str = "requests"  # requests | playwright
    strip_vat: float = 0.0  # 표시가에 포함된 VAT 비율(수출 시 면세) 예: 영국 0.20, 독일 0.19
    ship_base: float = 0.0  # 한국행 배송비 추정: 1병 기본요금(샵 통화)
    ship_extra: float = 0.0  # 추가 1병당 요금
    max_pages: int = 1
    follow_product_pages: bool = False  # 목록에 가격이 없을 때 상세 페이지 조회
    preset: str | None = None  # magento | shopware
    selectors: dict[str, str] = field(default_factory=dict)
    note: str = ""

    @property
    def base_url(self) -> str:
        first = self.search_url
        if not first and self.listing_urls:
            entry = self.listing_urls[0]
            first = entry["url"] if isinstance(entry, dict) else str(entry)
        p = urlparse(first)
        return f"{p.scheme}://{p.netloc}"


@dataclass
class AlertConfig:
    discount_pct: float = 5.0  # 기준가보다 이만큼(%) 이상 싸면 알림
    min_samples: int = 6  # 기준가 산출에 필요한 최소 표본(일 단위 최저가 기준)
    baseline_days: int = 180
    realert_hours: float = 48.0
    realert_drop_pct: float = 2.0  # 마지막 알림가보다 이만큼 더 싸지면 재알림
    fail_alert_after: int = 12  # 같은 샵이 연속 N회 실패하면 1회 점검 알림
    max_alerts_per_run: int = 15


@dataclass
class StorageConfig:
    history_path: str = "data/history.jsonl"
    state_path: str = "data/state.json"
    heartbeat_hours: float = 6.0  # 가격 변동이 없어도 이 간격으로 1건 기록
    change_pct: float = 0.5  # 이 이상 변하면 즉시 기록
    retention_days: int = 365


@dataclass
class HttpConfig:
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36 whisky-watch/0.1 (personal price monitor)"
    )
    timeout: float = 25
    delay_min: float = 1.5
    delay_max: float = 3.5
    respect_robots: bool = True
    max_product_page_fetches: int = 15


@dataclass
class Config:
    watchlist: list[str]
    shops: list[ShopConfig]
    exclude_words: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    allowed_volumes_ml: list[int] = field(default_factory=lambda: [700, 750, 1000])
    distilleries: list[str] = field(default_factory=lambda: list(DEFAULT_DISTILLERIES))
    rare_words: list[str] = field(default_factory=lambda: list(DEFAULT_RARE_WORDS))
    assume_bottles: int = 1
    fx_card_markup_pct: float = 0.0  # 카드 해외결제 수수료 등 (상품+배송에 가산)
    price_sanity: tuple[float, float] = (25.0, 30000.0)  # 샵 통화 기준 비정상 가격 필터
    tax: TaxConfig = field(default_factory=TaxConfig)
    alert: AlertConfig = field(default_factory=AlertConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    seed_prices: list[dict[str, Any]] = field(default_factory=list)  # [{match: "...", krw: 450000}]


def _build(cls, data: dict | None):
    data = data or {}
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    shops = [_build(ShopConfig, s) for s in raw.get("shops", [])]
    if not raw.get("watchlist"):
        raise ValueError("config.yaml 의 watchlist 가 비어 있습니다")
    cfg = Config(
        watchlist=[str(w) for w in raw["watchlist"]],
        shops=shops,
        tax=_build(TaxConfig, raw.get("tax")),
        alert=_build(AlertConfig, raw.get("alert")),
        storage=_build(StorageConfig, raw.get("storage")),
        http=_build(HttpConfig, raw.get("http")),
    )
    for key in (
        "exclude_words", "allowed_volumes_ml", "distilleries", "rare_words",
        "assume_bottles", "fx_card_markup_pct", "seed_prices",
    ):
        if key in raw and raw[key] is not None:
            setattr(cfg, key, raw[key])
    if raw.get("price_sanity"):
        cfg.price_sanity = tuple(raw["price_sanity"])
    return cfg
