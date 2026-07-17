"""Tests for the FilterRule rule engine."""

import pytest
from newsflow.core.filter import FilterRule, parse_filter_field, parse_keyword_csv


def test_empty_rule_passes_everything():
    rule = FilterRule()
    assert rule.is_empty() is True
    assert rule.matches("Any text at all") is True
    assert rule.matches("") is True


def test_include_requires_at_least_one_match():
    rule = FilterRule(include_keywords=("Python", "Rust"))
    assert rule.matches("Learning Python is fun") is True
    assert rule.matches("Rust is memory safe") is True
    assert rule.matches("JavaScript is popular") is False


def test_include_is_case_insensitive():
    rule = FilterRule(include_keywords=("python",))
    assert rule.matches("PYTHON is great") is True
    assert rule.matches("Python is great") is True


def test_exclude_drops_entries_containing_any():
    rule = FilterRule(exclude_keywords=("sponsored", "ad"))
    assert rule.matches("Real article") is True
    assert rule.matches("This post is sponsored") is False
    assert rule.matches("An ad for something") is False


def test_include_and_exclude_both_apply():
    rule = FilterRule(
        include_keywords=("Python",),
        exclude_keywords=("job",),
    )
    assert rule.matches("Python tutorial") is True
    assert rule.matches("Python job opening") is False  # excluded wins
    assert rule.matches("Rust tutorial") is False  # doesn't pass include


# ===== word-boundary semantics (ASCII) vs substring (CJK / symbols) =====


def test_ascii_keyword_matches_whole_words_only():
    """`ai` must not fire on "brain"/"said" — the audit's canonical trap."""
    rule = FilterRule(include_keywords=("ai",))
    assert rule.matches("New AI model released") is True
    assert rule.matches("The brain said nothing") is False


def test_ascii_keyword_no_longer_matches_inside_longer_words():
    rule = FilterRule(include_keywords=("bench",))
    assert rule.matches("A new park bench design") is True
    assert rule.matches("Performance benchmark results") is False


def test_ascii_phrase_keyword_matches_on_word_boundaries():
    rule = FilterRule(include_keywords=("machine learning",))
    assert rule.matches("Advances in machine learning today") is True
    assert rule.matches("machine learnings") is False


def test_cjk_keyword_keeps_substring_semantics():
    rule = FilterRule(include_keywords=("人工智能",))
    assert rule.matches("最新人工智能模型发布") is True


def test_ascii_keyword_matches_flush_against_cjk():
    """Boundaries are ASCII-only: unspaced CJK context ("AI芯片") must
    still hit, while an ASCII-embedded use ("OpenAI芯片") must not."""
    rule = FilterRule(include_keywords=("ai",))
    assert rule.matches("AI芯片竞赛升级") is True
    assert rule.matches("OpenAI芯片订单") is False


def test_symbol_keyword_keeps_substring_semantics():
    # "c++" isn't word-ish (trailing +), so it stays a plain substring.
    rule = FilterRule(include_keywords=("c++",))
    assert rule.matches("Modern C++ features") is True


# ===== regex rules =====


def test_include_regex_must_match():
    rule = FilterRule(include_regex=r"\bGPT-\d+")
    assert rule.matches("OpenAI ships GPT-6") is True
    assert rule.matches("OpenAI ships a new model") is False


def test_exclude_regex_drops_matches():
    rule = FilterRule(exclude_regex=r"rumou?r")
    assert rule.matches("Confirmed: launch date") is True
    assert rule.matches("Rumor: new device") is False
    assert rule.matches("Rumour: new device") is False


def test_invalid_stored_regex_fails_open():
    # A pattern that reaches storage invalid must not block dispatch.
    rule = FilterRule(include_regex="([unclosed")
    assert rule.matches("anything") is True


def test_regex_roundtrips_through_json():
    original = FilterRule(include_regex=r"\bGPT-\d+", exclude_keywords=("sponsored",))
    recovered = FilterRule.from_json(original.to_json())
    assert recovered == original


# ===== parse_filter_field =====


def test_parse_filter_field_keywords_vs_regex():
    assert parse_filter_field("a, b") == (("a", "b"), None)
    assert parse_filter_field("/GPT-\\d+|Claude/") == ((), "GPT-\\d+|Claude")
    assert parse_filter_field("") == ((), None)
    assert parse_filter_field(None) == ((), None)


def test_parse_filter_field_regex_may_contain_commas():
    # The whole-field form exists exactly so {2,3} never fights the CSV split.
    assert parse_filter_field("/a{2,3}/") == ((), "a{2,3}")


def test_parse_filter_field_rejects_bad_regex():
    with pytest.raises(ValueError):
        parse_filter_field("/([unclosed/")
    with pytest.raises(ValueError):
        parse_filter_field("//")
    with pytest.raises(ValueError):
        parse_filter_field("/" + "a" * 300 + "/")


@pytest.mark.parametrize(
    "payload,expected_include,expected_exclude",
    [
        (None, (), ()),
        ({}, (), ()),
        ({"include_keywords": ["a", "b"]}, ("a", "b"), ()),
        ({"exclude_keywords": ["x"]}, (), ("x",)),
        (
            {"include_keywords": ["a"], "exclude_keywords": ["b"]},
            ("a",),
            ("b",),
        ),
    ],
)
def test_from_json_roundtrip(payload, expected_include, expected_exclude):
    rule = FilterRule.from_json(payload)
    assert rule.include_keywords == expected_include
    assert rule.exclude_keywords == expected_exclude


def test_to_json_returns_none_for_empty_rule():
    assert FilterRule().to_json() is None


def test_to_json_round_trip_preserves_keywords():
    original = FilterRule(
        include_keywords=("Python", "Rust"),
        exclude_keywords=("sponsored",),
    )
    data = original.to_json()
    recovered = FilterRule.from_json(data)
    assert recovered == original


def test_parse_keyword_csv_trims_and_drops_empties():
    assert parse_keyword_csv("a, b ,c") == ("a", "b", "c")
    assert parse_keyword_csv(" , a , ") == ("a",)
    assert parse_keyword_csv("") == ()
    assert parse_keyword_csv(None) == ()
    assert parse_keyword_csv("single") == ("single",)
