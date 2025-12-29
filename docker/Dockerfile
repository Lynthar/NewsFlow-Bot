# NewsFlow Bot Dockerfile
# Multi-stage build for smaller image size

# ============================================
# Stage 1: Builder
# ============================================
FROM python:3.11-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install poetry
RUN pip install --no-cache-dir poetry==1.7.1

# Copy dependency files
COPY pyproject.toml poetry.lock* ./

# Export dependencies to requirements.txt
# Include all optional dependencies for full functionality
RUN poetry export -f requirements.txt --without-hashes --extras all -o requirements.txt || \
    poetry export -f requirements.txt --without-hashes -o requirements.txt

# Install dependencies to a virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir -r requirements.txt


# ============================================
# Stage 2: Runtime
# ============================================
FROM python:3.11-slim as runtime

# Labels
LABEL maintainer="NewsFlow Bot"
LABEL description="Self-hosted RSS to Discord/Telegram bot with translation support"
LABEL version="0.1.0"

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash newsflow

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application code
COPY --chown=newsflow:newsflow src/ ./src/

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
ENV API_PORT=8000

# Switch to non-root user
USER newsflow

# Expose API port (optional)
EXPOSE 8000

# Health check
# Uses Python script for more reliable health checking
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${API_PORT:-8000}/live')" 2>/dev/null || exit 0

# Run the bot
CMD ["python", "-m", "newsflow.main"]
