"""상품명 분석: 관심 제품 판별, 용량/도수/숙성연수 파싱, 가격 비교용 키 생성."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

DEFAULT_DISTILLERIES = ["springbank", "hazelburn", "longrow", "kilkerran"]
DEFAULT_EXCLUDE = [
    "miniature", "miniatures", "sample", "samples", "tasting set", "gift set", "glass",
    "glasses", "glencairn", "tumbler", "book", "poster", "t-shirt", "hoodie", "cap",
    "mini", "dram", "drams", "5cl", "3cl", "2cl", "1cl", "10cl", "20cl", "35cl",
]
DEFAULT_RARE_WORDS = [
    "private", "society", "single cask", "rundlets", "kilderkin", "kilderkins", "cage",
    "distillery exclusive", "vintage", "cask no", "hogshead", "butt", "1st fill",
]

_EDITION_TOKENS = [
    ("local-barley", r"\blocal\s+barley\b"),
    ("cask-strength", r"\bcask\s+strength\b|\bcs\b"),
    ("sherry", r"\bsherry\b|\boloroso\b|\bpedro\b"),
    ("port", r"\bport\b"),
    ("rum", r"\brum\b"),
    ("madeira", r"\bmadeira\b"),
    ("marsala", r"\bmarsala\b"),
    ("burgundy", r"\bburgundy\b"),
    ("green", r"\bgreen\b"),
    ("red", r"\bred\b"),
    ("rundlets", r"\brundlets?\b"),
]

_VOL_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(cl|ml|l|ltr|liter|litre)\b")
_ABV_RE = re.compile(r"(\d{2}(?:[.,]\d)?)\s*(?:%|vol|abv)")
_AGE_RE = re.compile(
    r"\b(\d{1,2})\s*[- ]?\s*(?:years?|yrs?|yo|y\.o\.?|jahre|jahr|ans|anni)\b(?:[- ]?old)?"
)
_YEAR_RE = re.compile(r"\b(19[4-9]\d|20[0-3]\d)\b")
_BATCH_RE = re.compile(r"\bbatch\s*(?:no\.?\s*)?(\d{1,3})\b")


def fold(text: str) -> str:
    """소문자화 + 악센트 제거 + 공백 정리."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


def parse_volume_ml(folded: str) -> int | None:
    for m in _VOL_RE.finditer(folded):
        val = float(m.group(1).replace(",", "."))
        unit = m.group(2)
        ml = val * 10 if unit == "cl" else val if unit == "ml" else val * 1000
        if 10 <= ml <= 5000:
            return int(round(ml))
    return None


def parse_abv(folded: str) -> float | None:
    m = _ABV_RE.search(folded)
    if not m:
        return None
    v = float(m.group(1).replace(",", "."))
    return v if 20 <= v <= 80 else None


def parse_age(folded: str, distillery: str | None) -> int | None:
    m = _AGE_RE.search(folded)
    if m:
        return int(m.group(1))
    if distillery:
        # "Springbank 15", "Kilkerran 12" 처럼 증류소명 바로 뒤에 숫자만 오는 경우
        m = re.search(rf"\b{re.escape(distillery)}\s+(\d{{1,2}})\b(?!\s*(?:cl|ml|l|%))", folded)
        if m:
            return int(m.group(1))
    return None


@dataclass
class Analysis:
    title: str
    distillery: str
    age: int | None
    abv: float | None
    volume_ml: int
    volume_assumed: bool
    year: int | None
    batch: int | None
    tokens: list[str] = field(default_factory=list)
    is_local_barley: bool = False
    is_rare: bool = False
    matched_term: str = ""

    @property
    def key(self) -> str:
        """가장 세밀한 비교 키 (연도/배치 포함)."""
        parts = [self.distillery, str(self.age or "nas"), *sorted(self.tokens)]
        if self.year:
            parts.append(f"y{self.year}")
        if self.batch:
            parts.append(f"b{self.batch}")
        parts.append(f"{self.volume_ml}ml")
        return "|".join(parts)

    @property
    def family_key(self) -> str:
        """연도/배치를 뺀 키. 세밀한 키의 이력이 부족할 때 폴백."""
        parts = [self.distillery, str(self.age or "nas"), *sorted(self.tokens), f"{self.volume_ml}ml"]
        return "|".join(parts)

    @property
    def label(self) -> str:
        return self.title


def _has_word(folded: str, word: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(fold(word))}(?![a-z0-9])", folded) is not None


def term_matches(folded_title: str, term: str) -> bool:
    """관심 키워드의 모든 단어가 상품명에 있으면 True (예: 'springbank local barley')."""
    words = re.findall(r"[a-z0-9]+", fold(term))
    return bool(words) and all(_has_word(folded_title, w) for w in words)


def analyze(
    title: str,
    *,
    watchlist: list[str],
    exclude_words: list[str] | None = None,
    allowed_volumes_ml: list[int] | None = None,
    distilleries: list[str] | None = None,
    rare_words: list[str] | None = None,
    default_volume_ml: int = 700,
) -> Analysis | None:
    """관심 제품이면 Analysis, 아니면 None (제외어/허용 용량 밖도 None)."""
    folded = fold(title)
    matched = next((t for t in watchlist if term_matches(folded, t)), None)
    if not matched:
        return None
    exclude = DEFAULT_EXCLUDE if exclude_words is None else exclude_words
    if any(_has_word(folded, w) for w in exclude):
        return None

    volume = parse_volume_ml(folded)
    if volume is not None and allowed_volumes_ml and volume not in allowed_volumes_ml:
        return None

    dists = distilleries or DEFAULT_DISTILLERIES
    distillery = next((d for d in dists if _has_word(folded, d)), None)
    if distillery is None:
        distillery = fold(matched).split(" ")[0]

    age = parse_age(folded, distillery)
    year_m = _YEAR_RE.search(folded)
    year = int(year_m.group(1)) if year_m else None
    # "12 year old" 의 12 가 아닌 4자리 연도만 취급하므로 age와 충돌하지 않음
    batch_m = _BATCH_RE.search(folded)
    tokens = [name for name, pat in _EDITION_TOKENS if re.search(pat, folded)]

    rare_list = DEFAULT_RARE_WORDS if rare_words is None else rare_words
    is_rare = (
        (age is not None and age >= 18)
        or (year is not None and year <= 2005)
        or any(_has_word(folded, w) for w in rare_list)
    )

    return Analysis(
        title=title.strip(),
        distillery=distillery,
        age=age,
        abv=parse_abv(folded),
        volume_ml=volume or default_volume_ml,
        volume_assumed=volume is None,
        year=year,
        batch=int(batch_m.group(1)) if batch_m else None,
        tokens=tokens,
        is_local_barley="local-barley" in tokens,
        is_rare=is_rare,
        matched_term=matched,
    )
