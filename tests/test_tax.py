from whiskywatch.tax import TaxConfig, compute_landed_cost

USD_KRW = 1400.0


def test_exempt_one_bottle_under_150_usd_only_liquor_and_education_tax():
    # 상품 13만원(약 93달러) -> 소액면세: 관세·부가세 0, 주세·교육세만
    lc = compute_landed_cost(130_000, 45_000, USD_KRW, volume_ml=700)
    assert lc.exempt
    assert lc.customs_value_krw == 130_000  # 한국행 운임 제외
    assert lc.duty_krw == 0 and lc.vat_krw == 0
    assert lc.liquor_tax_krw == 93_600  # 130,000 x 72%
    assert lc.education_tax_krw == 28_080  # 93,600 x 30%
    assert lc.total_krw == 130_000 + 45_000 + 93_600 + 28_080


def test_over_150_usd_all_taxes_on_item_plus_shipping():
    lc = compute_landed_cost(300_000, 45_000, USD_KRW, volume_ml=700)
    assert not lc.exempt
    cif = 345_000
    duty = cif * 0.20
    liquor = (cif + duty) * 0.72
    edu = liquor * 0.30
    vat = (cif + duty + liquor + edu) * 0.10
    assert lc.duty_krw == round(duty)
    assert lc.liquor_tax_krw == round(liquor)
    assert lc.education_tax_krw == round(edu)
    assert lc.vat_krw == round(vat)
    assert lc.total_krw == 300_000 + 45_000 + round(duty) + round(liquor) + round(edu) + round(vat)


def test_two_bottles_never_exempt_even_if_cheap():
    lc = compute_landed_cost(200_000, 50_000, USD_KRW, volume_ml=700, qty=2)
    assert not lc.exempt and lc.duty_krw > 0 and lc.vat_krw > 0


def test_over_one_litre_not_exempt():
    assert not compute_landed_cost(100_000, 40_000, USD_KRW, volume_ml=1500).exempt


def test_fta_zero_tariff():
    lc = compute_landed_cost(300_000, 45_000, USD_KRW, cfg=TaxConfig(tariff_rate=0.0))
    assert lc.duty_krw == 0
    assert lc.liquor_tax_krw == round(345_000 * 0.72)


def test_threshold_boundary_uses_item_price_only():
    # 물품가 정확히 150달러는 면세, 1원 초과는 과세 (배송비는 판정에 영향 없음)
    assert compute_landed_cost(150 * USD_KRW, 90_000, USD_KRW).exempt
    assert not compute_landed_cost(150 * USD_KRW + 1, 90_000, USD_KRW).exempt
