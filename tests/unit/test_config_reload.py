"""Hot reload of the declarative configs (SIGHUP / POST /api/admin/reload).

Pins the failure semantics that make runtime reload safe: a file that fails
to parse keeps the previously synced state (no partial wipe), errors are
reported instead of raised, and one broken file doesn't block the other.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from newsflow.config import get_settings
from newsflow.core.feed_fetcher import FetchResult
from newsflow.models.webhook import WebhookDestination
from newsflow.services.config_reload import reload_declarative_configs


def _patch_session_factory(monkeypatch, session):
    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *a):
            return False

    factory = lambda: _Ctx()  # noqa: E731
    monkeypatch.setattr(
        "newsflow.services.webhook_sync.get_session_factory",
        lambda: factory,
    )


def _patch_feed_fetcher(monkeypatch):
    mock_fetcher = AsyncMock()
    mock_fetcher.fetch_feed = AsyncMock(
        return_value=FetchResult(
            url="",
            success=True,
            entries=[{"guid": "e1", "title": "E1", "link": "https://feed.example.com/e1"}],
            etag=None,
            last_modified=None,
            feed_title="Test Feed",
            feed_description=None,
        )
    )
    monkeypatch.setattr(
        "newsflow.services.feed_service.get_fetcher",
        lambda: mock_fetcher,
    )


def _point_settings_at(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "webhooks_config_path", tmp_path / "webhooks.yaml")
    monkeypatch.setattr(settings, "sources_config_path", tmp_path / "sources.yaml")


async def test_reload_applies_a_valid_file(session, monkeypatch, tmp_path):
    _patch_session_factory(monkeypatch, session)
    _patch_feed_fetcher(monkeypatch)
    _point_settings_at(monkeypatch, tmp_path)
    (tmp_path / "webhooks.yaml").write_text(
        "destinations:\n  a:\n    url: https://example.com/h\n",
        encoding="utf-8",
    )

    result = await reload_declarative_configs()

    assert result.ok is True
    dests = (await session.execute(select(WebhookDestination))).scalars().all()
    assert [d.name for d in dests] == ["a"]


async def test_reload_with_broken_file_keeps_previous_state(session, monkeypatch, tmp_path):
    _patch_session_factory(monkeypatch, session)
    _patch_feed_fetcher(monkeypatch)
    _point_settings_at(monkeypatch, tmp_path)
    path = tmp_path / "webhooks.yaml"
    path.write_text("destinations:\n  a:\n    url: https://example.com/h\n", encoding="utf-8")
    assert (await reload_declarative_configs()).ok is True

    # Now break the file: reload must report the error and leave the
    # destination from the previous sync untouched.
    path.write_text(
        "destinations:\n  b:\n    url: https://example.com/h2\n    secert: oops\n",
        encoding="utf-8",
    )
    result = await reload_declarative_configs()

    assert result.ok is False
    assert "secert" in result.detail
    dests = (await session.execute(select(WebhookDestination))).scalars().all()
    assert [d.name for d in dests] == ["a"]


async def test_reload_with_no_files_is_a_clean_noop(monkeypatch, tmp_path):
    _point_settings_at(monkeypatch, tmp_path)
    result = await reload_declarative_configs()
    assert result.ok is True
    assert "skipped" in result.detail


async def test_admin_reload_route_maps_failure_to_400(monkeypatch):
    from fastapi import HTTPException

    from newsflow.api.routes.admin import reload_configs
    from newsflow.services.config_reload import ReloadResult

    monkeypatch.setattr(
        "newsflow.services.config_reload.reload_declarative_configs",
        AsyncMock(return_value=ReloadResult(ok=False, detail="webhooks.yaml: boom")),
    )
    with pytest.raises(HTTPException) as exc:
        await reload_configs(_=None)
    assert exc.value.status_code == 400
    assert "boom" in exc.value.detail


async def test_admin_reload_route_returns_detail_on_success(monkeypatch):
    from newsflow.api.routes.admin import reload_configs
    from newsflow.services.config_reload import ReloadResult

    monkeypatch.setattr(
        "newsflow.services.config_reload.reload_declarative_configs",
        AsyncMock(return_value=ReloadResult(ok=True, detail="webhooks.yaml synced")),
    )
    response = await reload_configs(_=None)
    assert response.ok is True
    assert "synced" in response.detail
