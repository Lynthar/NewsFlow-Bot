"""
Health check endpoints.

Provides endpoints for monitoring service health.
"""

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from newsflow.api.deps import get_db
from newsflow.config import get_settings

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
        timestamp=datetime.now(timezone.utc).isoformat(),
        version="0.1.0",
        components={
            "discord_enabled": settings.discord_enabled,
            "telegram_enabled": settings.telegram_enabled,
            "translation_enabled": settings.can_translate(),
            "api_enabled": settings.api_enabled,
        },
    )


@router.get("/ready", response_model=ReadinessResponse)
async def readiness_check(
    db: AsyncSession = Depends(get_db),
) -> ReadinessResponse:
    """
    Readiness check endpoint.

    Verifies all required services are available.
    """
    checks: dict[str, bool] = {}

    # Check database
    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:
        checks["database"] = False

    # Check settings
    settings = get_settings()
    checks["config"] = settings.validate_minimal_config()

    # Overall readiness
    ready = all(checks.values())

    return ReadinessResponse(ready=ready, checks=checks)


@router.get("/live")
async def liveness_check() -> dict[str, str]:
    """
    Liveness check endpoint.

    Simple check to verify the service is running.
    """
    return {"status": "alive"}
