"""create_app construction: route wiring, opt-in CORS, and the api_host
default. No server is started — assertions walk the FastAPI app object."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")  # needs the api extra
pytest.importorskip("httpx")  # drives the app over ASGI; not an api-extra dependency

import httpx  # noqa: E402

from newsflow.api import create_app  # noqa: E402
from newsflow.config import Settings  # noqa: E402


def test_routes_include_admin_and_subscriptions(configure):
    configure(telegram_token="dummy")
    app = create_app()
    # starlette 1.x hides included routes behind lazy router objects — the
    # OpenAPI schema is the stable public surface to assert against.
    paths = set(app.openapi()["paths"])
    assert "/api/admin/reload" in paths
    assert "/api/subscriptions" in paths
    assert "/api/subscriptions/{sub_id}/pause" in paths
    assert "/api/subscriptions/opml" in paths
    assert "/health" in paths


def test_cors_is_off_by_default_and_opt_in(configure):
    from fastapi.middleware.cors import CORSMiddleware

    configure(telegram_token="dummy")
    app = create_app()
    assert all(m.cls is not CORSMiddleware for m in app.user_middleware)

    configure(api_cors_origins=["https://dash.example.com"])
    app = create_app()
    assert any(m.cls is CORSMiddleware for m in app.user_middleware)


async def _request(method: str, path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        return await client.request(method, path, headers=headers)


async def test_cors_never_allows_credentials(configure):
    configure(api_cors_origins=["https://dash.example.com"])

    response = await _request(
        "OPTIONS",
        "/api/feeds",
        {"Origin": "https://dash.example.com", "Access-Control-Request-Method": "GET"},
    )

    assert response.headers["access-control-allow-origin"] == "https://dash.example.com"
    assert "access-control-allow-credentials" not in response.headers


# ===== read gate =====

_DATA_READS = [
    "/api/feeds",
    "/api/stats",
    "/api/subscriptions?platform=discord&channel=c1",
    "/metrics",
]


@pytest.mark.parametrize("path", _DATA_READS)
async def test_an_api_key_closes_every_data_read(db, configure, path):
    # Feed URLs often embed tokens, so once a key exists reading them needs it too.
    configure(api_key="secret")

    assert (await _request("GET", path)).status_code == 401
    authorized = await _request("GET", path, {"Authorization": "Bearer secret"})
    assert authorized.status_code == 200


async def test_the_health_probe_stays_open_when_an_api_key_is_set(db, configure):
    configure(api_key="secret")

    assert (await _request("GET", "/health")).status_code == 200


async def test_reads_are_open_until_an_api_key_is_configured(db, configure):
    configure(api_key="")

    assert (await _request("GET", "/api/feeds")).status_code == 200


def test_api_host_defaults_to_loopback():
    assert Settings(telegram_token="dummy").api_host == "127.0.0.1"


def test_cors_origins_accepts_comma_form():
    settings = Settings(telegram_token="dummy", api_cors_origins="https://a.com, https://b.com")
    assert settings.api_cors_origins == ["https://a.com", "https://b.com"]


# ===== readiness status codes =====


class _GoodDB:
    async def execute(self, *args, **kwargs):
        return None


class _BadDB:
    async def execute(self, *args, **kwargs):
        raise RuntimeError("db down")


async def test_ready_returns_200_when_healthy(configure):
    from newsflow.api.routes.health import readiness_check

    configure(telegram_token="x")
    resp = await readiness_check(db=_GoodDB())
    assert resp.status_code == 200


async def test_ready_returns_503_when_db_down(configure):
    """Orchestrators and load balancers act on the status code, not the
    body — a 200 with ready:false kept routing traffic to a dead app."""
    import json

    from newsflow.api.routes.health import readiness_check

    configure(telegram_token="x")
    resp = await readiness_check(db=_BadDB())
    assert resp.status_code == 503
    body = json.loads(resp.body)
    assert body["ready"] is False
    assert body["checks"]["database"] is False
