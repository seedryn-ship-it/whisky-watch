"""run_once 전체 흐름 테스트 (네트워크 없이 가짜 fetcher/notifier 사용)."""

import json
from pathlib import Path

from whiskywatch.config import AlertConfig, Config, ShopConfig, StorageConfig
from whiskywatch.fetchers import BlockedError
from whiskywatch.monitor import run_once
from whiskywatch.normalize import analyze
from whiskywatch.store import History, State
from whiskywatch.tax import compute_landed_cost

FX = Path(__file__).parent / "fixtures"
NOW = 1_800_000_000
RATES = {"GBP": 1800.0, "EUR": 1500.0, "USD": 1400.0, "KRW": 1.0}


class FakeFetcher:
    def __init__(self, html=None, exc=None):
        self.html, self.exc, self.urls = html, exc, []

    def get(self, url):
        self.urls.append(url)
        if self.exc:
            raise self.exc
        return self.html


class FakePool:
    def __init__(self, fetcher):
        self.fetcher = fetcher

    def get(self, kind):
        return self.fetcher

    def close(self):
        pass


class Rec:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    def send(self, text):
        self.sent.append(text)
        return self.ok


def make_cfg(tmp_path, **alert):
    return Config(
        watchlist=["springbank", "hazelburn"],
        shops=[
            ShopConfig(
                id="s1", name="Test Shop", currency="GBP", search_url="https://shop.example/search?q={q}",
                strip_vat=0.20, ship_base=30, ship_extra=0,
            )
        ],
        alert=AlertConfig(min_samples=3, discount_pct=5, **alert),
        storage=StorageConfig(
            history_path=str(tmp_path / "h.jsonl"), state_path=str(tmp_path / "s.json")
        ),
    )


def seed_history(cfg, title, landed_values, shop="other", days=None):
    a = analyze(title, watchlist=cfg.watchlist, allowed_volumes_ml=cfg.allowed_volumes_ml)
    h = History(cfg.storage.history_path)
    for i, v in enumerate(landed_values):
        h.record(
            {"t": NOW - (i + 1) * 86400, "shop": shop, "url": f"https://{shop}.example/{i}", "title": title,
             "key": a.key, "fam": a.family_key, "landed": v, "item": 0, "cur": "GBP", "stock": True},
            heartbeat_hours=0, change_pct=0,
        )
    h.save(365, NOW)
    return a


def run(cfg, html, *, notifier=None, dry=False, fetcher=None):
    history, state = History(cfg.storage.history_path), State(cfg.storage.state_path)
    notifier = notifier or Rec()
    fetcher = fetcher or FakeFetcher(html)
    summary = run_once(cfg, history, state, notifier, FakePool(fetcher), dry_run=dry, now=NOW, rates=RATES)
    if not dry:
        history.save(365, NOW)
        state.save()
    return summary, notifier, history, state


LISTING = (FX / "jsonld_listing.html").read_text(encoding="utf-8")


def test_expected_landed_cost_for_fixture_item(tmp_path):
    # £119.95 (VAT 20% 제외 -> £99.96) + 배송 £30, 1 GBP = 1800원
    item = 119.95 / 1.2 * 1800
    lc = compute_landed_cost(item, 30 * 1800, 1400.0)
    assert lc.exempt  # 약 $128 < $150
    assert 402_000 < lc.total_krw < 403_000


def test_first_run_records_but_does_not_alert_and_sends_start_message(tmp_path):
    cfg = make_cfg(tmp_path)
    s, n, history, state = run(cfg, LISTING)
    assert s.alerts_sent == 0 and s.no_baseline == 2 and s.observations == 2  # 로컬발리 + 15년 (롱로우는 이 테스트 watchlist 밖)
    assert s.recorded == len(history.rows) == 2
    assert any("시작" in m for m in n.sent) and "started" in state.data
    titles = {r["title"] for r in history.rows}
    assert not any("Miniature" in t or "Glass" in t or "Glenfiddich" in t for t in titles)


def test_alert_fires_when_landed_cost_far_below_history_median(tmp_path):
    cfg = make_cfg(tmp_path)
    seed_history(cfg, "Springbank 10 Year Old Local Barley 2026 Release 70cl", [520_000, 500_000, 510_000, 505_000])
    s, n, _, state = run(cfg, LISTING)
    alerts = [m for m in n.sent if "Local Barley" in m and "기준" in m]
    assert s.alerts_sent == 1 and len(alerts) == 1
    msg = alerts[0]
    assert "Test Shop" in msg and "소액면세" in msg and "₩402" in msg
    assert "표본 4건" in msg and "https://shop.example/p/springbank-10-local-barley" in msg
    assert "-2" in msg  # 약 -21% 할인
    assert "s1|https://shop.example/p/springbank-10-local-barley" in state.alerts


def test_no_alert_when_not_cheap_enough(tmp_path):
    cfg = make_cfg(tmp_path)
    seed_history(cfg, "Springbank 10 Year Old Local Barley 2026 Release 70cl", [410_000] * 4)  # 402k 는 -2%
    s, n, *_ = run(cfg, LISTING)
    assert s.alerts_sent == 0


def test_out_of_stock_items_never_alert(tmp_path):
    cfg = make_cfg(tmp_path)
    seed_history(cfg, "Springbank 15 Year Old 46% 70cl", [900_000] * 5)
    s, n, *_ = run(cfg, LISTING)
    assert not any("Springbank 15" in m and "기준" in m for m in n.sent)


def test_realert_suppressed_then_allowed_after_further_drop(tmp_path):
    cfg = make_cfg(tmp_path)
    seed_history(cfg, "Springbank 10 Year Old Local Barley 2026 Release 70cl", [520_000] * 4)
    run(cfg, LISTING)
    s2, n2, *_ = run(cfg, LISTING)  # 같은 가격 -> 재알림 없음
    assert s2.alerts_sent == 0
    cheaper = LISTING.replace('"119.95"', '"104.95"')  # 추가 하락
    s3, n3, *_ = run(cfg, cheaper)
    assert s3.alerts_sent == 1


def test_notifier_failure_does_not_mark_alert_as_sent(tmp_path):
    cfg = make_cfg(tmp_path)
    seed_history(cfg, "Springbank 10 Year Old Local Barley 2026 Release 70cl", [520_000] * 4)
    _, _, _, state = run(cfg, LISTING, notifier=Rec(ok=False))
    assert not state.alerts
    s, *_ = run(cfg, LISTING)  # 다음 회차에 재시도되어 발송
    assert s.alerts_sent == 1


def test_seed_price_used_when_history_missing(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.seed_prices = [{"match": "springbank 10 local barley", "krw": 600_000}]
    s, n, *_ = run(cfg, LISTING)
    assert s.alerts_sent == 1
    assert any("시드" in m for m in n.sent)


def test_family_key_fallback_when_year_specific_history_is_thin(tmp_path):
    cfg = make_cfg(tmp_path)
    # 2024 릴리스 이력만 존재 -> 2026 상품은 family key(연도 제외)로 비교
    seed_history(cfg, "Springbank 10 Year Old Local Barley 2024 Release 70cl", [520_000] * 4)
    s, *_ = run(cfg, LISTING)
    assert s.alerts_sent == 1


def test_dry_run_writes_nothing(tmp_path):
    cfg = make_cfg(tmp_path)
    seed_history(cfg, "Springbank 10 Year Old Local Barley 2026 Release 70cl", [520_000] * 4)
    before = Path(cfg.storage.history_path).read_text()
    s, n, history, state = run(cfg, LISTING, dry=True)
    assert s.alerts_sent == 1  # 미리보기는 나오지만
    assert Path(cfg.storage.history_path).read_text() == before
    assert not Path(cfg.storage.state_path).exists() and not state.alerts


def test_shop_failure_counts_then_alerts_once_then_recovers(tmp_path):
    cfg = make_cfg(tmp_path, fail_alert_after=3)
    bad = FakeFetcher(exc=BlockedError("HTTP 403"))
    sent = []
    for _ in range(4):
        s, n, _, state = run(cfg, None, fetcher=bad)
        sent += n.sent
    assert s.shops_ok == 0 and "s1" in s.shops_failed
    assert sum("점검 필요" in m for m in sent) == 1  # 3회째에 한 번만
    assert state.health["s1"]["fails"] == 4
    s, *_ , state = run(cfg, LISTING)
    assert state.health["s1"]["fails"] == 0 and not state.health["s1"]["notified"]


def test_empty_parse_is_treated_as_failure(tmp_path):
    cfg = make_cfg(tmp_path)
    s, *_ = run(cfg, "<html><body>redesigned page</body></html>")
    assert "s1" in s.shops_failed and s.shops_ok == 0


def test_price_sanity_filters_shipping_lines_misparsed_as_price(tmp_path):
    cfg = make_cfg(tmp_path)
    html = LISTING.replace('"119.95"', '"4.95"')
    s, _, history, _ = run(cfg, html)
    assert all("Local Barley" not in r["title"] for r in history.rows)


def test_history_is_json_lines_and_compact(tmp_path):
    cfg = make_cfg(tmp_path)
    run(cfg, LISTING)
    n_first = len(Path(cfg.storage.history_path).read_text().splitlines())
    run(cfg, LISTING)  # 가격 변동/하트비트 없음 -> 추가 기록 없음
    lines = Path(cfg.storage.history_path).read_text().splitlines()
    assert len(lines) == n_first
    row = json.loads(lines[0])
    assert {"t", "shop", "url", "key", "fam", "landed"} <= set(row)


def test_baseline_builds_naturally_over_days_then_alerts_on_price_drop(tmp_path):
    """시드 없이 실제 실행 흐름만으로: 8일간 가격 유지 -> 기준가 형성 -> 9일째 하락 시 알림."""
    cfg = make_cfg(tmp_path)  # min_samples=3
    normal = LISTING
    alerts_by_day = []
    for day in range(9):
        html = normal if day < 8 else LISTING.replace('"119.95"', '"94.95"')
        history, state = History(cfg.storage.history_path), State(cfg.storage.state_path)
        n = Rec()
        s = run_once(cfg, history, state, n, FakePool(FakeFetcher(html)), now=NOW + day * 86400, rates=RATES)
        history.save(365, NOW + day * 86400)
        state.save()
        alerts_by_day.append(s.alerts_sent)
    assert alerts_by_day[:8] == [0] * 8  # 이력이 쌓이는 동안, 그리고 가격이 그대로일 땐 조용
    assert alerts_by_day[8] == 1
