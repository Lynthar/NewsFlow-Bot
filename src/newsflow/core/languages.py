"""Language-code shape validation for user-facing commands.

Translation providers each accept slightly different code sets, so this
is deliberately shape-only: well-formed unknown codes pass through (they
fail loudly at translate time with the provider's own error), while
obvious non-codes ("chinese", "中文") are rejected at input instead of
being stored and then silently failing on every entry.
"""

import re

_LANG_RE = re.compile(r"^([A-Za-z]{2,3})(?:[-_]([A-Za-z0-9]{2,8}))?$")

# Language-code columns are String(10) across the schema (Subscription,
# FeedEntry, ChannelDigest, ChannelSettings); longer well-formed codes
# ("ca-valencia") must be rejected here, not by a Postgres insert error.
_MAX_CODE_LENGTH = 10

# Shown in command error messages.
LANGUAGE_CODE_EXAMPLES = "zh-CN, zh-TW, en, ja, ko, de, fr, ru"


def normalize_language_code(value: str) -> str | None:
    """Validate a BCP-47-ish code's shape; return it normalized, or None.

    "zh-cn" → "zh-CN", "EN" → "en", "zh_hans" → "zh-Hans",
    "chinese" → None.
    """
    stripped = value.strip()
    m = _LANG_RE.match(stripped)
    if not m or len(stripped) > _MAX_CODE_LENGTH:
        return None
    primary = m.group(1).lower()
    sub = m.group(2)
    if sub is None:
        return primary
    if len(sub) == 2 and sub.isalpha():
        sub = sub.upper()  # region: zh-CN
    elif len(sub) == 4 and sub.isalpha():
        sub = sub.capitalize()  # script: zh-Hans
    return f"{primary}-{sub}"


def same_primary_language(a: str | None, b: str | None) -> bool:
    """Whether two language codes share the primary subtag (zh-CN ~ ZH)."""
    if not a or not b:
        return False
    return a.replace("_", "-").split("-")[0].lower() == b.replace("_", "-").split("-")[0].lower()


# --- script-based same-language detection (translation short-circuit) -------

_HAN_RE = re.compile(r"[一-鿿]")
_KANA_RE = re.compile(r"[぀-ヿ]")
_HANGUL_RE = re.compile(r"[가-힯]")

# High-frequency characters that exist in exactly one Chinese variant.
# Used to keep the zh short-circuit away from simplified↔traditional
# conversion scenarios (a zh-TW target with a simplified-Chinese source
# still needs the translator).
_SIMPLIFIED_ONLY = set("们这国说时会对经现发么样还没让见业动车长门问间")
_TRADITIONAL_ONLY = set("們這國說時會對經現發麼樣還沒讓見業動車長門問間")

_TRADITIONAL_TARGETS = ("zh-tw", "zh-hk", "zh-mo", "zh-hant")


def _zh_variant_mismatch(text: str, target: str) -> bool:
    """True when `text`'s Chinese variant differs from the target's."""
    chars = set(text)
    if target.lower() in _TRADITIONAL_TARGETS:
        return bool(chars & _SIMPLIFIED_ONLY)
    return bool(chars & _TRADITIONAL_ONLY)


def text_clearly_in_language(text: str, lang_code: str) -> bool:
    """Conservative script check: True only when `text`'s script uniquely
    identifies it as `lang_code`, so translating would be a no-op.

    A false True suppresses a needed translation, so everything errs
    toward False: Latin-script languages are never claimed (en/de/fr
    share a script), zh is never claimed across a simplified↔traditional
    boundary, and short texts are never claimed at all.
    """
    primary = lang_code.replace("_", "-").split("-")[0].lower()
    if primary not in ("zh", "ja", "ko"):
        return False
    sample = text[:400]
    letters = [ch for ch in sample if ch.isalpha()]
    if len(letters) < 8:
        return False
    total = len(letters)
    han = sum(1 for ch in letters if _HAN_RE.match(ch))
    kana = sum(1 for ch in letters if _KANA_RE.match(ch))
    hangul = sum(1 for ch in letters if _HANGUL_RE.match(ch))
    if primary == "zh":
        return han / total >= 0.5 and kana == 0 and not _zh_variant_mismatch(sample, lang_code)
    if primary == "ja":
        # Real Japanese prose always carries kana; han-only text is
        # more likely Chinese, so require both signals.
        return kana / total >= 0.05 and (han + kana) / total >= 0.5
    return hangul / total >= 0.5  # ko
