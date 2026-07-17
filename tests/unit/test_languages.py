"""normalize_language_code: shape validation + casing normalization."""

from newsflow.core.languages import normalize_language_code


def test_normalizes_casing():
    assert normalize_language_code("zh-cn") == "zh-CN"
    assert normalize_language_code("EN") == "en"
    assert normalize_language_code("ZH-TW") == "zh-TW"


def test_normalizes_script_subtag_and_underscore():
    assert normalize_language_code("zh_hans") == "zh-Hans"


def test_passes_plain_primary_codes():
    assert normalize_language_code("ja") == "ja"
    assert normalize_language_code("yue") == "yue"


def test_rejects_non_codes():
    # "chinese" saved fine before and then silently failed on every entry.
    for bad in ("chinese", "中文", "", "z", "en-", "en-US-x-foo-bar-baz"):
        assert normalize_language_code(bad) is None, bad


def test_trims_whitespace():
    assert normalize_language_code(" zh-CN ") == "zh-CN"


def test_rejects_codes_longer_than_db_columns():
    # Language-code columns are String(10) schema-wide; a well-formed but
    # longer code ("ca-valencia") must fail validation, not a PG insert.
    assert normalize_language_code("ca-valencia") is None
    assert normalize_language_code("cmn-Hans") == "cmn-Hans"  # 8 chars still fine


# ===== same_primary_language =====


def test_same_primary_language_matches_across_variants_and_casing():
    from newsflow.core.languages import same_primary_language

    assert same_primary_language("ZH", "zh-CN") is True
    assert same_primary_language("en", "EN-us") is True
    assert same_primary_language("de", "en") is False
    assert same_primary_language(None, "en") is False


# ===== text_clearly_in_language (translation short-circuit) =====


def test_chinese_text_detected_for_zh_target():
    from newsflow.core.languages import text_clearly_in_language

    text = "苹果公司今日发布全新产品线,定价策略引发市场热议"
    assert text_clearly_in_language(text, "zh-CN") is True


def test_japanese_text_not_claimed_as_chinese():
    from newsflow.core.languages import text_clearly_in_language

    text = "本日、東京で新製品が発表されました。価格はまだ未定です。"
    assert text_clearly_in_language(text, "zh-CN") is False  # kana present
    assert text_clearly_in_language(text, "ja") is True


def test_korean_text_detected_only_for_ko():
    from newsflow.core.languages import text_clearly_in_language

    text = "오늘 서울에서 신제품이 공개되었습니다 가격은 미정입니다"
    assert text_clearly_in_language(text, "ko") is True
    assert text_clearly_in_language(text, "zh-CN") is False


def test_latin_script_languages_are_never_claimed():
    from newsflow.core.languages import text_clearly_in_language

    text = "Apple announces a new product line with aggressive pricing"
    assert text_clearly_in_language(text, "en") is False  # en/de/fr share the script
    assert text_clearly_in_language(text, "de") is False


def test_zh_variant_boundary_blocks_the_shortcut():
    from newsflow.core.languages import text_clearly_in_language

    simplified = "苹果公司今日发布全新产品,这个定价让市场没想到"
    traditional = "蘋果公司今日發布全新產品,這個定價讓市場沒想到"
    # Simplified source + traditional target still needs the translator.
    assert text_clearly_in_language(simplified, "zh-TW") is False
    assert text_clearly_in_language(traditional, "zh-CN") is False
    # Same-variant pairs do short-circuit.
    assert text_clearly_in_language(simplified, "zh-CN") is True
    assert text_clearly_in_language(traditional, "zh-TW") is True


def test_short_text_is_never_claimed():
    from newsflow.core.languages import text_clearly_in_language

    assert text_clearly_in_language("苹果发布", "zh-CN") is False  # < 8 letters
