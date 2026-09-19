"""Hot reload of the declarative configs (SIGHUP / POST /api/admin/reload).

Pins the failure semantics that make runtime reload safe: a file that fails
to parse keeps the previously synced state (no partial wipe), errors are
reported instead of raised, and one broken file doesn't block the other.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from newsflow.models.webhook import WebhookDestination
from newsflow.services.config_reload import reload_declarative_configs

VALID = "destinations:\n  a:\n    url: https://example.com/h\n"
BROKEN = "destinations:\n  b:\n    url: https://example.com/h2\n    secert: oops\n"


@pytest_asyncio.fixture
async def session(db):
    """A session on the shared test database; the sync opens its own alongside."""
    async with db() as s:
        yield s


@pytest.fixture
def yaml_dir(configure, tmp_path):
    """Both declarative files live under tmp_path; a file exists iff the test wrote it."""
    configure(
        webhooks_config_path=tmp_path / "webhooks.yaml",
        sources_config_path=tmp_path / "sources.yaml",
    )
    return tmp_path


async def _destination_names(session) -> list[str]:
    return [d.name for d in await session.scalars(select(WebhookDestination))]


async def test_reload_applies_a_valid_file(session, yaml_dir):
    (yaml_dir / "webhooks.yaml").write_text(VALID, encoding="utf-8")

    result = await reload_declarative_configs()

    assert result.ok is True
    assert await _destination_names(session) == ["a"]


async def test_reload_with_broken_file_keeps_previous_state(session, yaml_dir):
    path = yaml_dir / "webhooks.yaml"
    path.write_text(VALID, encoding="utf-8")
    assert (await reload_declarative_configs()).ok is True

    # Now break the file: reload must report the error and leave the
    # destination from the previous sync untouched.
    path.write_text(BROKEN, encoding="utf-8")
    result = await reload_declarative_configs()

    assert result.ok is False
    assert "secert" in result.detail
    assert await _destination_names(session) == ["a"]


async def test_reload_with_no_files_is_a_clean_noop(yaml_dir):
    result = await reload_declarative_configs()
    assert result.ok is True
    assert "skipped" in result.detail


async def test_admin_reload_route_maps_failure_to_400(db, yaml_dir):
    from fastapi import HTTPException

    from newsflow.api.routes.admin import reload_configs

    (yaml_dir / "webhooks.yaml").write_text(BROKEN, encoding="utf-8")
    with pytest.raises(HTTPException) as exc:
        await reload_configs(_=None)
    assert exc.value.status_code == 400
    assert "secert" in exc.value.detail


async def test_admin_reload_route_returns_detail_on_success(db, yaml_dir):
    from newsflow.api.routes.admin import reload_configs

    (yaml_dir / "webhooks.yaml").write_text(VALID, encoding="utf-8")
    response = await reload_configs(_=None)
    assert response.ok is True
    assert "synced" in response.detail
