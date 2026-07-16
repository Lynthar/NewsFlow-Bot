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

    from newsflow.api.routes import feeds, health, ingest, stats

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

    # Add CORS middleware. allow_origins=["*"] with allow_credentials=True
    # is invalid per the CORS spec — browsers drop credentials when the
    # origin is a wildcard — so we keep the wildcard (the API is meant to
    # be callable from anywhere in self-hosted mode) and leave credentials
    # off. If a future deployment needs cookies/auth-headers cross-origin,
    # pin allow_origins to a specific list and flip this back on.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Include routers
    app.include_router(health.router, tags=["Health"])
    app.include_router(feeds.router, prefix="/api/feeds", tags=["Feeds"])
    app.include_router(stats.router, prefix="/api/stats", tags=["Statistics"])
    app.include_router(ingest.router, prefix="/api/ingest", tags=["Ingest"])

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
