"""
Service layer for business logic.
"""

from newsflow.services.cache import (
    CacheBackend,
    MemoryCache,
    RedisCache,
    get_cache,
    init_cache,
)
from newsflow.services.dispatcher import Dispatcher, DispatchResult, get_dispatcher
from newsflow.services.feed_service import AddFeedResult, FeedService, FetchFeedResult
from newsflow.services.subscription_service import (
    SubscribeResult,
    SubscriptionService,
    UnsubscribeResult,
)
from newsflow.services.translation import (
    TranslationProvider,
    TranslationResult,
    TranslationService,
)
from newsflow.services.translation.factory import (
    create_translation_service,
    get_translation_service,
)

__all__ = [
    # Feed service
    "FeedService",
    "AddFeedResult",
    "FetchFeedResult",
    # Subscription service
    "SubscriptionService",
    "SubscribeResult",
    "UnsubscribeResult",
    # Dispatcher
    "Dispatcher",
    "DispatchResult",
    "get_dispatcher",
    # Cache
    "CacheBackend",
    "MemoryCache",
    "RedisCache",
    "get_cache",
    "init_cache",
    # Translation
    "TranslationProvider",
    "TranslationResult",
    "TranslationService",
    "create_translation_service",
    "get_translation_service",
]
