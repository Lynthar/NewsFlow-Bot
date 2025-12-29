"""
Content processing utilities.

Handles:
- HTML cleaning and text extraction
- Image URL extraction
- Text truncation for message limits
- Source name mapping
"""

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from bs4 import BeautifulSoup

# Maximum lengths for Discord/Telegram
MAX_TITLE_LENGTH = 256
MAX_SUMMARY_LENGTH = 1024
MAX_EMBED_DESCRIPTION = 4096

# Domain to source name mapping
DOMAIN_TO_SOURCE = {
    # English names
    "cnn.com": {"en": "CNN", "zh": "有线电视新闻网"},
    "bbc.com": {"en": "BBC", "zh": "英国广播公司"},
    "bbc.co.uk": {"en": "BBC", "zh": "英国广播公司"},
    "wsj.com": {"en": "Wall Street Journal", "zh": "华尔街日报"},
    "foreignaffairs.com": {"en": "Foreign Affairs", "zh": "外交事务"},
    "ft.com": {"en": "Financial Times", "zh": "金融时报"},
    "reuters.com": {"en": "Reuters", "zh": "路透社"},
    "theatlantic.com": {"en": "The Atlantic", "zh": "大西洋月刊"},
    "economist.com": {"en": "The Economist", "zh": "经济学人"},
    "nytimes.com": {"en": "The New York Times", "zh": "纽约时报"},
    "bloomberg.com": {"en": "Bloomberg", "zh": "彭博社"},
    "theconversation.com": {"en": "The Conversation", "zh": "对话"},
    "nautil.us": {"en": "Nautilus", "zh": "鹦鹉螺"},
    "longreads.com": {"en": "Longreads", "zh": "长读"},
    "nature.com": {"en": "Nature", "zh": "《自然》"},
    "science.org": {"en": "Science", "zh": "《科学》"},
    "eff.org": {"en": "EFF", "zh": "电子前哨基金会"},
    "ieee.org": {"en": "IEEE", "zh": "电气和电子工程师协会"},
    "brookings.edu": {"en": "Brookings", "zh": "布鲁金斯学会"},
    "theguardian.com": {"en": "The Guardian", "zh": "卫报"},
    "washingtonpost.com": {"en": "Washington Post", "zh": "华盛顿邮报"},
    "apnews.com": {"en": "AP News", "zh": "美联社"},
    "npr.org": {"en": "NPR", "zh": "美国公共广播"},
    "wired.com": {"en": "Wired", "zh": "连线"},
    "arstechnica.com": {"en": "Ars Technica", "zh": "Ars Technica"},
    "techcrunch.com": {"en": "TechCrunch", "zh": "TechCrunch"},
    "theverge.com": {"en": "The Verge", "zh": "The Verge"},
    "hackernews.com": {"en": "Hacker News", "zh": "Hacker News"},
}


@dataclass
class ProcessedContent:
    """Processed content ready for display."""

    title: str
    summary: str
    plain_text: str
    images: list[str]
    source_name: str


def clean_html(html: str) -> tuple[str, list[str]]:
    """
    Clean HTML and extract text and images.

    Args:
        html: Raw HTML content

    Returns:
        Tuple of (clean_text, image_urls)
    """
    if not html or not html.strip():
        return "", []

    # Check if it's actually HTML
    if not html.strip().startswith("<") and "<" not in html:
        return html.strip(), []

    soup = BeautifulSoup(html, "lxml")

    # Remove script and style elements
    for element in soup(["script", "style", "noscript"]):
        element.decompose()

    # Extract images before getting text
    images = []
    for img in soup.find_all("img"):
        src = img.get("src")
        if src and src.startswith(("http://", "https://")):
            images.append(src)

    # Get text
    text = soup.get_text(separator=" ", strip=True)

    # Clean up whitespace
    text = re.sub(r"\s+", " ", text).strip()

    return text, images


def truncate_text(text: str, max_length: int, suffix: str = "...") -> str:
    """
    Truncate text to max_length, trying to break at word boundary.

    Args:
        text: Text to truncate
        max_length: Maximum length
        suffix: Suffix to add when truncated

    Returns:
        Truncated text
    """
    if len(text) <= max_length:
        return text

    # Account for suffix length
    truncate_at = max_length - len(suffix)
    if truncate_at <= 0:
        return suffix[:max_length]

    truncated = text[:truncate_at]

    # Try to break at last space
    last_space = truncated.rfind(" ")
    if last_space > truncate_at * 0.7:  # Only if we don't lose too much
        truncated = truncated[:last_space]

    return truncated.rstrip() + suffix


def get_source_name(url: str, language: str = "en") -> str:
    """
    Get human-readable source name from URL.

    Args:
        url: The article URL
        language: Target language ('en' or 'zh')

    Returns:
        Source name in the requested language
    """
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        # Remove www prefix
        if domain.startswith("www."):
            domain = domain[4:]

        # Check mapping
        if domain in DOMAIN_TO_SOURCE:
            lang_key = language if language in ("en", "zh") else "en"
            return DOMAIN_TO_SOURCE[domain].get(lang_key, domain)

        # Check if subdomain of known domain
        for known_domain, names in DOMAIN_TO_SOURCE.items():
            if domain.endswith("." + known_domain):
                lang_key = language if language in ("en", "zh") else "en"
                return names.get(lang_key, domain)

        # Return domain without TLD as fallback
        parts = domain.split(".")
        if len(parts) >= 2:
            return parts[-2].title()

        return domain

    except Exception:
        return "Unknown"


def process_content(
    title: str,
    summary: str | None,
    content: str | None,
    link: str,
    language: str = "en",
) -> ProcessedContent:
    """
    Process raw content into display-ready format.

    Args:
        title: Article title
        summary: Article summary (may contain HTML)
        content: Full content (may contain HTML)
        link: Article URL
        language: Target language for source name

    Returns:
        ProcessedContent with cleaned and truncated text
    """
    # Clean title
    clean_title = clean_html(title)[0] if "<" in title else title
    clean_title = truncate_text(clean_title, MAX_TITLE_LENGTH)

    # Process summary/content
    raw_text = content or summary or ""
    plain_text, images = clean_html(raw_text)

    # Truncate for display
    display_summary = truncate_text(plain_text, MAX_SUMMARY_LENGTH)

    # Get source name
    source_name = get_source_name(link, language)

    return ProcessedContent(
        title=clean_title,
        summary=display_summary,
        plain_text=plain_text,
        images=images,
        source_name=source_name,
    )


def extract_first_image(html: str) -> str | None:
    """
    Extract first image URL from HTML content.

    Args:
        html: HTML content

    Returns:
        First image URL or None
    """
    _, images = clean_html(html)
    return images[0] if images else None


def is_valid_image_url(url: str) -> bool:
    """
    Check if URL looks like a valid image URL.

    Args:
        url: URL to check

    Returns:
        True if appears to be valid image URL
    """
    if not url or not url.startswith(("http://", "https://")):
        return False

    # Check extension
    image_extensions = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg")
    parsed = urlparse(url)
    path_lower = parsed.path.lower()

    if any(path_lower.endswith(ext) for ext in image_extensions):
        return True

    # Check for common image hosting patterns
    image_hosts = ("imgur.com", "i.imgur.com", "pbs.twimg.com", "media.")
    if any(host in parsed.netloc.lower() for host in image_hosts):
        return True

    return False
