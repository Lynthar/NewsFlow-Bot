"""Per-subscription message templates: {placeholder} substitution.

Markdown-ish text with single-brace placeholders. ``{title}`` / ``{summary}`` are the
*effective* values (translated when available); ``original_*`` / ``translated_*`` are
always literal. ``{{`` / ``}}`` emit literal braces and a literal ``\n`` is normalized
to a newline at storage time.

Unknown placeholders are rejected by ``validate_template`` at set time but pass through
verbatim at render time. A line whose known placeholders ALL resolve empty is dropped;
partially-resolved lines stay. Runs of 3+ newlines collapse and the result is trimmed.

Stateless: callers pass a plain name->value mapping (``Message.to_template_values``).
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

    Checks length, unknown word-shaped placeholders, and that some placeholder is
    used, since without one every entry renders the same text. Run on the normalized
    form at set time; render never validates (fail-open for stored rows).
    """
    errors: list[str] = []
    if len(template) > TEMPLATE_MAX_LENGTH:
        errors.append(f"template is too long ({len(template)} > {TEMPLATE_MAX_LENGTH} characters)")
    # {{...}} is a literal-brace escape, not a placeholder.
    body = template.replace("{{", "").replace("}}", "")
    names = {m.group(1) for m in _TOKEN_RE.finditer(body)}
    unknown = sorted(names - PLACEHOLDERS)
    if unknown:
        listed = ", ".join("{" + name + "}" for name in unknown)
        errors.append(f"unknown placeholder(s): {listed}. Valid placeholders: {PLACEHOLDER_LIST}")
    elif not names:
        errors.append(
            "template uses no placeholder, so every entry would read the same. "
            f"Valid placeholders: {PLACEHOLDER_LIST}"
        )
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
