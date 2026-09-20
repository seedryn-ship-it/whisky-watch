"""CLI 통합 테스트: 로컬 HTTP 서버를 가짜 샵으로 삼아 diagnose / run / tax 를 끝까지 실행."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from whiskywatch import __main__ as cli
from whiskywatch import fetchers, monitor
from whiskywatch.store import History

FIXTURE = (Path(__file__).parent / "fixtures" / "jsonld_listing.html").read_bytes()
RATES = {"GBP": 1800.0, "EUR": 1500.0, "USD": 1400.0, "KRW": 1.0}


class _Shop(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/robots.txt":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(FIXTURE)))
        self.end_headers()
        self.wfile.write(FIXTURE)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setattr(fetchers.time, "sleep", lambda s: None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Shop)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""
watchlist: [springbank, longrow]
alert: {{min_samples: 3, discount_pct: 5}}
seed_prices:
  - {{match: "springbank 10 local barley", krw: 600000}}
shops:
  - id: fake
    name: Fake Shop
    currency: GBP
    search_url: "http://127.0.0.1:{port}/search?q={{q}}"
    strip_vat: 0.20
    ship_base: 30
http: {{delay_min: 0, delay_max: 0}}
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(monitor, "get_rates", lambda currencies, state: dict(RATES))

    class Rec:
        sent = []

        def send(self, text):
            Rec.sent.append(text)
            return True

    Rec.sent = []
    monkeypatch.setattr(cli.TelegramNotifier, "from_env", classmethod(lambda cls: Rec()))
    yield cfg, Rec
    srv.shutdown()


def test_diagnose_reports_parsed_products(env, capsys):
    cfg, _ = env
    assert cli.main(["--config", str(cfg), "diagnose"]) == 0
    out = capsys.readouterr().out
    assert "Fake Shop" in out and "관심 상품" in out and "Local Barley" in out
    assert "key=springbank|10|local-barley|y2026|700ml" in out


def test_diagnose_skips_disabled_shops_and_does_not_fail(env, capsys):
    cfg, _ = env
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace("shops:\n", """shops:
  - id: off
    name: Off Shop
    currency: GBP
    enabled: false
    search_url: "http://127.0.0.1:9/search?q={q}"
""", 1),
        encoding="utf-8",
    )
    assert cli.main(["--config", str(cfg), "diagnose"]) == 0
    out = capsys.readouterr().out
    assert "Off Shop" in out and "건너뜀" in out and "수집 실패" not in out


def test_run_dry_run_prints_preview_and_writes_nothing(env, capsys):
    cfg, _ = env
    assert cli.main(["--config", str(cfg), "run", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "알림 미리보기" in out and "Springbank 10 Year Old Local Barley" in out and "알림 1/1" in out
    assert not (cfg.parent / "data").exists()


def test_run_real_sends_alert_and_persists_state(env, capsys):
    cfg, Rec = env
    assert cli.main(["--config", str(cfg), "run"]) == 0
    assert any("Local Barley" in m and "기준" in m for m in Rec.sent)
    hist = History(cfg.parent / "data" / "history.jsonl")
    assert len(hist.rows) >= 2  # 로컬발리, 15년, 롱로우
    assert (cfg.parent / "data" / "state.json").exists()
    Rec.sent.clear()
    assert cli.main(["--config", str(cfg), "run"]) == 0
    assert not any("기준" in m and "Local Barley" in m for m in Rec.sent)  # 중복 알림 없음


def test_tax_command(env, capsys, monkeypatch):
    cfg, _ = env
    monkeypatch.setattr(cli, "get_rates", lambda currencies, state: dict(RATES))
    assert cli.main(["--config", str(cfg), "tax", "--price", "120", "--currency", "GBP",
                     "--strip-vat", "0.2", "--shipping", "30"]) == 0
    out = capsys.readouterr().out
    assert "소액면세 적용: 예" in out and "도착가 합계" in out
