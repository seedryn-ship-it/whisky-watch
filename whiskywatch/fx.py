"""환율: 통화 -> KRW, 그리고 USD -> KRW (소액면세 150달러 판정용)."""

from __future__ import annotations

import time
from typing import Callable

import requests

FRANKFURTER = "https://api.frankfurter.dev/v1/latest?base=USD&symbols=KRW,{syms}"
ER_API = "https://open.er-api.com/v6/latest/USD"
MAX_CACHE_AGE = 3 * 24 * 3600


class FxError(Exception):
    pass


def _fetch_usd_table(currencies: list[str], http_get: Callable | None = None) -> dict[str, float]:
    """USD 기준 {통화: 1USD 당 수량} (KRW 포함)."""
    http_get = http_get or (lambda url: requests.get(url, timeout=15).json())
    need = sorted(set(currencies) | {"KRW", "USD"})
    syms = ",".join(c for c in need if c != "USD")
    errors = []
    for url, extract in (
        (FRANKFURTER.format(syms=syms), lambda d: d["rates"]),
        (ER_API, lambda d: d["rates"]),
    ):
        try:
            rates = dict(extract(http_get(url)))
            rates["USD"] = 1.0
            if all(c in rates for c in need):
                return {c: float(rates[c]) for c in need}
        except Exception as e:  # 네트워크/파싱 오류 -> 다음 소스
            errors.append(f"{url}: {e}")
    raise FxError("환율 조회 실패: " + " | ".join(errors))


def get_rates(
    currencies: list[str],
    state: dict,
    *,
    http_get: Callable | None = None,
    now: float | None = None,
) -> dict[str, float]:
    """{통화: KRW 환율}. 실패 시 state['fx'] 캐시(최대 3일)를 사용."""
    now = now or time.time()
    try:
        table = _fetch_usd_table(currencies, http_get)
        state["fx"] = {"t": now, "table": table}
    except FxError:
        cache = state.get("fx")
        if not cache or now - cache["t"] > MAX_CACHE_AGE:
            raise
        table = cache["table"]
        missing = [c for c in currencies if c not in table]
        if missing:
            raise
    krw_per_usd = table["KRW"]
    out = {c: krw_per_usd / table[c] for c in set(currencies) | {"USD"}}
    out["KRW"] = 1.0
    return out
