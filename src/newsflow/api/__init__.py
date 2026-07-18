"""
FastAPI REST API for NewsFlow Bot.

Provides endpoints for:
- Health checks
- Feed management
- Statistics
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from newsflow import __version__
from newsflow.config import get_settings

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def create_app() -> "FastAPI":
    """
    Create and configure the FastAPI application.

    This function is only called when the API is enabled.
    """
    try:
        from fastapi import FastAPI
        from fastapi.middleware.cors import CORSMiddleware
    except ImportError:
        raise ImportError(
            "FastAPI is required for the API service. "
            "Install it with: pip install 'newsflow-bot[api]'"
        )

    from newsflow.api.routes import (
        admin,
        feeds,
        health,
        ingest,
        metrics,
        stats,
        subscriptions,
    )

    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        """Application lifespan handler."""
        logger.info("API starting up...")
        yield
        logger.info("API shutting down...")

    app = FastAPI(
        title="NewsFlow Bot API",
        description="REST API for managing NewsFlow Bot feeds and subscriptions",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.log_level == "DEBUG" else None,
        redoc_url="/redoc" if settings.log_level == "DEBUG" else None,
    )

    # CORS is opt-in: no configured origins = no CORS headers at all (the
    # default deployment is loopback/API-key anyway). API_CORS_ORIGINS=*
    # restores the old blanket wildcard; credentials stay off because a
    # wildcard origin with credentials is invalid per the CORS spec.
    if settings.api_cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.api_cors_origins,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Include routers. Data-bearing GET routers carry the read gate (open
    # until an API key is configured, then key-required); health probes stay
    # open for container HEALTHCHECK / orchestrator probes. The gate on
    # write-only routers is harmless — their routes demand the same key.
    from fastapi import Depends

    from newsflow.api.deps import require_read_api_key

    read_gate = [Depends(require_read_api_key)]
    app.include_router(health.router, tags=["Health"])
    app.include_router(feeds.router, prefix="/api/feeds", tags=["Feeds"], dependencies=read_gate)
    app.include_router(
        stats.router, prefix="/api/stats", tags=["Statistics"], dependencies=read_gate
    )
    app.include_router(ingest.router, prefix="/api/ingest", tags=["Ingest"])
    app.include_router(admin.router, prefix="/api/admin", tags=["Admin"])
    app.include_router(
        subscriptions.router,
        prefix="/api/subscriptions",
        tags=["Subscriptions"],
        dependencies=read_gate,
    )
    app.include_router(metrics.router, tags=["Metrics"], dependencies=read_gate)

    return app


async def run_api_server() -> None:
    """Run the API server."""
    try:
        import uvicorn
    except ImportError:
        raise ImportError(
            "Uvicorn is required for the API service. "
            "Install it with: pip install 'newsflow-bot[api]'"
        )

    settings = get_settings()

    if not settings.api_enabled:
        logger.info("API server is disabled")
        return

    app = create_app()

    config = uvicorn.Config(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
        # Uvicorn's default dictConfig attaches its own plain-text handlers
        # with propagate=False, which interleaves non-JSON lines into the
        # LOG_FORMAT=json stream. None skips that config entirely so uvicorn
        # records propagate to the root handler's shared formatter.
        log_config=None,
    )
    server = uvicorn.Server(config)

    logger.info(f"Starting API server on {settings.api_host}:{settings.api_port}")
    await server.serve()
