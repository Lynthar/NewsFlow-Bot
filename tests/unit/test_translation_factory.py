"""Tests for create_translation_service wiring.

The setting `translation_cache_ttl_days` exists for the user to tune
how long translations stay in cache, but the factory used to build
TranslationService without forwarding it — leaving the documented
knob with no effect. Pin that down with a regression test.
"""

from unittest.mock import MagicMock, patch

from newsflow.services.translation import factory as factory_mod


class _DummyProvider:
    name = "dummy"


def test_create_translation_service_uses_configured_ttl_days():
    settings = MagicMock()
    settings.translation_cache_ttl_days = 3  # not the default 7

    with (
        patch.object(
            factory_mod, "create_translation_provider", return_value=_DummyProvider()
        ),
        patch.object(factory_mod, "get_settings", return_value=settings),
        patch.object(factory_mod, "get_cache", return_value=None),
    ):
        service = factory_mod.create_translation_service()

    assert service is not None
    assert service.cache_ttl == 3 * 86400


def test_create_translation_service_default_ttl_matches_default_setting():
    settings = MagicMock()
    settings.translation_cache_ttl_days = 7  # default in config.py

    with (
        patch.object(
            factory_mod, "create_translation_provider", return_value=_DummyProvider()
        ),
        patch.object(factory_mod, "get_settings", return_value=settings),
        patch.object(factory_mod, "get_cache", return_value=None),
    ):
        service = factory_mod.create_translation_service()

    assert service is not None
    assert service.cache_ttl == 7 * 86400
