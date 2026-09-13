"""The allowed-key sets of both YAML schemas are the config dataclasses' fields,
and the shipped samples document every one of them: a field with no sample line,
or a sample key with no field to land in, fails here before an operator hits it."""

import re
from pathlib import Path

import pytest

from newsflow.services import source_sync, webhook_sync
from newsflow.services._yamlcfg import yaml_keys

SAMPLES = Path(__file__).parents[2] / "samples"
SOURCES_SAMPLE = SAMPLES / "sources.example.yaml"
WEBHOOKS_SAMPLE = SAMPLES / "webhooks.example.yaml"

# (allowed-key table, dataclass, keys the dataclass carries that are not block
# keys — `name` is the mapping key `sources: {name: {...}}`, sample it documents)
_SCHEMAS = [
    (source_sync._SOURCE_KEYS, source_sync.SourceCfg, {"name"}, SOURCES_SAMPLE),
    (source_sync._SUBSCRIBER_KEYS, source_sync.SubscriberCfg, set(), SOURCES_SAMPLE),
    (webhook_sync._TOP_LEVEL_KEYS, webhook_sync.WebhookConfig, set(), WEBHOOKS_SAMPLE),
    (
        webhook_sync._DESTINATION_KEYS,
        webhook_sync.WebhookConfigDestination,
        {"name"},
        WEBHOOKS_SAMPLE,
    ),
]


def _keys_mentioned(sample: Path) -> set[str]:
    """Every `key:` opening a line or a `- ` list item, commented-out ones
    included — the samples show optional keys as `# key: value`."""
    text = sample.read_text(encoding="utf-8")
    return set(re.findall(r"^\s*#?\s*(?:-\s+)?([a-z_]+):", text, re.M))


@pytest.mark.parametrize("allowed, cls, not_block_keys, sample", _SCHEMAS)
def test_allowed_keys_are_the_dataclass_fields(allowed, cls, not_block_keys, sample):
    assert allowed == yaml_keys(cls) - not_block_keys


@pytest.mark.parametrize("allowed, cls, not_block_keys, sample", _SCHEMAS)
def test_every_allowed_key_is_shown_in_the_sample(allowed, cls, not_block_keys, sample):
    assert allowed <= _keys_mentioned(sample)


def test_samples_parse_under_the_strict_schemas():
    # Unknown keys are errors, so a sample that documents a key the schema
    # lacks fails right here rather than on the operator's first boot.
    assert len(source_sync.parse_sources_yaml(SOURCES_SAMPLE)) == 3
    cfg = webhook_sync.parse_webhooks_yaml(WEBHOOKS_SAMPLE)
    assert len(cfg.destinations) == 7
