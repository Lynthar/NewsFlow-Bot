"""
Translation services.

Provides abstraction for multiple translation providers.
"""

from newsflow.services.translation.base import (
    TranslationProvider,
    TranslationResult,
    TranslationService,
)

__all__ = [
    "TranslationProvider",
    "TranslationResult",
    "TranslationService",
]
