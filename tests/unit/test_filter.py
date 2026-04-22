"""Tests for the FilterRule rule engine."""

import pytest

from newsflow.core.filter import FilterRule, parse_keyword_csv


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


def test_include_substring_match():
    """Matches should find the keyword anywhere in the text."""
    rule = FilterRule(include_keywords=("bench",))
    assert rule.matches("Performance benchmark results") is True


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
