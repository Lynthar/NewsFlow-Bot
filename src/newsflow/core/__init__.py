"""
Core modules for NewsFlow Bot.
"""

from newsflow.core.content_processor import (
    ProcessedContent,
    clean_html,
    get_source_name,
    process_content,
    truncate_text,
)
from newsflow.core.feed_fetcher import (
    FeedFetcher,
    FetchResult,
    close_fetcher,
    get_fetcher,
)

__all__ = [
    # Feed fetcher
    "FeedFetcher",
    "FetchResult",
    "get_fetcher",
    "close_fetcher",
    # Content processor
    "ProcessedContent",
    "clean_html",
    "get_source_name",
    "process_content",
    "truncate_text",
]
