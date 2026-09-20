from whiskywatch.normalize import analyze, fold, parse_volume_ml, term_matches

WL = ["springbank", "springbank local barley", "hazelburn", "longrow", "kilkerran"]
VOLS = [700, 750, 1000]


def A(title):
    return analyze(title, watchlist=WL, allowed_volumes_ml=VOLS)


def test_local_barley_analysis():
    a = A("Springbank 10 Year Old Local Barley 2026 Release 70cl 55.6%")
    assert a.distillery == "springbank" and a.age == 10
    assert a.is_local_barley and a.year == 2026 and a.volume_ml == 700 and a.abv == 55.6
    assert a.key == "springbank|10|local-barley|y2026|700ml"
    assert a.family_key == "springbank|10|local-barley|700ml"


def test_bare_age_after_distillery_and_german_units():
    a = A("Kilkerran 12 46% 0,7l")
    assert a.age == 12 and a.volume_ml == 700
    b = A("Springbank 18 Jahre 46% 0,7 l")
    assert b.age == 18 and b.is_rare


def test_batch_and_cask_strength():
    a = A("Springbank 12 Year Old Cask Strength Batch 24 70cl")
    assert a.batch == 24 and "cask-strength" in a.tokens
    assert a.key.endswith("b24|700ml")


def test_vintage_is_rare_and_year_not_confused_with_age():
    a = A("Springbank 1997 Vintage 25 Year Old 70cl")
    assert a.year == 1997 and a.age == 25 and a.is_rare


def test_excludes_miniatures_glasses_and_bad_volumes():
    assert A("Springbank 10 Year Old 5cl Miniature") is None
    assert A("Springbank Glencairn Glass") is None
    assert A("Hazelburn 8 Year Old 20cl") is None
    assert A("Glenfiddich 12 Year Old 70cl") is None


def test_unknown_volume_assumed_700():
    a = A("Hazelburn 12 Year Old")
    assert a.volume_ml == 700 and a.volume_assumed


def test_term_matching_is_whole_word_and_accent_insensitive():
    assert term_matches(fold("Springbänk 10"), "springbank")
    assert not term_matches(fold("Springbankers Choice"), "springbank")
    assert term_matches(fold("Springbank 10 Local Barley"), "springbank local barley")
    assert not term_matches(fold("Springbank 10"), "springbank local barley")


def test_parse_volume():
    assert parse_volume_ml("70cl") == 700
    assert parse_volume_ml("0.7l") == 700
    assert parse_volume_ml("1l") == 1000
    assert parse_volume_ml("46% no volume") is None


def test_cask_strength_batches_are_split_by_abv():
    wl = ["springbank"]
    a = analyze("Springbank 12 Year Old Cask Strength 56.2% (2024 Release)", watchlist=wl)
    b = analyze("Springbank 12 Year Old Cask Strength 57.2% (2024 Release)", watchlist=wl)
    c = analyze("Springbank 12 Year Old Cask Strength 56.4% (2024 Release)", watchlist=wl)
    assert a.key != b.key and a.family_key != b.family_key  # 다른 배치는 서로의 기준가가 되지 않는다
    assert a.key == c.key  # 표기 오차 수준(56.2 vs 56.4)은 같은 상품
    assert "a56" in a.key and "a56" in a.family_key
    lb = analyze("Springbank 10 Year Old Local Barley 55.2% 2025", watchlist=wl)
    assert "a55" not in lb.key  # 도수를 안 적는 샵과도 비교되도록 Local Barley 는 연도로만 구분
