"""Tests for create_translation_service wiring.

The setting `translation_cache_ttl_days` exists for the user to tune
how long translations stay in cache, but the factory used to build
TranslationService without forwarding it — leaving the documented
knob with no effect. Pin that down with a regression test.
"""

from newsflow.services.translation import factory as factory_mod


def _deepl_configured(configure, **fields):
    # DeepL only stores the key at construction; nothing is contacted.
    configure(translation_enabled=True, translation_provider="deepl", deepl_api_key="k", **fields)


def test_create_translation_service_uses_configured_ttl_days(configure):
    _deepl_configured(configure, translation_cache_ttl_days=3)  # not the default 7

    service = factory_mod.create_translation_service()

    assert service is not None
    assert service.cache_ttl == 3 * 86400


def test_create_translation_service_default_ttl_matches_default_setting(configure):
    _deepl_configured(configure)

    service = factory_mod.create_translation_service()

    assert service is not None
    assert service.cache_ttl == 7 * 86400
