"""core/message_template semantics: substitution, escapes, line collapse.

These pin the template contract users write against: single-brace
placeholders, effective-vs-original value split, \\n normalization,
{{ }} literal braces, set-time validation vs render-time fail-open,
and the empty-line collapse rule.
"""

from datetime import UTC, datetime

from newsflow.adapters.base import Message
from newsflow.core.message_template import (
    PLACEHOLDER_LIST,
    PLACEHOLDERS,
    TEMPLATE_MAX_LENGTH,
    normalize_template,
    render_template,
    validate_template,
)


def _values(**overrides: str) -> dict[str, str]:
    base = dict.fromkeys(PLACEHOLDERS, "")
    base.update(
        title="Hello World",
        summary="A summary.",
        url="https://ex.io/a?x=1&y=2",
        link="https://ex.io/a?x=1&y=2",
        source="Example Feed",
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------- rendering


def test_basic_substitution() -> None:
    out = render_template("📌 {title}\n{summary}\n🔗 {url}", _values())
    assert out == "📌 Hello World\nA summary.\n🔗 https://ex.io/a?x=1&y=2"


def test_unknown_placeholder_passes_through_at_render() -> None:
    out = render_template("{title} {tittle} {not_a_thing}", _values())
    assert out == "Hello World {tittle} {not_a_thing}"


def test_double_brace_renders_literal_braces() -> None:
    out = render_template("{{title}} is {title}", _values())
    assert out == "{title} is Hello World"


def test_line_with_only_empty_placeholders_is_dropped() -> None:
    out = render_template("{title}\n🖼 {image_url}\n🔗 {url}", _values(image_url=""))
    assert out == "Hello World\n🔗 https://ex.io/a?x=1&y=2"


def test_line_with_partially_empty_placeholders_is_kept() -> None:
    out = render_template("{source} · {published}", _values(published=""))
    assert out.startswith("Example Feed")


def test_line_with_only_unknown_placeholder_is_kept() -> None:
    # Unknown tokens don't count as "resolved empty" — the line survives.
    out = render_template("{mystery}", _values())
    assert out == "{mystery}"


def test_static_lines_survive_and_blank_runs_collapse() -> None:
    out = render_template("{title}\n\n\n\n---\n{summary}", _values())
    assert out == "Hello World\n\n---\nA summary."


def test_result_is_trimmed() -> None:
    out = render_template("\n\n{title}\n\n", _values())
    assert out == "Hello World"


def test_dropped_lines_do_not_leave_holes() -> None:
    out = render_template("{title}\n📝 {summary}\n🔗 {url}", _values(summary=""))
    assert "📝" not in out
    assert out == "Hello World\n🔗 https://ex.io/a?x=1&y=2"


# ------------------------------------------------------------ normalization


def test_normalize_converts_backslash_n_and_strips() -> None:
    assert normalize_template("  a\\nb  ") == "a\nb"


def test_normalize_keeps_real_newlines() -> None:
    assert normalize_template("a\nb") == "a\nb"


# --------------------------------------------------------------- validation


def test_validate_accepts_all_canonical_placeholders() -> None:
    template = " ".join("{" + name + "}" for name in sorted(PLACEHOLDERS))
    assert validate_template(template) == []


def test_validate_rejects_unknown_word_shaped_tokens() -> None:
    errors = validate_template("{title} {tittle}")
    assert len(errors) == 1
    assert "{tittle}" in errors[0]
    assert PLACEHOLDER_LIST in errors[0]


def test_validate_ignores_literal_braces_and_non_word_tokens() -> None:
    assert validate_template("{{anything}} {'-'} {123}") == []


def test_validate_rejects_overlong_template() -> None:
    errors = validate_template("x" * (TEMPLATE_MAX_LENGTH + 1))
    assert len(errors) == 1
    assert "too long" in errors[0]


# ------------------------------------------------- Message value contract


def _message(**overrides: object) -> Message:
    kwargs: dict = {
        "title": "Original",
        "summary": "Orig sum",
        "link": "https://ex.io/x",
        "source": "Feed",
        "published_at": datetime(2026, 7, 18, 12, 30, tzinfo=UTC),
        "image_url": "https://ex.io/i.png",
        "title_translated": "译标题",
        "summary_translated": "译摘要",
    }
    kwargs.update(overrides)
    return Message(**kwargs)


def test_message_values_keys_match_placeholder_set() -> None:
    assert set(_message().to_template_values().keys()) == set(PLACEHOLDERS)


def test_effective_values_prefer_translation() -> None:
    values = _message().to_template_values()
    assert values["title"] == "译标题"
    assert values["summary"] == "译摘要"
    assert values["original_title"] == "Original"
    assert values["translated_title"] == "译标题"


def test_effective_values_fall_back_to_original() -> None:
    values = _message(title_translated=None, summary_translated=None).to_template_values()
    assert values["title"] == "Original"
    assert values["summary"] == "Orig sum"
    assert values["translated_title"] == ""
    assert values["translated_summary"] == ""


def test_url_and_link_are_aliases_and_published_formats() -> None:
    values = _message().to_template_values()
    assert values["url"] == values["link"] == "https://ex.io/x"
    assert values["published"] == "2026-07-18 12:30"


def test_missing_optionals_resolve_empty() -> None:
    values = _message(published_at=None, image_url=None).to_template_values()
    assert values["published"] == ""
    assert values["image_url"] == ""
