"""probe: 로컬 HTTP 서버를 가짜 샵으로 삼아 Shopify/차단/robots 케이스를 확인."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from whiskywatch import __main__ as cli
from whiskywatch.probe import Prober

PRODUCTS = {"products": [
    {"title": "Springbank 15 Year Old", "vendor": "Springbank", "variants": [{"price": "95.00"}]},
    {"title": "Random Vodka", "vendor": "Foo", "variants": [{"price": "20.00"}]},
]}


def make_server(routes):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            path = self.path.split("?")[0]
            status, ctype, body = routes.get(path, (404, "text/plain", "nf"))
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body.encode())

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture(autouse=True)
def _noproxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def probe(base):
    return Prober("test-agent", timeout=5, delay=(0, 0)).probe(base)


def test_shopify_catalog_is_detected():
    srv, base = make_server({
        "/robots.txt": (200, "text/plain", "User-agent: *\nDisallow: /search\n"),
        "/": (200, "text/html", '<html><script src="https://cdn.shopify.com/x.js"></script></html>'),
        "/products.json": (200, "application/json", json.dumps(PRODUCTS)),
        "/collections/springbank/products.json": (200, "application/json", json.dumps(PRODUCTS)),
    })
    r = probe(base)
    srv.shutdown()
    assert r["verdict"] == "가능" and r["platform"] == "shopify" and r["watched"] == 1 and r["handle"] == "springbank"
    assert "Springbank 15" in r["sample"]


def test_robots_blocks_products_json():
    srv, base = make_server({
        "/robots.txt": (200, "text/plain", "User-agent: *\nDisallow: /products.json\n"),
        "/": (200, "text/html", "<html>cdn.shopify.com</html>"),
        "/products.json": (200, "application/json", json.dumps(PRODUCTS)),
    })
    r = probe(base)
    srv.shutdown()
    assert r["verdict"] == "수동" and "robots.txt" in r["detail"]


def test_403_is_reported_as_blocked():
    srv, base = make_server({"/robots.txt": (404, "text/plain", ""), "/": (403, "text/html", "denied")})
    r = probe(base)
    srv.shutdown()
    assert r["verdict"] == "차단" and "403" in r["detail"]


def test_challenge_page_with_200_is_blocked():
    srv, base = make_server({"/": (200, "text/html", "<html><title>Just a moment...</title></html>")})
    r = probe(base)
    srv.shutdown()
    assert r["verdict"] == "차단"


def test_non_shopify_needs_manual_check():
    srv, base = make_server({"/": (200, "text/html", "<html>Powered by Magento mage-init</html>")})
    r = probe(base)
    srv.shutdown()
    assert r["verdict"] == "수동" and r["platform"] == "magento"


def test_unreachable_is_failure():
    r = probe("http://127.0.0.1:9")
    assert r["verdict"] == "실패"


def test_cli_probe_prints_summary(tmp_path, capsys):
    srv, base = make_server({"/": (200, "text/html", "<html>cdn.shopify.com</html>"),
                             "/products.json": (200, "application/json", json.dumps(PRODUCTS))})
    cfg = tmp_path / "config.yaml"
    cfg.write_text("watchlist: [springbank]\nshops: []\nhttp: {delay_min: 0, delay_max: 0}\n", encoding="utf-8")
    assert cli.main(["--config", str(cfg), "probe", "--domains", base]) == 0
    srv.shutdown()
    out = capsys.readouterr().out
    assert "요약" in out and "바로 추가 가능 (1)" in out
