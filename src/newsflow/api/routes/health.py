"""
Health check endpoints.

Provides endpoints for monitoring service health.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from newsflow import __version__
from newsflow.api.deps import get_db
from newsflow.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter()


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    timestamp: str
    version: str
    components: dict[str, Any]


class ReadinessResponse(BaseModel):
    """Readiness check response."""

    ready: bool
    checks: dict[str, bool]


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """
    Basic health check endpoint.

    Returns the service status and version.
    """
    settings = get_settings()

    return HealthResponse(
        status="healthy",
        timestamp=datetime.now(UTC).isoformat(),
        version=__version__,
        components={
            "discord_enabled": settings.discord_enabled,
            "telegram_enabled": settings.telegram_enabled,
            "translation_enabled": settings.can_translate(),
            "api_enabled": settings.api_enabled,
        },
    )


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse, "description": "Not ready"}},
)
async def readiness_check(
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """
    Readiness check endpoint.

    Verifies all required services are available. Not-ready returns HTTP 503:
    orchestrators and load balancers act on the status code, not the JSON
    body — a 200 with ready:false would keep routing traffic to a dead
    instance.
    """
    checks: dict[str, bool] = {}

    # Check database
    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:
        # The 503 alone says nothing about why; without this the operator has
        # a flapping readiness probe and no record of the cause.
        logger.warning("Readiness probe: database check failed", exc_info=True)
        checks["database"] = False

    # Check settings
    settings = get_settings()
    checks["config"] = settings.validate_minimal_config()

    # Overall readiness
    ready = all(checks.values())

    return JSONResponse(
        status_code=200 if ready else 503,
        content=ReadinessResponse(ready=ready, checks=checks).model_dump(),
    )


@router.get("/live")
async def liveness_check() -> dict[str, str]:
    """
    Liveness check endpoint.

    Simple check to verify the service is running.
    """
    return {"status": "alive"}
