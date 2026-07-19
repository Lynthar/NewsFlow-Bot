"""Per-subscription message templates: {placeholder} substitution.

A template is Markdown-ish text (``**bold**``, ``[text](url)``) with
single-brace placeholders. Discord renders the result natively as
message content; Telegram converts it through
``core.telegram_markdown.markdown_to_telegram_html``.

Semantics (all pinned by tests):

- ``{title}`` / ``{summary}`` are the *effective* values — translated
  when a translation exists, original otherwise. The ``original_*`` /
  ``translated_*`` variants are always literal, so a bilingual layout is
  ``{translated_title}`` + ``{original_title}``.
- Literal ``\\n`` (as typed in Discord's single-line option box) is
  normalized to a real newline before storage.
- ``{{`` and ``}}`` produce literal braces.
- Unknown word-shaped placeholders are rejected at *set* time
  (`validate_template`) but pass through verbatim at *render* time —
  a stored template never breaks delivery (same fail-open philosophy
  as stored filter regexes).
- A line that contains at least one known placeholder where ALL of them
  resolved empty is dropped entirely — ``🔗 {url}`` never renders as a
  dangling ``🔗``. Lines whose placeholders partially resolve are kept.
- Runs of 3+ newlines collapse to a blank line; the result is trimmed.

This module is a stateless primitive: it knows nothing about Message or
the ORM. Callers pass a plain name→value mapping (see
``Message.to_template_values`` in adapters/base.py, pinned to
PLACEHOLDERS by test).
"""

import re
from collections.abc import Mapping

# Curated order for help/error text: everyday names first, bilingual
# variants last.
_CANONICAL_ORDER = (
    "title",
    "summary",
    "url",
    "link",
    "source",
    "published",
    "image_url",
    "mention",
    "original_title",
    "translated_title",
    "original_summary",
    "translated_summary",
)

PLACEHOLDERS: frozenset[str] = frozenset(_CANONICAL_ORDER)

#: Human-readable list for command usage/help text.
PLACEHOLDER_LIST: str = ", ".join("{" + name + "}" for name in _CANONICAL_ORDER)

TEMPLATE_MAX_LENGTH = 1000

_TOKEN_RE = re.compile(r"\{([a-z_]+)\}")

# Sentinels for the {{ }} literal-brace escape. Control characters can't
# be typed in a chat message, so they can't collide with template text.
_LBRACE = "\x00"
_RBRACE = "\x01"


def normalize_template(raw: str) -> str:
    """Canonicalize a template as typed by the user for storage.

    Converts literal ``\\n`` two-character sequences to real newlines
    (Discord's slash-command option box is single-line, so ``\\n`` is
    the only way to express a line break there) and trims surrounding
    whitespace.
    """
    return raw.replace("\\n", "\n").strip()


def validate_template(template: str) -> list[str]:
    """Return user-facing problems with a template; empty list = valid.

    Checks length and unknown word-shaped placeholders. Run on the
    normalized form at set time; render never validates (fail-open for
    stored rows).
    """
    errors: list[str] = []
    if len(template) > TEMPLATE_MAX_LENGTH:
        errors.append(f"template is too long ({len(template)} > {TEMPLATE_MAX_LENGTH} characters)")
    # {{...}} is a literal-brace escape, not a placeholder.
    body = template.replace("{{", "").replace("}}", "")
    unknown = sorted({m.group(1) for m in _TOKEN_RE.finditer(body)} - PLACEHOLDERS)
    if unknown:
        listed = ", ".join("{" + name + "}" for name in unknown)
        errors.append(f"unknown placeholder(s): {listed}. Valid placeholders: {PLACEHOLDER_LIST}")
    return errors


def render_template(template: str, values: Mapping[str, str]) -> str:
    """Substitute placeholders and apply line-level cleanup.

    ``values`` maps every canonical placeholder name to its (possibly
    empty) string value; names absent from the mapping are treated as
    unknown and left verbatim.
    """
    text = template.replace("{{", _LBRACE).replace("}}", _RBRACE)

    out_lines: list[str] = []
    for line in text.split("\n"):
        known = 0
        nonempty = 0

        def _sub(match: re.Match[str]) -> str:
            nonlocal known, nonempty
            name = match.group(1)
            if name not in values:
                return match.group(0)
            known += 1
            value = values[name]
            if value:
                nonempty += 1
            return value

        rendered = _TOKEN_RE.sub(_sub, line)
        if known and not nonempty:
            # The line existed to carry values that all turned out empty
            # (hidden summary, feed without images, …) — drop it whole,
            # decorations included.
            continue
        out_lines.append(rendered.rstrip())

    result = "\n".join(out_lines)
    result = re.sub(r"\n{3,}", "\n\n", result)
    result = result.strip("\n").strip()
    return result.replace(_LBRACE, "{").replace(_RBRACE, "}")
