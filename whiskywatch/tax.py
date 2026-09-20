"""한국 개인 해외직구 위스키의 도착가(landed cost) 계산.

계산식 (관세청 '해외직구물품 예상세액 조회' 안내와 동일한 구조)
  1) 관세   = 과세가격 x 관세율
  2) 주세   = (과세가격 + 관세) x 72%
  3) 교육세 = 주세 x 30%
  4) 부가세 = (과세가격 + 관세 + 주세 + 교육세) x 10%

과세가격
  - 물품가격 150달러 이하: 물품가격만 (한국행 운임/보험료 제외)
  - 150달러 초과        : 물품가격 + 한국행 운임/보험료

소액물품 면세 (자가사용, 1L 이하 1병, 물품가격 150달러 이하)
  - 관세·부가세만 면제, 주세·교육세는 그대로 부과 (주류 특칙)

주의: 관세율은 기본 20%. 한-영/한-EU FTA 원산지증명서(C/O)를 받을 수 있으면 0%이므로
config.yaml의 tax.tariff_rate 로 조정하세요.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TaxConfig:
    tariff_rate: float = 0.20
    liquor_tax_rate: float = 0.72
    education_tax_rate: float = 0.30
    vat_rate: float = 0.10
    exemption_usd: float = 150.0
    exemption_max_bottles: int = 1
    exemption_max_volume_ml: int = 1000


@dataclass(frozen=True)
class LandedCost:
    item_krw: int
    shipping_krw: int
    customs_value_krw: int
    duty_krw: int
    liquor_tax_krw: int
    education_tax_krw: int
    vat_krw: int
    total_krw: int
    exempt: bool  # 소액면세(관세·부가세 면제) 적용 여부

    @property
    def tax_krw(self) -> int:
        return self.duty_krw + self.liquor_tax_krw + self.education_tax_krw + self.vat_krw


def compute_landed_cost(
    item_krw: float,
    shipping_krw: float,
    usd_krw: float,
    *,
    volume_ml: int = 700,
    qty: int = 1,
    cfg: TaxConfig | None = None,
) -> LandedCost:
    """item_krw: 상품 합계(수량 반영), shipping_krw: 한국까지의 배송비 합계."""
    cfg = cfg or TaxConfig()
    if usd_krw <= 0:
        raise ValueError("usd_krw must be positive")

    item_usd = item_krw / usd_krw
    exempt = (
        qty <= cfg.exemption_max_bottles
        and volume_ml <= cfg.exemption_max_volume_ml
        and item_usd <= cfg.exemption_usd
    )

    customs_value = item_krw if exempt else item_krw + shipping_krw
    duty = 0.0 if exempt else customs_value * cfg.tariff_rate
    liquor = (customs_value + duty) * cfg.liquor_tax_rate
    edu = liquor * cfg.education_tax_rate
    vat = 0.0 if exempt else (customs_value + duty + liquor + edu) * cfg.vat_rate

    duty_i, liquor_i, edu_i, vat_i = (round(x) for x in (duty, liquor, edu, vat))
    item_i, ship_i = round(item_krw), round(shipping_krw)
    total = item_i + ship_i + duty_i + liquor_i + edu_i + vat_i
    return LandedCost(
        item_krw=item_i,
        shipping_krw=ship_i,
        customs_value_krw=round(customs_value),
        duty_krw=duty_i,
        liquor_tax_krw=liquor_i,
        education_tax_krw=edu_i,
        vat_krw=vat_i,
        total_krw=total,
        exempt=exempt,
    )


def all_in_cost(item_krw: float, shipping_krw: float = 0.0) -> LandedCost:
    """관세·주세·교육세·부가세가 이미 가격에 포함돼 판매되는 샵(예: Winemoa)용. 세금을 더하지 않는다."""
    item_i, ship_i = round(item_krw), round(shipping_krw)
    return LandedCost(
        item_krw=item_i, shipping_krw=ship_i, customs_value_krw=item_i,
        duty_krw=0, liquor_tax_krw=0, education_tax_krw=0, vat_krw=0,
        total_krw=item_i + ship_i, exempt=False,
    )
