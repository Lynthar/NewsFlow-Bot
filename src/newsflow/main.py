"""
NewsFlow Bot - Main Entry Point

Self-hosted RSS to Discord/Telegram bot with optional translation.
"""

import asyncio
import logging
import signal
import sys
from pathlib import Path

import structlog

from newsflow.config import get_settings, Settings
from newsflow.core import close_fetcher, get_scheduler, shutdown_scheduler
from newsflow.models import close_db, init_db


def setup_logging(settings: Settings) -> None:
    """Configure structured logging."""
    # Set log level
    log_level = getattr(logging, settings.log_level)

    # Configure structlog
    if settings.log_format == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Configure standard logging
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler()],
    )

    # Set third-party loggers to WARNING
    for logger_name in ["aiohttp", "discord", "telegram", "apscheduler"]:
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def ensure_data_dir(settings: Settings) -> None:
    """Ensure data directory exists."""
    data_dir = settings.data_dir
    if not data_dir.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        logging.info(f"Created data directory: {data_dir}")


async def start_discord_bot(settings: Settings) -> None:
    """Start Discord bot if enabled."""
    if not settings.discord_enabled:
        return

    logging.info("Starting Discord bot...")
    # Import here to avoid loading discord.py if not needed
    from newsflow.adapters.discord.bot import start_discord

    await start_discord(settings.discord_token)


async def start_telegram_bot(settings: Settings) -> None:
    """Start Telegram bot if enabled."""
    if not settings.telegram_enabled:
        return

    logging.info("Starting Telegram bot...")
    # Import here to avoid loading telegram if not needed
    from newsflow.adapters.telegram.bot import start_telegram

    await start_telegram(settings.telegram_token)


async def shutdown(loop: asyncio.AbstractEventLoop) -> None:
    """Graceful shutdown handler."""
    logging.info("Shutting down...")

    # Stop scheduler
    shutdown_scheduler(wait=False)

    # Close feed fetcher
    await close_fetcher()

    # Close database
    await close_db()

    # Cancel all tasks
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in tasks:
        task.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)
    logging.info("Shutdown complete")


async def start_api_server(settings: Settings) -> None:
    """Start REST API server if enabled."""
    if not settings.api_enabled:
        return

    logging.info(f"Starting API server on {settings.api_host}:{settings.api_port}...")

    try:
        from newsflow.api import run_api_server
        await run_api_server()
    except ImportError:
        logging.warning(
            "FastAPI not installed. Install with: pip install 'newsflow-bot[api]'"
        )


async def main() -> None:
    """Main entry point."""
    settings = get_settings()

    # Setup
    setup_logging(settings)
    logger = logging.getLogger(__name__)

    # Validate configuration
    if not settings.validate_minimal_config():
        logger.error("No platform token configured. Set DISCORD_TOKEN or TELEGRAM_TOKEN.")
        sys.exit(1)

    logger.info("=" * 50)
    logger.info("  NewsFlow Bot Starting...")
    logger.info("=" * 50)
    logger.info(f"  Discord:     {'✓ enabled' if settings.discord_enabled else '✗ disabled'}")
    logger.info(f"  Telegram:    {'✓ enabled' if settings.telegram_enabled else '✗ disabled'}")
    logger.info(f"  Translation: {'✓ enabled' if settings.can_translate() else '✗ disabled'}")
    logger.info(f"  REST API:    {'✓ enabled' if settings.api_enabled else '✗ disabled'}")
    logger.info(f"  Fetch Interval: {settings.fetch_interval_minutes} minutes")
    logger.info("=" * 50)

    # Ensure data directory exists
    ensure_data_dir(settings)

    # Initialize database
    logger.info("Initializing database...")
    await init_db()

    # Initialize cache if configured
    if settings.cache_backend == "redis" and settings.redis_url:
        from newsflow.services.cache import init_cache
        init_cache("redis", redis_url=settings.redis_url)
        logger.info("Redis cache initialized")
    else:
        from newsflow.services.cache import init_cache
        init_cache("memory")
        logger.info("Memory cache initialized")

    # Start scheduler
    scheduler = get_scheduler()
    scheduler.start()

    # Setup signal handlers
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown(loop)))

    # Start all services
    try:
        tasks = []

        if settings.discord_enabled:
            tasks.append(start_discord_bot(settings))

        if settings.telegram_enabled:
            tasks.append(start_telegram_bot(settings))

        if settings.api_enabled:
            tasks.append(start_api_server(settings))

        if not tasks:
            logger.error("No services to start!")
            return

        logger.info("All services starting...")

        # Run all services concurrently
        await asyncio.gather(*tasks)

    except asyncio.CancelledError:
        logger.info("Main tasks cancelled")
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        raise


def cli() -> None:
    """CLI entry point."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
