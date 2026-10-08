# NewsFlow Bot Dockerfile
# Multi-stage build for smaller image size

# ============================================
# Stage 1: Builder
# ============================================
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies (for lxml / cryptography / grpcio C extensions)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install exactly what poetry.lock pins, so the image is what the tests ran against.
# The lock is exported here; Poetry lives in this stage only, never in the runtime image.
COPY pyproject.toml poetry.lock README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir "poetry==2.5.1" "poetry-plugin-export==1.10.1" \
 && poetry export -f requirements.txt --all-extras --without-hashes -o /tmp/requirements.txt

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# The import check aborts the build, naming the culprit, if an optional package is missing:
# otherwise the bot would start with digest or translation silently broken.
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && pip install --no-cache-dir --no-deps . \
 && python -c "import openai, deepl, redis, fastapi, aiohttp, feedparser, sqlalchemy, yaml, defusedxml; print('build deps OK; openai=', openai.__version__)"


# ============================================
# Stage 2: Runtime
# ============================================
FROM python:3.11-slim AS runtime

# Labels
LABEL maintainer="NewsFlow Bot"
LABEL description="Self-hosted RSS to Discord/Telegram bot with translation support"
# Keep in sync with pyproject.toml [tool.poetry] version and
# src/newsflow/__init__.py __version__ on every release bump.
LABEL version="0.9.9"

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash newsflow

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application code + alembic migrations
COPY --chown=newsflow:newsflow src/ ./src/
COPY --chown=newsflow:newsflow alembic/ ./alembic/
COPY --chown=newsflow:newsflow alembic.ini ./alembic.ini

# LGPL v3 wants the notice to travel with the object it applies to, and what
# ships is the image, not the repo. The README carries that notice; the LGPL and
# GPL texts themselves already arrive inside /opt/venv with the library.
COPY --chown=newsflow:newsflow LICENSE README.md ./

# Create data directory with correct permissions
RUN mkdir -p /app/data && chown newsflow:newsflow /app/data

# Set environment variables
ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Default configuration
ENV DATABASE_URL=sqlite+aiosqlite:///./data/newsflow.db
ENV LOG_LEVEL=INFO
ENV API_ENABLED=false
# The app default is loopback (127.0.0.1); inside a container the published
# port mapping is the exposure boundary, so bind wide here. Overridable via
# .env like everything else.
ENV API_HOST=0.0.0.0
ENV API_PORT=8000

# Switch to non-root user
USER newsflow

# Expose API port (optional)
EXPOSE 8000

# Unhealthy if the dispatch heartbeat is missing or any heartbeat file (one per loop and
# connected platform) is older than 120 minutes. dispatch is touched once per round, so
# FETCH_INTERVAL_MINUTES above ~100 needs a higher threshold or the check flaps.
HEALTHCHECK --interval=60s --timeout=5s --start-period=5m --retries=2 \
    CMD [ -f /app/data/heartbeat/dispatch ] \
     && [ "$(find /app/data/heartbeat -type f -mmin +120 2>/dev/null | wc -l)" = "0" ]

# Run the bot
CMD ["python", "-m", "newsflow.main"]
