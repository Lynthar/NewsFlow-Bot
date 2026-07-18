"""checkconfig CLI: offline validation of YAML files + cross-file references.

The Settings-construction path (.env loading) is pydantic's own behavior and
isn't re-tested here; these tests inject a Settings instance with paths under
tmp_path and assert on the finding lists.
"""

from __future__ import annotations

from pathlib import Path

from newsflow.checkconfig import _check_sources_yaml, _check_webhooks_yaml
from newsflow.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_token="dummy",
        webhooks_config_path=tmp_path / "webhooks.yaml",
        sources_config_path=tmp_path / "sources.yaml",
    )


def test_missing_files_are_informational_not_errors(tmp_path):
    errors: list[str] = []
    infos: list[str] = []
    settings = _settings(tmp_path)
    _check_webhooks_yaml(settings, errors, infos)
    _check_sources_yaml(settings, errors, infos)
    assert errors == []
    assert any("not present" in line for line in infos)


def test_unknown_key_in_webhooks_yaml_is_reported(tmp_path):
    (tmp_path / "webhooks.yaml").write_text(
        "destinations:\n  a:\n    url: https://example.com/h\n    secert: oops\n",
        encoding="utf-8",
    )
    errors: list[str] = []
    _check_webhooks_yaml(_settings(tmp_path), errors, [])
    assert len(errors) == 1 and "secert" in errors[0]


def test_dangling_webhook_destination_reference_is_an_error(tmp_path):
    # sources.yaml points a webhook subscriber at a destination that
    # webhooks.yaml never declares — it would sync fine and deliver nothing.
    (tmp_path / "webhooks.yaml").write_text(
        "destinations:\n  real:\n    url: https://example.com/h\n",
        encoding="utf-8",
    )
    (tmp_path / "sources.yaml").write_text(
        "sources:\n  s:\n    url: https://e/x\n    type: json_api\n"
        "    config:\n      items: '$.a'\n"
        "    subscribers:\n      - platform: webhook\n        channel: ghost\n",
        encoding="utf-8",
    )
    errors: list[str] = []
    _check_sources_yaml(_settings(tmp_path), errors, [])
    assert len(errors) == 1
    assert "ghost" in errors[0]


def test_declared_webhook_destination_reference_passes(tmp_path):
    (tmp_path / "webhooks.yaml").write_text(
        "destinations:\n  real:\n    url: https://example.com/h\n",
        encoding="utf-8",
    )
    (tmp_path / "sources.yaml").write_text(
        "sources:\n  s:\n    url: https://e/x\n    type: json_api\n"
        "    config:\n      items: '$.a'\n"
        "    subscribers:\n      - platform: webhook\n        channel: real\n",
        encoding="utf-8",
    )
    errors: list[str] = []
    _check_sources_yaml(_settings(tmp_path), errors, [])
    assert errors == []
