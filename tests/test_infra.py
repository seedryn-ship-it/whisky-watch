"""store / fx / notify / fetchers 단위 테스트 (fetchers 는 로컬 HTTP 서버 사용)."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from whiskywatch import fetchers
from whiskywatch.fetchers import BlockedError, FetchError, PlaywrightFetcher, RequestsFetcher
from whiskywatch.fx import FxError, get_rates
from whiskywatch.notify import TelegramNotifier, esc
from whiskywatch.store import DAY, History, State, lookup_baseline

NOW = 1_800_000_000


# ------------------------------------------------------------------ store

def row(t, landed, shop="a", url="u1", stock=True, key="k", fam="f"):
    return {"t": t, "shop": shop, "url": url, "title": "x", "key": key, "fam": fam, "landed": landed, "stock": stock}


def test_record_gating_by_change_and_heartbeat(tmp_path):
    h = History(tmp_path / "h.jsonl")
    assert h.record(row(NOW, 100_000), heartbeat_hours=6, change_pct=0.5)
    assert not h.record(row(NOW + 3600, 100_100), heartbeat_hours=6, change_pct=0.5)  # +0.1%, 1h
    assert h.record(row(NOW + 3600, 101_000), heartbeat_hours=6, change_pct=0.5)  # +1%
    assert h.record(row(NOW + 3600 + 7 * 3600, 101_000), heartbeat_hours=6, change_pct=0.5)  # heartbeat
    assert h.record(row(NOW + 3600 + 7 * 3600 + 60, 101_000, stock=False), heartbeat_hours=6, change_pct=0.5)  # 재고 변화


def test_save_prunes_old_rows_and_is_noop_without_changes(tmp_path):
    p = tmp_path / "h.jsonl"
    h = History(p)
    h.record(row(NOW - 400 * DAY, 1), heartbeat_hours=0, change_pct=0)
    h.record(row(NOW, 2, url="u2"), heartbeat_hours=0, change_pct=0)
    assert h.save(365, NOW)
    assert len(p.read_text().splitlines()) == 1
    assert not History(p).save(365, NOW)


def test_baseline_median_uses_daily_min_and_key_fallback(tmp_path):
    h = History(tmp_path / "h.jsonl")
    # 같은 날 여러 번 관측돼도 (샵,URL,일) 당 최저 1건만 반영
    for i, v in enumerate([300, 200, 250]):
        h.record(row(NOW - 3600 * i, v), heartbeat_hours=0, change_pct=0)
    for d in range(1, 4):
        h.record(row(NOW - d * DAY, 100 * (d + 2), url=f"x{d}", key="other", fam="k"), heartbeat_hours=0, change_pct=0)
    idx = h.build_index(NOW, 180)
    assert sorted(idx["k"]) == [200, 300, 400, 500]
    b = lookup_baseline(idx, ["k"], min_samples=4)
    assert b.median_krw == 350 and b.samples == 4 and b.source == "history"
    assert lookup_baseline(idx, ["k"], min_samples=5) is None
    fb = lookup_baseline(idx, ["nope", "k"], min_samples=4)  # 첫 키에 표본 없으면 다음 키
    assert fb.key == "k"


def test_state_roundtrip_and_no_rewrite_when_unchanged(tmp_path):
    s = State(tmp_path / "s.json")
    s.alerts["a|b"] = {"t": 1, "landed": 2}
    assert s.save()
    s2 = State(tmp_path / "s.json")
    assert s2.alerts["a|b"]["landed"] == 2 and not s2.save()


# ------------------------------------------------------------------ fx

def _frank(url):
    if "frankfurter" in url:
        return {"rates": {"KRW": 1400.0, "GBP": 0.75, "EUR": 0.90}}
    raise AssertionError(url)


def test_fx_rates_from_usd_table():
    state = {}
    r = get_rates(["GBP", "EUR"], state, http_get=_frank, now=NOW)
    assert r["USD"] == pytest.approx(1400.0)
    assert r["GBP"] == pytest.approx(1400 / 0.75)
    assert r["EUR"] == pytest.approx(1400 / 0.90)
    assert state["fx"]["t"] == NOW


def test_fx_fallback_to_second_source_then_cache():
    def flaky(url):
        if "frankfurter" in url:
            raise RuntimeError("down")
        return {"rates": {"KRW": 1300.0, "GBP": 0.80}}

    state = {}
    assert get_rates(["GBP"], state, http_get=flaky, now=NOW)["GBP"] == pytest.approx(1300 / 0.8)

    def dead(url):
        raise RuntimeError("down")

    # 캐시 사용 (3일 이내)
    assert get_rates(["GBP"], state, http_get=dead, now=NOW + DAY)["GBP"] == pytest.approx(1300 / 0.8)
    with pytest.raises(FxError):
        get_rates(["GBP"], state, http_get=dead, now=NOW + 4 * DAY)
    with pytest.raises(FxError):
        get_rates(["GBP"], {}, http_get=dead, now=NOW)


# ------------------------------------------------------------------ notify

class _Resp:
    def __init__(self, code):
        self.status_code = code


def test_telegram_payload_chunking_and_html_escape():
    calls = []
    n = TelegramNotifier("TOK", "123", http_post=lambda url, payload: calls.append((url, payload)) or _Resp(200))
    assert n.send("x" * 8000)
    assert len(calls) == 3
    url, payload = calls[0]
    assert url == "https://api.telegram.org/botTOK/sendMessage"
    assert payload["chat_id"] == "123" and payload["parse_mode"] == "HTML" and payload["disable_web_page_preview"]
    assert esc("A&B <c>") == "A&amp;B &lt;c&gt;"


def test_telegram_failure_returns_false():
    assert not TelegramNotifier("t", "c", http_post=lambda u, p: _Resp(400)).send("hi")

    def boom(u, p):
        raise requests.ConnectionError()

    assert not TelegramNotifier("t", "c", http_post=boom).send("hi")


# ------------------------------------------------------------------ fetchers

class _H(BaseHTTPRequestHandler):
    flaky_hits = 0
    seen: list = []

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        _H.seen.append(self.path)
        if self.path == "/robots.txt":
            return self._send(200, "User-agent: *\nDisallow: /private", "text/plain")
        if self.path.startswith("/private"):
            return self._send(200, "<html>secret</html>")
        if self.path == "/forbidden":
            return self._send(403, "no")
        if self.path == "/cf":
            return self._send(200, "<html><title>Just a moment...</title>cf-chl-bypass</html>")
        if self.path == "/missing":
            return self._send(404, "nope")
        if self.path == "/flaky":
            _H.flaky_hits += 1
            return self._send(500, "err") if _H.flaky_hits == 1 else self._send(200, "<html>ok after retry</html>")
        if self.path == "/js":
            return self._send(
                200,
                "<html><body><div id=x></div><script>document.getElementById('x').textContent='rendered-by-js'</script></body></html>",
            )
        return self._send(200, "<html><body>" + "hello " * 5 + "</body></html>")


@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setattr(fetchers.time, "sleep", lambda s: None)
    _H.flaky_hits, _H.seen = 0, []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def make_fetcher(**kw):
    return RequestsFetcher("test-agent", timeout=5, delay=(0, 0), **kw)


def test_requests_fetcher_basic_and_retry_on_500(server):
    f = make_fetcher()
    assert "hello" in f.get(server + "/ok")
    assert "ok after retry" in f.get(server + "/flaky")


def test_requests_fetcher_blocked_and_errors(server):
    f = make_fetcher()
    with pytest.raises(BlockedError):
        f.get(server + "/forbidden")
    with pytest.raises(BlockedError):
        f.get(server + "/cf")
    with pytest.raises(FetchError) as e:
        f.get(server + "/missing")
    assert not isinstance(e.value, BlockedError)


def test_robots_txt_is_respected_and_can_be_disabled(server):
    f = make_fetcher()
    with pytest.raises(FetchError, match="robots"):
        f.get(server + "/private/page")
    assert "/private/page" not in _H.seen  # 실제 요청조차 보내지 않음
    assert "secret" in make_fetcher(respect_robots=False).get(server + "/private/page")


def test_playwright_fetcher_renders_javascript(server):
    pf = PlaywrightFetcher("test-agent", timeout=20, delay=(0, 0))
    try:
        try:
            html = pf.get(server + "/js")
        except FetchError as e:
            pytest.skip(f"chromium 을 실행할 수 없는 환경: {e}")
        assert "rendered-by-js" in html
        with pytest.raises(BlockedError):
            pf.get(server + "/forbidden")
    finally:
        pf.close()
