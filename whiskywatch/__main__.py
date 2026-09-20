"""CLI

  python -m whiskywatch run              # 한 번 실행 (GitHub Actions 가 주기적으로 호출)
  python -m whiskywatch run --dry-run    # 저장/발송 없이 결과만 화면에 출력
  python -m whiskywatch diagnose         # 샵별 수집/파싱이 실제로 되는지 점검
  python -m whiskywatch tax --price 95 --currency GBP --shipping 30
  python -m whiskywatch test-telegram    # 텔레그램 연결 테스트
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Config, load_config
from .fetchers import BlockedError, FetchError, FetcherPool
from .fx import FxError, get_rates
from .monitor import collect_shop, run_once
from .normalize import analyze
from .probe import DEFAULT_CANDIDATES, Prober, format_line
from .notify import ConsoleNotifier, TelegramNotifier, won
from .store import History, State
from .tax import compute_landed_cost


def _pool(cfg: Config) -> FetcherPool:
    h = cfg.http
    return FetcherPool(h.user_agent, h.timeout, (h.delay_min, h.delay_max), h.respect_robots)


def _resolve(cfg: Config, config_path: Path) -> None:
    base = config_path.resolve().parent
    for attr in ("history_path", "state_path"):
        p = Path(getattr(cfg.storage, attr))
        if not p.is_absolute():
            setattr(cfg.storage, attr, str(base / p))


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    _resolve(cfg, Path(args.config))
    history, state = History(cfg.storage.history_path), State(cfg.storage.state_path)
    notifier = ConsoleNotifier() if args.dry_run else TelegramNotifier.from_env()
    pool = _pool(cfg)
    try:
        s = run_once(cfg, history, state, notifier, pool, dry_run=args.dry_run)
    finally:
        pool.close()
    if not args.dry_run:
        history.save(cfg.storage.retention_days)
        state.save()
    print(
        f"샵 성공 {s.shops_ok} / 실패 {len(s.shops_failed)} | 관측 {s.observations} | "
        f"기록 {s.recorded} | 기준가 없음 {s.no_baseline} | 알림 {s.alerts_sent}/{s.alerts_found}"
    )
    for shop, err in s.shops_failed.items():
        print(f"  - 실패 {shop}: {err}")
    # 일부 샵 실패는 정상 종료(다음 회차에 재시도). 전 샵 실패만 오류 코드.
    return 1 if s.shops_ok == 0 and s.shops_failed else 0


def cmd_diagnose(args) -> int:
    cfg = load_config(args.config)
    pool = _pool(cfg)
    term = args.term or cfg.watchlist[0]
    bad = 0
    try:
        for shop in cfg.shops:
            if args.shop and shop.id != args.shop:
                continue
            if not shop.enabled and not args.shop:
                print(f"\n== {shop.name} ({shop.id}) [비활성 - 건너뜀] ==")
                continue
            if args.fetch:
                shop.fetch = args.fetch
            print(f"\n== {shop.name} ({shop.id}) {'' if shop.enabled else '[비활성]'} [fetch={shop.fetch}] ==")
            try:
                cands = collect_shop(shop, cfg, pool.get(shop.fetch), terms=[term])
            except BlockedError as e:
                print(f"  차단됨: {e}\n  -> config 에서 fetch: playwright 를 시도하거나, 집 PC 등 다른 IP 에서 실행하세요.")
                bad += 1
                continue
            except FetchError as e:
                print(f"  수집 실패: {e}")
                bad += 1
                continue
            watched = []
            for c in cands:
                a = analyze(
                    c.title, watchlist=cfg.watchlist, exclude_words=cfg.exclude_words,
                    allowed_volumes_ml=cfg.allowed_volumes_ml, distilleries=cfg.distilleries,
                    rare_words=cfg.rare_words,
                )
                if a:
                    watched.append((c, a))
            with_price = [c for c, _ in watched if c.price is not None]
            print(f"  후보 {len(cands)}건 / 관심 상품 {len(watched)}건 / 가격 파싱 {len(with_price)}건")
            if not cands:
                print("  -> 파싱 0건: listing_urls(또는 search_url) 주소가 맞는지, selectors/preset 지정이 필요한지 확인하세요.")
                bad += 1
            for c, a in watched[:5]:
                price = f"{c.currency or shop.currency} {c.price:,.2f}" if c.price is not None else "가격 없음"
                print(f"    - {c.title[:70]} | {price} | 재고={c.in_stock} | {a.volume_ml}ml | key={a.key} | src={c.source}")
    finally:
        pool.close()
    return 1 if bad else 0


def cmd_probe(args) -> int:
    cfg = load_config(args.config)
    domains = [d.strip() for d in args.domains.split(",") if d.strip()] if args.domains else DEFAULT_CANDIDATES
    pr = Prober(cfg.http.user_agent, cfg.http.timeout, (cfg.http.delay_min, cfg.http.delay_max))
    results = []
    for d in domains:
        r = pr.probe(d)
        results.append(r)
        print(format_line(r), flush=True)
    print("\n==== 요약 ====")
    for label, title in (("가능", "바로 추가 가능"), ("확인", "Shopify 이지만 추가 확인 필요"), ("수동", "접속은 되지만 사람이 구조를 봐야 함"),
                         ("차단", "이 서버 IP 에서는 차단됨"), ("실패", "접속 실패")):
        names = [r["domain"] for r in results if r["verdict"] == label]
        print(f"{title} ({len(names)}): {', '.join(names) if names else '-'}")
    return 0


def cmd_tax(args) -> int:
    cfg = load_config(args.config)
    state = State(Path(args.config).resolve().parent / cfg.storage.state_path)
    try:
        rates = get_rates([args.currency, "USD"], state.data)
    except FxError as e:
        print(f"환율 조회 실패: {e}")
        return 1
    price = args.price / (1 + args.strip_vat)
    markup = 1 + cfg.fx_card_markup_pct / 100
    item = price * args.qty * rates[args.currency] * markup
    ship = args.shipping * rates[args.currency] * markup
    lc = compute_landed_cost(item, ship, rates["USD"], volume_ml=args.volume, qty=args.qty, cfg=cfg.tax)
    print(f"환율 1 {args.currency} = {rates[args.currency]:,.1f} KRW, 1 USD = {rates['USD']:,.1f} KRW")
    print(f"상품 {won(lc.item_krw)} + 배송 {won(lc.shipping_krw)}")
    print(f"  관세 {won(lc.duty_krw)} / 주세 {won(lc.liquor_tax_krw)} / 교육세 {won(lc.education_tax_krw)} / 부가세 {won(lc.vat_krw)}")
    print(f"  소액면세 적용: {'예' if lc.exempt else '아니오'}")
    print(f"도착가 합계 {won(lc.total_krw)}  (병당 {won(lc.total_krw / args.qty)})")
    return 0


def cmd_test_telegram(args) -> int:
    n = TelegramNotifier.from_env()
    ok = n.send("<b>whisky-watch</b>\n텔레그램 연결 테스트입니다.")
    if ok:
        print("전송 성공")
    else:
        print(f"전송 실패: {n.last_error}")
        hint = {
            "401": "토큰이 틀렸습니다 (BotFather 가 준 값을 그대로, 앞뒤 공백 없이).",
            "404": "토큰 형식이 틀렸습니다 ('bot' 글자를 붙이지 않은 123456:ABC... 형태여야 합니다).",
            "400": "채팅 ID 가 틀렸거나, 봇에게 먼저 아무 메시지도 보내지 않았습니다.",
            "403": "봇이 차단되었거나 채팅에 참여하지 않았습니다.",
        }.get(n.last_error.split(" ")[1] if n.last_error.startswith("HTTP ") else "", "")
        if hint:
            print("  ->", hint)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="whiskywatch", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(fn=cmd_run)

    d = sub.add_parser("diagnose")
    d.add_argument("--shop")
    d.add_argument("--term")
    d.add_argument("--fetch", choices=["requests", "playwright"], help="config 의 fetch 설정을 이번 진단에만 바꿔서 시도")
    d.set_defaults(fn=cmd_diagnose)

    pb = sub.add_parser("probe", help="후보 샵들이 이 환경에서 접속/수집 가능한지 점검")
    pb.add_argument("--domains", help="쉼표로 구분한 도메인 (기본: 내장 후보 목록)")
    pb.set_defaults(fn=cmd_probe)

    t = sub.add_parser("tax")
    t.add_argument("--price", type=float, required=True, help="샵 표시가(샵 통화)")
    t.add_argument("--currency", default="GBP")
    t.add_argument("--strip-vat", type=float, default=0.0, help="예: 영국 0.20")
    t.add_argument("--shipping", type=float, default=0.0, help="한국행 배송비(샵 통화)")
    t.add_argument("--qty", type=int, default=1)
    t.add_argument("--volume", type=int, default=700)
    t.set_defaults(fn=cmd_tax)

    sub.add_parser("test-telegram").set_defaults(fn=cmd_test_telegram)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
