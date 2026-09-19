"""
NewsFlow Bot - Main Entry Point

Self-hosted RSS to Discord/Telegram bot with optional translation.
"""

import asyncio
import logging
import signal
import sys
from typing import Any

import structlog
from structlog.typing import Processor

from newsflow.config import Settings, get_settings
from newsflow.core import close_fetcher
from newsflow.models import close_db
from newsflow.models.migrate import upgrade_to_head
from newsflow.services.dispatcher import get_dispatcher


def setup_logging(settings: Settings) -> None:
    """Configure structured logging.

    structlog events and plain stdlib records (aiohttp, discord, …) are
    rendered through one shared ProcessorFormatter, so ``LOG_FORMAT`` selects
    the output for both: ``json`` for machine-readable logs, ``console`` for
    human-readable dev output.
    """
    log_level = getattr(logging, settings.log_level)

    timestamper = structlog.processors.TimeStamper(fmt="iso")
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        # Required for exc_info to render as a traceback in json mode: JSONRenderer
        # otherwise falls back to repr() for stdlib records and drops the stack for
        # structlog events. Shared by both chains.
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if settings.log_format == "json"
        else structlog.dev.ConsoleRenderer()
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)

    # Several third-party loggers leak secrets at DEBUG: httpx's INFO request line
    # carries the bot token in the URL path, and aiosqlite logs bound SQL parameters.
    # Pinned to WARNING; opt into SQL tracing via DB_ECHO, never via LOG_LEVEL.
    for logger_name in [
        "aiohttp",
        "discord",
        "telegram",
        "httpx",
        "httpcore",
        "aiosqlite",
    ]:
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def ensure_data_dir(settings: Settings) -> None:
    """Ensure data directory exists."""
    data_dir = settings.data_dir
    if not data_dir.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        logging.info(f"Created data directory: {data_dir}")


def clear_stale_heartbeats(settings: Settings) -> None:
    """Delete leftover heartbeat files from the previous run.

    Heartbeats live in the persistent data volume, so they survive restarts.
    The Docker HEALTHCHECK flags the container unhealthy if ANY heartbeat
    file is stale — without this sweep, disabling a platform (e.g. removing
    DISCORD_TOKEN) leaves its old heartbeat behind and the container goes
    permanently unhealthy two hours later. Each enabled task re-creates its
    own file within the healthcheck's start period.
    """
    heartbeat_dir = settings.data_dir / "heartbeat"
    if not heartbeat_dir.is_dir():
        return
    for path in heartbeat_dir.iterdir():
        try:
            if path.is_file():
                path.unlink()
        except OSError as e:
            logging.warning(f"Could not remove stale heartbeat {path.name}: {e}")


async def start_discord_bot(settings: Settings) -> None:
    """Start Discord bot if enabled."""
    if not settings.discord_enabled:
        return

    logging.info("Starting Discord bot...")
    # Import here to avoid loading discord.py if not needed
    from newsflow.adapters.discord.bot import start_discord

    assert settings.discord_token is not None
    await start_discord(settings.discord_token)


async def start_telegram_bot(settings: Settings) -> None:
    """Start Telegram bot if enabled."""
    if not settings.telegram_enabled:
        return

    logging.info("Starting Telegram bot...")
    # Import here to avoid loading telegram if not needed
    from newsflow.adapters.telegram.bot import start_telegram

    assert settings.telegram_token is not None
    await start_telegram(settings.telegram_token)


async def start_webhook_adapter_task(settings: Settings) -> None:
    """Start the webhook adapter if webhooks.yaml is present."""
    if not settings.webhooks_enabled:
        return

    # Import here to avoid loading aiohttp/yaml eagerly when the feature is off.
    from newsflow.adapters.webhook.bot import start_webhook

    logging.info("Starting webhook adapter...")
    await start_webhook()


async def shutdown(loop: asyncio.AbstractEventLoop) -> None:
    """Graceful shutdown handler.

    Order matters: platform adapters stop first (they stop polling /
    accepting commands and their start() tasks return), then the remaining
    loops are cancelled, and the shared HTTP client and database close LAST —
    closing the engine while a dispatch round is mid-commit would turn an
    orderly stop into a burst of connection errors.
    """
    logging.info("Shutting down...")

    settings = get_settings()
    if settings.telegram_enabled:
        try:
            from newsflow.adapters.telegram.bot import stop_telegram

            await stop_telegram()
        except Exception:
            logging.exception("Telegram adapter did not stop cleanly")
    if settings.discord_enabled:
        try:
            from newsflow.adapters.discord.bot import stop_discord

            await stop_discord()
        except Exception:
            logging.exception("Discord adapter did not stop cleanly")
    if settings.webhooks_enabled:
        try:
            from newsflow.adapters.webhook.bot import stop_webhook

            await stop_webhook()
        except Exception:
            logging.exception("Webhook adapter did not stop cleanly")

    # Cancel remaining tasks (dispatch/cleanup/digest/monitor loops, the
    # platform keep-alive wrappers) and wait for them to unwind.
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    # Close shared clients only after nothing can be using them.
    await close_fetcher()
    await close_db()
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
        logging.warning("FastAPI not installed. Install with: pip install 'newsflow-bot[api]'")


async def start_dispatch_loop(settings: Settings) -> None:
    """Start the unified dispatch loop for all platforms."""
    dispatcher = get_dispatcher()
    logging.info("Starting unified dispatch loop...")
    await dispatcher.run_dispatch_loop(settings.fetch_interval_minutes)


async def start_cleanup_loop() -> None:
    """Start the periodic cleanup loop."""
    dispatcher = get_dispatcher()
    await dispatcher.run_cleanup_loop()


async def start_platform_monitor() -> None:
    """Emit per-platform heartbeats while each adapter reports as connected."""
    dispatcher = get_dispatcher()
    await dispatcher.run_platform_monitor()


async def start_digest_loop() -> None:
    """Periodically deliver AI-generated digests to channels that enabled them."""
    dispatcher = get_dispatcher()
    await dispatcher.run_digest_loop()


async def main() -> None:
    """Main entry point."""
    settings = get_settings()

    # Setup
    setup_logging(settings)
    logger = logging.getLogger(__name__)

    # Validate configuration
    if not settings.validate_minimal_config():
        logger.error(
            "No delivery platform configured. Set DISCORD_TOKEN or TELEGRAM_TOKEN, "
            "or provide a webhooks.yaml for a webhook-only deployment."
        )
        sys.exit(1)

    logger.info("=" * 50)
    logger.info("  NewsFlow Bot Starting...")
    logger.info("=" * 50)
    logger.info(f"  Discord:     {'✓ enabled' if settings.discord_enabled else '✗ disabled'}")
    logger.info(f"  Telegram:    {'✓ enabled' if settings.telegram_enabled else '✗ disabled'}")
    logger.info(f"  Webhook:     {'✓ enabled' if settings.webhooks_enabled else '✗ disabled'}")
    logger.info(f"  Translation: {'✓ enabled' if settings.can_translate() else '✗ disabled'}")
    logger.info(f"  REST API:    {'✓ enabled' if settings.api_enabled else '✗ disabled'}")
    logger.info(f"  Fetch Interval: {settings.fetch_interval_minutes} minutes")
    logger.info("=" * 50)

    # Ensure data directory exists
    ensure_data_dir(settings)

    # Sweep heartbeat files from the previous run so a disabled platform's
    # leftover file can't trip the Docker HEALTHCHECK as permanently stale.
    clear_stale_heartbeats(settings)

    # Apply database migrations (creates schema on a fresh DB, evolves it
    # on an upgraded deploy).
    await upgrade_to_head()

    # Reconcile webhook destinations + subscriptions from webhooks.yaml.
    # Runs before services start so the first dispatch cycle sees a correct
    # subscription set. Missing YAML file = feature disabled, so skip quietly.
    if settings.webhooks_enabled:
        from newsflow.services.webhook_sync import (
            WebhookConfigError,
            sync_webhooks,
        )

        try:
            await sync_webhooks(settings.webhooks_config_path)
        except WebhookConfigError as e:
            logger.error(f"webhooks.yaml is invalid; aborting startup. {e}")
            sys.exit(1)

    # Reconcile declarative non-RSS sources (JSON-API, IMAP email) from
    # sources.yaml — same file-presence opt-in and fail-fast policy as webhooks.
    if settings.sources_enabled:
        from newsflow.services.source_sync import SourceConfigError, sync_sources

        try:
            await sync_sources(settings.sources_config_path)
        except SourceConfigError as e:
            logger.error(f"sources.yaml is invalid; aborting startup. {e}")
            sys.exit(1)

    # Initialize cache if configured
    if settings.cache_backend == "redis" and settings.redis_url:
        from newsflow.services.cache import init_cache

        init_cache("redis", redis_url=settings.redis_url)
        logger.info("Redis cache initialized")
    else:
        from newsflow.services.cache import init_cache

        init_cache("memory")
        logger.info("Memory cache initialized")

    # loop.add_signal_handler is unimplemented on Windows' ProactorEventLoop; Ctrl+C
    # still surfaces as KeyboardInterrupt and is caught in cli(). `_shutdown_tasks`
    # holds strong refs — the loop only weak-refs bare create_task results.
    loop = asyncio.get_running_loop()
    _shutdown_tasks: set[asyncio.Task[Any]] = set()

    def _trigger_shutdown() -> None:
        task = asyncio.create_task(shutdown(loop), name="shutdown")
        _shutdown_tasks.add(task)
        task.add_done_callback(_shutdown_tasks.discard)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _trigger_shutdown)
        except NotImplementedError:
            logger.debug(f"Signal {sig.name} handler not supported on this platform")

    # SIGHUP (Unix only) hot-reloads the declarative YAML configs — the
    # admin API offers the same via POST /api/admin/reload. A bad file
    # keeps the previous state instead of killing the process.
    if hasattr(signal, "SIGHUP"):

        def _trigger_reload() -> None:
            from newsflow.services.config_reload import reload_declarative_configs

            get_dispatcher().spawn(reload_declarative_configs(), name="config-reload")

        try:
            loop.add_signal_handler(signal.SIGHUP, _trigger_reload)
        except NotImplementedError:
            logger.debug("SIGHUP handler not supported on this platform")

    # Start all services
    try:
        tasks = []

        if settings.discord_enabled:
            tasks.append(start_discord_bot(settings))

        if settings.telegram_enabled:
            tasks.append(start_telegram_bot(settings))

        if settings.webhooks_enabled:
            tasks.append(start_webhook_adapter_task(settings))

        if settings.api_enabled:
            tasks.append(start_api_server(settings))

        if not tasks:
            logger.error("No services to start!")
            return

        # Add the unified dispatch loop (runs for all platforms)
        tasks.append(start_dispatch_loop(settings))

        # Add the cleanup loop (deletes old entries/sent records)
        tasks.append(start_cleanup_loop())

        # Add the platform monitor (per-platform heartbeats for HEALTHCHECK)
        tasks.append(start_platform_monitor())

        # Add the digest loop (periodic AI-generated summaries)
        tasks.append(start_digest_loop())

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
