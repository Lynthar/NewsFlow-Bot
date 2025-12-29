# NewsFlow Bot - Development Commands

.PHONY: install dev test lint format run docker-build docker-up docker-down clean

# ============================================
# Development
# ============================================

# Install dependencies
install:
	poetry install

# Install with all optional dependencies
install-all:
	poetry install --all-extras

# Run development server
dev:
	poetry run python -m newsflow.main

run: dev

# Run tests
test:
	poetry run pytest tests/ -v

# Run tests with coverage
test-cov:
	poetry run pytest tests/ -v --cov=newsflow --cov-report=html

# Type check
typecheck:
	poetry run mypy src/

# Lint code
lint:
	poetry run ruff check src/

# Format code
format:
	poetry run ruff format src/
	poetry run ruff check --fix src/

# ============================================
# Docker - Basic
# ============================================

# Build Docker image
docker-build:
	docker build -f docker/Dockerfile -t newsflow-bot:latest .

# Start with Docker Compose
docker-up:
	docker-compose -f docker/docker-compose.yml up -d

# Stop Docker Compose
docker-down:
	docker-compose -f docker/docker-compose.yml down

# View Docker logs
docker-logs:
	docker-compose -f docker/docker-compose.yml logs -f newsflow

# Restart container
docker-restart:
	docker-compose -f docker/docker-compose.yml restart newsflow

# ============================================
# Docker - With Redis
# ============================================

# Start with Redis cache
docker-up-redis:
	docker-compose -f docker/docker-compose.yml --profile with-redis up -d

# Stop with Redis
docker-down-redis:
	docker-compose -f docker/docker-compose.yml --profile with-redis down

# ============================================
# Docker - With PostgreSQL
# ============================================

# Start with PostgreSQL
docker-up-postgres:
	docker-compose -f docker/docker-compose.yml --profile with-postgres up -d

# Stop with PostgreSQL
docker-down-postgres:
	docker-compose -f docker/docker-compose.yml --profile with-postgres down

# ============================================
# Docker - Full Stack (Redis + PostgreSQL)
# ============================================

# Start full stack
docker-up-full:
	docker-compose -f docker/docker-compose.yml --profile with-redis --profile with-postgres up -d

# Stop full stack
docker-down-full:
	docker-compose -f docker/docker-compose.yml --profile with-redis --profile with-postgres down

# ============================================
# Database
# ============================================

# Initialize database
db-init:
	poetry run python -c "import asyncio; from newsflow.models import init_db; asyncio.run(init_db())"

# Create migration
db-migrate:
	poetry run alembic revision --autogenerate -m "$(msg)"

# Apply migrations
db-upgrade:
	poetry run alembic upgrade head

# ============================================
# Cleanup
# ============================================

# Clean build artifacts
clean:
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info/
	rm -rf .pytest_cache/
	rm -rf .mypy_cache/
	rm -rf .ruff_cache/
	rm -rf htmlcov/
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete

# Clean Docker resources
docker-clean:
	docker-compose -f docker/docker-compose.yml down -v --rmi local

# ============================================
# Help
# ============================================

help:
	@echo "NewsFlow Bot - Commands"
	@echo ""
	@echo "Development:"
	@echo "  make install       Install dependencies"
	@echo "  make install-all   Install with all optional dependencies"
	@echo "  make dev/run       Run development server"
	@echo "  make test          Run tests"
	@echo "  make lint          Lint code"
	@echo "  make format        Format code"
	@echo ""
	@echo "Docker:"
	@echo "  make docker-build  Build Docker image"
	@echo "  make docker-up     Start services"
	@echo "  make docker-down   Stop services"
	@echo "  make docker-logs   View logs"
	@echo "  make docker-restart Restart container"
	@echo ""
	@echo "Docker with extras:"
	@echo "  make docker-up-redis    Start with Redis cache"
	@echo "  make docker-up-postgres Start with PostgreSQL"
	@echo "  make docker-up-full     Start with Redis + PostgreSQL"
	@echo ""
	@echo "Database:"
	@echo "  make db-init       Initialize database"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean         Clean build artifacts"
	@echo "  make docker-clean  Clean Docker resources"
