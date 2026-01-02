# Copilot Instructions — NewsFlow Bot

Purpose: Help AI coding agents become productive quickly in this repository by documenting the architecture, key workflows, conventions, and integration points.

---

## Quick orientation (big picture)
- Core loop: FeedFetcher (src/newsflow/core/feed_fetcher.py) → FeedService/Repository → Dispatcher (src/newsflow/services/dispatcher.py) → Adapters (src/newsflow/adapters/*) which send messages to platforms.
- Components:
  - `core/` — scheduling, fetching, content processing
  - `services/` — business logic (feed, subscription, dispatcher, translation)
  - `adapters/` — platform-specific integration (Discord, Telegram, Webhook)
  - `models/` + `repositories/` — SQLAlchemy asyncio models + DB access
  - `api/` — optional FastAPI management endpoints
- Entrypoints:
  - CLI: `newsflow` console script → `src/newsflow/main.py::cli()`
  - Docker Compose: `docker/docker-compose.yml` (profiles for redis/postgres)

## Key developer workflows (how to run, test, debug)
- Install: `make install` (or `poetry install`)
- Run locally: `make dev` (runs `python -m newsflow.main`) or `poetry run python -m newsflow.main`
- Tests: `make test` (uses pytest + pytest-asyncio). Tests live under `tests/` and pytest config is in `pyproject.toml` (`testpaths = [