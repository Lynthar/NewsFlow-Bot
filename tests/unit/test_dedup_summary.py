"""Tests for content_processor.dedup_summary.

Google News RSS and some headline-only feeds repeat the title as the
description verbatim (or with a trivial source-attribution tail). The
dedup heuristic drops those so the adapter displays only the title
instead of showing "Title / Title" to the user.
"""

from newsflow.core.content_processor import dedup_summary


def test_exact_match_dropped():
    assert dedup_summary("Fed cuts rates", "Fed cuts rates") == ""


def test_google_news_source_suffix_dropped():
    # Canonical Google News pattern: title + double-space + source name
    # after HTML strip.
    assert dedup_summary("Fed cuts rates", "Fed cuts rates  Reuters") == ""


def test_title_with_dash_source_suffix_dropped():
    assert (
        dedup_summary("Fed cuts rates", "Fed cuts rates - Reuters")
        == ""
    )


def test_title_with_ellipsis_dropped():
    assert dedup_summary("Fed cuts rates", "Fed cuts rates…") == ""


def test_real_summary_kept():
    summary = (
        "Fed cuts rates by 25 basis points after months of debate, "
        "citing weakening job market."
    )
    assert dedup_summary("Fed cuts rates", summary) == summary


def test_case_insensitive():
    assert dedup_summary("Fed Cuts Rates", "FED CUTS RATES") == ""


def test_whitespace_normalized():
    # Extra whitespace / tabs / newlines don't fool the check.
    assert (
        dedup_summary(
            "Fed cuts rates", "  Fed    cuts\n\nrates  "
        )
        == ""
    )


def test_empty_summary_returned_as_is():
    assert dedup_summary("Title", "") == ""


def test_empty_title_keeps_summary():
    assert dedup_summary("", "Some summary") == "Some summary"


def test_both_empty():
    assert dedup_summary("", "") == ""


def test_summary_that_extends_title_kept():
    # Borderline: starts with title, but has real content after.
    # Heuristic threshold = 30 chars of remainder; this has more.
    summary = "Fed cuts rates and markets respond with a historic rally today"
    assert dedup_summary("Fed cuts rates", summary) == summary


def test_summary_with_short_but_real_addition_on_threshold():
    # "and markets surge" is <30 chars of real content. Acceptable collateral
    # damage — the heuristic treats it as a source suffix. Documents the
    # tradeoff.
    summary = "Fed cuts rates and markets surge"
    remainder = summary[len("Fed cuts rates"):].strip(" -—…|·,.")
    assert len(remainder) < 30  # sanity: the remainder is short
    assert dedup_summary("Fed cuts rates", summary) == ""


def test_title_case_punctuation_summary_match():
    # Title has trailing punctuation, summary doesn't (or vice versa).
    # Current implementation treats them as different because
    # normalization doesn't strip punctuation, only case+whitespace.
    # Document this as a limitation if someone edits the heuristic.
    assert (
        dedup_summary("Fed cuts rates.", "Fed cuts rates")
        == "Fed cuts rates"
    )


def test_unrelated_summary_kept():
    assert (
        dedup_summary(
            "Fed cuts rates",
            "Markets rose sharply as investors priced in further easing",
        )
        == "Markets rose sharply as investors priced in further easing"
    )
