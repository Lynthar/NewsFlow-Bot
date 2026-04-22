"""LLM-based digest generation.

Pattern mirrors services/translation/: an abstract provider + a factory that
picks one based on settings. Currently only OpenAI (compatible API) is
supported, but the shape makes it easy to add Anthropic / Google / etc. later.
"""

from newsflow.services.summarization.base import (
    DigestArticle,
    DigestResult,
    SummarizationProvider,
)
from newsflow.services.summarization.factory import get_summarizer

__all__ = [
    "DigestArticle",
    "DigestResult",
    "SummarizationProvider",
    "get_summarizer",
]
