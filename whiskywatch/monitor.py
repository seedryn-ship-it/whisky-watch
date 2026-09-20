"""한 번의 감시 사이클: 수집 -> 도착가 계산 -> 기준가(중앙값) 비교 -> 알림 -> 이력 저장."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import quote_plus

from . import parsers
from .config import Config, ShopConfig
from .fetchers import BlockedError, FetchError, FetcherPool
from .fx import FxError, get_rates
from .normalize import Analysis, analyze, fold, term_matches
from .notify import esc, won
from .store import Baseline, History, State, lookup_baseline
from .tax import LandedCost, all_in_cost, compute_landed_cost


@dataclass
class Observation:
    shop: ShopConfig
    cand: parsers.Candidate
    analysis: Analysis
    currency: str
    price_native_ex_vat: float
    ship_native: float
    landed: LandedCost  # 수량(assume_bottles) 전체 기준
    per_bottle_krw: int
    qty: int
    in_stock: bool | None


@dataclass
class RunSummary:
    shops_ok: int = 0
    shops_failed: dict[str, str] = field(default_factory=dict)
    observations: int = 0
    recorded: int = 0
    alerts_sent: int = 0
    alerts_found: int = 0
    no_baseline: int = 0


# ------------------------------------------------------------------ collect

def _targets(shop: ShopConfig, cfg: Config, terms: list[str] | None) -> list[tuple[str, int]]:
    """(URL 템플릿, 최대 페이지) 목록. 목록 페이지가 지정되면 그것을, 아니면 검색 URL 을 키워드별로 쓴다."""
    targets: list[tuple[str, int]] = []
    for entry in shop.listing_urls:
        if isinstance(entry, dict):
            targets.append((str(entry["url"]), int(entry.get("pages", 1))))
        else:
            targets.append((str(entry), shop.max_pages))
    if not targets and shop.search_url:
        for term in terms or cfg.watchlist:
            targets.append((shop.search_url.replace("{q}", quote_plus(term)), shop.max_pages))
    return targets


def collect_shop(
    shop: ShopConfig,
    cfg: Config,
    fetcher,
    terms: list[str] | None = None,
) -> list[parsers.Candidate]:
    """샵 하나에서 후보(상품명/URL/가격) 수집. 전부 실패했을 때만 예외를 던진다(일부 실패는 허용)."""
    cands: list[parsers.Candidate] = []
    seen: set[str] = set()
    first_error: FetchError | None = None

    for template, pages in _targets(shop, cfg, terms):
        paged = "{page}" in template
        for page in range(1, (pages if paged else 1) + 1):
            url = template.replace("{page}", str(page))
            try:
                body = fetcher.get(url)
            except FetchError as e:
                if page == 1 and first_error is None:
                    first_error = e
                break  # 이 목록은 여기까지 (2쪽 이후의 404 등은 '끝'으로 간주)
            if shop.type == "shopify":
                got = parsers.parse_shopify_products(body, shop.base_url, shop.currency)
            else:
                got = parsers.parse_listing(
                    body, url, currency=shop.currency, selectors=shop.selectors or None, preset=shop.preset
                )
            new = [c for c in got if c.url not in seen]
            seen.update(c.url for c in new)
            cands += new
            if not new:
                break  # 더 새로운 상품이 없으면 다음 페이지를 보지 않음

    if not cands and first_error is not None:
        raise first_error

    uniq: dict[str, parsers.Candidate] = {}
    for c in cands:
        if c.url not in uniq or (uniq[c.url].price is None and c.price is not None):
            uniq[c.url] = c
    cands = list(uniq.values())

    if shop.follow_product_pages:
        budget = cfg.http.max_product_page_fetches
        for i, c in enumerate(cands):
            if c.price is not None or budget <= 0:
                continue
            if not any(term_matches(fold(c.title), t) for t in cfg.watchlist):
                continue
            budget -= 1
            try:
                detail = parsers.parse_product_page(fetcher.get(c.url), c.url, shop.currency)
            except FetchError:
                continue
            if detail and detail.price is not None:
                detail.title = detail.title or c.title
                cands[i] = detail
    return cands


# ------------------------------------------------------------------ evaluate

def evaluate(
    shop: ShopConfig,
    cand: parsers.Candidate,
    cfg: Config,
    rates: dict[str, float],
) -> Observation | None:
    if cand.price is None:
        return None
    a = analyze(
        cand.title,
        watchlist=cfg.watchlist,
        exclude_words=cfg.exclude_words,
        allowed_volumes_ml=cfg.allowed_volumes_ml,
        distilleries=cfg.distilleries,
        rare_words=cfg.rare_words,
    )
    if a is None:
        return None
    currency = (cand.currency or shop.currency).upper()
    if currency not in rates:
        return None
    lo, hi = cfg.price_sanity
    if not (lo <= cand.price <= hi):
        return None

    qty = max(1, int(cfg.assume_bottles))
    price_ex_vat = cand.price if shop.all_in else cand.price / (1 + shop.strip_vat)
    ship_native = shop.ship_base + shop.ship_extra * (qty - 1)
    markup = 1 + cfg.fx_card_markup_pct / 100
    item_krw = price_ex_vat * qty * rates[currency] * markup
    ship_krw = ship_native * rates[shop.currency] * markup
    if shop.all_in:  # 세금·통관 포함가: 세금을 다시 더하지 않는다
        lc = all_in_cost(item_krw, ship_krw)
    else:
        lc = compute_landed_cost(item_krw, ship_krw, rates["USD"], volume_ml=a.volume_ml, qty=qty, cfg=cfg.tax)
    return Observation(
        shop=shop,
        cand=cand,
        analysis=a,
        currency=currency,
        price_native_ex_vat=price_ex_vat,
        ship_native=ship_native,
        landed=lc,
        per_bottle_krw=round(lc.total_krw / qty),
        qty=qty,
        in_stock=cand.in_stock,
    )


# ------------------------------------------------------------------ message

def format_alert(obs: Observation, base: Baseline, pct: float, rates: dict[str, float]) -> str:
    a, lc, shop = obs.analysis, obs.landed, obs.shop
    tags = []
    if a.is_local_barley:
        tags.append("Local Barley")
    if a.is_rare:
        tags.append("고숙성/희귀")
    tag_txt = f" [{', '.join(tags)}]" if tags else ""
    stock_txt = "재고 있음" if obs.in_stock else "재고 미확인"
    if base.source == "history":
        base_txt = f"기준(이력 중앙값, 표본 {base.samples}건)"
    else:
        base_txt = "기준(직접 입력한 시드 가격)"
    cur = obs.currency
    item_line = f"상품 {cur} {obs.price_native_ex_vat:,.2f} -> {won(lc.item_krw / obs.qty)}"
    if shop.all_in:
        item_line += " (샵 표기: 관세·주세·교육세·부가세 포함가)"
    elif shop.strip_vat:
        item_line += f" (VAT {shop.strip_vat * 100:.0f}% 제외 가정)"
    ship_line = f"배송(추정) {shop.currency} {obs.ship_native / obs.qty:,.2f} -> {won(lc.shipping_krw / obs.qty)}"
    if shop.all_in:
        tax_line = "세금: 별도 없음 (샵 표기 포함가 기준, 결제 화면에서 최종 확인)"
        if not lc.shipping_krw:
            ship_line = "배송: 별도 추정 없음"
    elif lc.exempt:
        tax_line = (
            f"세금 {won(lc.tax_krw)} = 주세 {won(lc.liquor_tax_krw)} + 교육세 {won(lc.education_tax_krw)} "
            f"(소액면세: 관세·부가세 면제)"
        )
    else:
        tax_line = (
            f"세금 {won(lc.tax_krw / obs.qty)} = 관세 {won(lc.duty_krw / obs.qty)} + 주세 {won(lc.liquor_tax_krw / obs.qty)} "
            f"+ 교육세 {won(lc.education_tax_krw / obs.qty)} + 부가세 {won(lc.vat_krw / obs.qty)}"
        )
    abv = f" {a.abv:g}%" if a.abv else ""
    vol = f"{a.volume_ml}ml" + ("(추정)" if a.volume_assumed else "")
    return (
        f"<b>{esc(a.title)}</b>{esc(tag_txt)}\n"
        f"{esc(shop.name)} · {stock_txt} · {vol}{abv}\n\n"
        f"도착가(추정) <b>{won(obs.per_bottle_krw)}</b>\n"
        f"{base_txt} {won(base.median_krw)} 대비 <b>{pct:+.1f}%</b>\n\n"
        f"{esc(item_line)}\n{esc(ship_line)}\n{esc(tax_line)}\n\n"
        f"{esc(obs.cand.url)}"
    )


# ------------------------------------------------------------------ run

def run_once(
    cfg: Config,
    history: History,
    state: State,
    notifier,
    fetchers: FetcherPool,
    *,
    dry_run: bool = False,
    now: float | None = None,
    rates: dict[str, float] | None = None,
) -> RunSummary:
    now = now or time.time()
    summary = RunSummary()
    shops = [s for s in cfg.shops if s.enabled]

    if rates is None:
        currencies = sorted({s.currency for s in shops} | {"USD"})
        try:
            rates = get_rates(currencies, state.data)
        except FxError as e:
            notifier.send(f"<b>whisky-watch</b>\n환율 조회 실패로 이번 회차를 건너뜁니다: {esc(str(e))[:300]}")
            summary.shops_failed["fx"] = str(e)
            return summary

    index = history.build_index(now, cfg.alert.baseline_days)
    found: list[tuple[float, Observation, Baseline]] = []

    for shop in shops:
        health = state.health.setdefault(shop.id, {"fails": 0, "notified": False})
        try:
            fetcher = fetchers.get(shop.fetch)
            cands = collect_shop(shop, cfg, fetcher)
            if not cands:
                raise FetchError("응답은 왔지만 상품을 하나도 파싱하지 못함 (마크업 변경/차단 가능성)")
        except (BlockedError, FetchError) as e:
            health["fails"] += 1
            health["last_error"] = str(e)[:300]
            summary.shops_failed[shop.id] = str(e)
            if health["fails"] >= cfg.alert.fail_alert_after and not health["notified"] and not dry_run:
                if notifier.send(
                    f"<b>whisky-watch 점검 필요</b>\n{esc(shop.name)} 수집이 연속 {health['fails']}회 실패했습니다.\n"
                    f"{esc(str(e))[:300]}"
                ):
                    health["notified"] = True
            continue

        health.update({"fails": 0, "notified": False})
        health.pop("last_error", None)
        summary.shops_ok += 1

        for cand in cands:
            obs = evaluate(shop, cand, cfg, rates)
            if obs is None:
                continue
            summary.observations += 1
            a = obs.analysis
            base = lookup_baseline(
                index,
                [a.key, a.family_key],
                cfg.alert.min_samples,
                cfg.seed_prices,
                fold(a.title),
                term_matches,
            )
            if base is None:
                summary.no_baseline += 1
            elif obs.in_stock is not False:
                pct = (obs.per_bottle_krw / base.median_krw - 1) * 100
                if pct <= -cfg.alert.discount_pct:
                    found.append((pct, obs, base))

            row = {
                "t": int(now), "shop": shop.id, "url": cand.url, "title": a.title,
                "key": a.key, "fam": a.family_key, "landed": obs.per_bottle_krw,
                "item": round(cand.price, 2), "cur": obs.currency, "stock": obs.in_stock,
            }
            if not dry_run and history.record(
                row, heartbeat_hours=cfg.storage.heartbeat_hours, change_pct=cfg.storage.change_pct
            ):
                summary.recorded += 1

    # ---- 알림 (큰 할인 순, 중복 억제)
    found.sort(key=lambda x: x[0])
    fresh = []
    for pct, obs, base in found:
        k = f"{obs.shop.id}|{obs.cand.url}"
        prev = state.alerts.get(k)
        if prev:
            recent = now - prev["t"] < cfg.alert.realert_hours * 3600
            not_cheaper = obs.per_bottle_krw > prev["landed"] * (1 - cfg.alert.realert_drop_pct / 100)
            if recent and not_cheaper:
                continue
        fresh.append((pct, obs, base))
    summary.alerts_found = len(fresh)

    for pct, obs, base in fresh[: cfg.alert.max_alerts_per_run]:
        text = format_alert(obs, base, pct, rates)
        ok = notifier.send(text)
        if ok:
            summary.alerts_sent += 1
            if not dry_run:
                state.alerts[f"{obs.shop.id}|{obs.cand.url}"] = {"t": int(now), "landed": obs.per_bottle_krw}
    extra = len(fresh) - cfg.alert.max_alerts_per_run
    if extra > 0:
        notifier.send(f"그 외 기준가 이하 상품 {extra}건이 더 있습니다 (한 번에 최대 {cfg.alert.max_alerts_per_run}건 발송).")

    # ---- 최초 실행 안내
    if not dry_run and not state.data.get("started") and summary.shops_ok:
        if notifier.send(
            "<b>whisky-watch 시작</b>\n"
            f"감시 샵 {summary.shops_ok}곳, 이번 회차 관측 {summary.observations}건.\n"
            f"기준가는 같은 상품의 일별 최저 도착가가 {cfg.alert.min_samples}건 이상 쌓이면 자동으로 잡히고, "
            f"그 전에는 config.yaml 의 seed_prices 로 지정한 상품만 비교합니다."
        ):
            state.data["started"] = int(now)

    return summary
