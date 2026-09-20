"""가격 이력(JSONL)과 상태(JSON) 저장 + 기준가(중앙값) 계산.

이력은 git 에 커밋되어도 부담 없도록 '변동이 있거나 heartbeat 시간이 지났을 때만' 기록합니다.
"""

from __future__ import annotations

import json
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

DAY = 86400


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


class History:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.rows: list[dict] = []
        self._last: dict[str, dict] = {}  # "shop|url" -> 마지막 행
        self._new: list[dict] = []
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.rows.append(row)
                self._last[f"{row['shop']}|{row['url']}"] = row

    # --------------------------------------------------------------- write
    def record(
        self,
        row: dict,
        *,
        heartbeat_hours: float,
        change_pct: float,
    ) -> bool:
        """변동/하트비트 조건을 만족할 때만 기록. 기록했으면 True."""
        k = f"{row['shop']}|{row['url']}"
        prev = self._last.get(k)
        if prev is not None:
            age_h = (row["t"] - prev["t"]) / 3600
            change = abs(row["landed"] - prev["landed"]) / max(prev["landed"], 1) * 100
            if age_h < heartbeat_hours and change < change_pct and row.get("stock") == prev.get("stock"):
                return False
        self.rows.append(row)
        self._new.append(row)
        self._last[k] = row
        return True

    def save(self, retention_days: int, now: float | None = None) -> bool:
        """변경이 있을 때만 파일을 다시 씀. 오래된 행은 정리. 변경 여부 반환."""
        now = now or time.time()
        cutoff = now - retention_days * DAY
        pruned = [r for r in self.rows if r["t"] >= cutoff]
        changed = bool(self._new) or len(pruned) != len(self.rows)
        if not changed:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            for r in pruned:
                f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.rows = pruned
        self._new = []
        return True

    # ---------------------------------------------------------- baseline
    def build_index(self, now: float, days: int) -> dict[str, list[int]]:
        """{비교 키: [(상품·샵·일) 단위 최저 도착가, ...]} — full key 와 family key 모두로 색인."""
        cutoff = now - days * DAY
        daily: dict[tuple[str, str, str], dict] = {}
        for r in self.rows:
            if r["t"] < cutoff:
                continue
            slot = (r["shop"], r["url"], _day(r["t"]))
            if slot not in daily or r["landed"] < daily[slot]["landed"]:
                daily[slot] = r
        index: dict[str, list[int]] = defaultdict(list)
        for r in daily.values():
            index[r["key"]].append(r["landed"])
            if r.get("fam") and r["fam"] != r["key"]:
                index[r["fam"]].append(r["landed"])
        return index


@dataclass
class Baseline:
    median_krw: int
    samples: int
    key: str
    source: str  # "history" | "seed"


def lookup_baseline(
    index: dict[str, list[int]],
    keys: Iterable[str],
    min_samples: int,
    seeds: list[dict] | None = None,
    title_folded: str = "",
    term_matches=None,
) -> Baseline | None:
    for k in keys:
        vals = index.get(k, [])
        if len(vals) >= min_samples:
            return Baseline(int(statistics.median(vals)), len(vals), k, "history")
    for s in seeds or []:
        if term_matches and term_matches(title_folded, str(s["match"])):
            return Baseline(int(s["krw"]), 0, str(s["match"]), "seed")
    return None


class State:
    """알림 중복 방지, 샵 장애 카운터, 환율 캐시 등."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict = {"alerts": {}, "health": {}}
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                pass
        self._orig = json.dumps(self.data, sort_keys=True)

    @property
    def alerts(self) -> dict:
        return self.data.setdefault("alerts", {})

    @property
    def health(self) -> dict:
        return self.data.setdefault("health", {})

    def save(self) -> bool:
        cur = json.dumps(self.data, sort_keys=True)
        if cur == self._orig:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        self._orig = cur
        return True
