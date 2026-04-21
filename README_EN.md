# NewsFlow Bot

<div align="center">

**Self-hosted RSS push backend for Discord / Telegram bots — subscriptions, translation, health management**

[![Python](https://img.shields.io/badge/Python-3.11%20–%203.13-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![discord.py](https://img.shields.io/badge/discord.py-2.3+-7289da.svg)](https://github.com/Rapptz/discord.py)
[![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-20.7+-0088cc.svg)](https://python-telegram-bot.org/)
[![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0-red.svg)](https://www.sqlalchemy.org/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ed.svg)](https://www.docker.com/)

English | [简体中文](README.md)

</div>

---

## 📖 Table of Contents

- [What is this?](#-what-is-this)
- [Features](#-features)
- [Architecture](#️-architecture)
- [Quick Start (Docker)](#-quick-start-docker)
- [Requirements](#-requirements)
- [Configuration](#️-configuration)
- [Command Reference](#-command-reference)
  - [Discord slash commands](#discord-slash-commands)
  - [Telegram commands](#telegram-commands)
- [OPML Import / Export](#-opml-import--export)
- [REST API (Optional)](#-rest-api-optional)
- [Advanced Deployment](#-advanced-deployment)
- [Health Checks](#-health-checks)
- [FAQ](#-faq)
- [Contributing](#-contributing)
- [License](#-license)

---

## 🎯 What is this?

NewsFlow Bot is an RSS push backend you run **on your own server**. Give it a Discord or Telegram bot token, run `/feed add <url>` in your channel, and the bot will push new articles to that channel whenever the source updates — optionally translating them to the language you want.

**Design principles**

- 🏠 **Self-hosted first** — your VPS, your tokens, your translation API keys; data stays with you
- ⚡ **Zero-config start** — minimum setup is one platform token + `docker compose up`
- 🧩 **Progressive complexity** — translation, REST API, Redis, Postgres are all optional; enable as needed
- 🛡️ **Production-ready** — exponential backoff, SSRF guard, rate limiting, health checks, alembic migrations built in

**Non-goals (won't be added here)**

- ❌ No multi-tenancy / quotas / billing (a centralized variant will live in a separate repo)
- ❌ No bundled web UI (manage via bot commands or the optional REST API)
- ❌ No forced translation (off by default; configure DeepL / OpenAI / Google to enable)

---

## ✨ Features

| Feature | Notes |
|---|---|
| 📡 **RSS fetching** | `feedparser` + `aiohttp`, ETag / Last-Modified conditional requests, concurrent fetch, size cap, SSRF guard |
| 🌐 **Multi-platform push** | Discord (slash commands) + Telegram (prefix commands) in parallel; platform and channel isolated |
| 🌍 **Automatic translation** | Optional DeepL / OpenAI-compatible / Google Cloud Translation; two-tier cache (DB + memory/Redis) |
| 🔁 **Exponential backoff** | Transient source failures automatically stretch the retry interval; 10 consecutive failures auto-disables and **notifies subscribers** |
| 📋 **OPML import/export** | Migrate from Feedly / Reeder / any RSS reader, or back up your subscription list |
| ⏸ **Pause / resume** | Temporarily stop a feed without losing the subscription |
| 👀 **Subscribe preview** | Push the latest article within seconds of subscribing, no need to wait for the next cycle |
| 🩺 **Health visibility** | `/feed status <url>` shows error count, backoff window, recent articles |
| 🐳 **Docker ready** | Single `docker compose up`; built-in heartbeat healthcheck + auto alembic migrations |
| 🔒 **Chain-of-safety** | URL scheme / host allowlist (rejects private / loopback / cloud-metadata IPs); 5 MiB response cap |
| 🔧 **Flexible DB** | SQLite by default; change one URL to switch to PostgreSQL |

---

## 🏗️ Architecture

```
                   ┌──────────── NewsFlow Process ────────────┐
                   │                                           │
                   │   Single asyncio event loop runs:          │
                   │                                           │
Discord Slash ◀───▶│  • Discord Adapter       ┐                │
Commands           │                          │                │
Telegram  ◀───────▶│  • Telegram Adapter      │ register with   │
Polling            │                          │ Dispatcher       │
                   │  • Dispatch Loop         ┘                │
                   │    (every N min: fetch → translate → send) │
                   │  • Cleanup Loop                           │
                   │    (every 24h: prune old entries)          │
                   │  • Platform Monitor                       │
                   │    (every 30s: check adapters → heartbeat) │
                   │                                           │
                   │  Shared singletons: Settings / Fetcher /    │
                   │  TranslationService / Cache / DB Engine     │
                   │                                           │
                   └───────────────┬───────────────────────────┘
                                   │
                                   ▼
                    SQLite file  /  PostgreSQL (optional)
```

**Layers**: `adapters/` & `api/` → `services/` (business logic) → `repositories/` (data access) → `models/` (SQLAlchemy ORM); `core/` holds stateless primitives (fetcher, HTML cleaner, URL validator, time format).

Deep architecture notes live in [DEVELOPMENT.md](DEVELOPMENT.md).

---

## 🚀 Quick Start (Docker)

Example for **Debian 12 / Ubuntu 22+**. Swap the Docker install commands for your distro if needed.

### 1. Install Docker (if you haven't)

```bash
sudo apt update
sudo apt install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg | \
    sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/debian $(lsb_release -cs) stable" | \
    sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
```

### 2. Get a bot token

<details>
<summary><b>Discord Bot Token</b></summary>

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and create an application
2. On the `Bot` page, click `Reset Token` to get one
3. Enable **MESSAGE CONTENT INTENT**
4. On `OAuth2 → URL Generator` tick `bot` + `applications.commands`, grant `Send Messages`, `Embed Links`, `Read Message History`
5. Invite the bot to your server with the generated URL
</details>

<details>
<summary><b>Telegram Bot Token</b></summary>

1. Message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`
2. Follow the prompts to name the bot
3. Grab the token — format looks like `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`
</details>

### 3. Clone, configure, start

```bash
git clone https://github.com/Lynthar/NewsFlow-Bot.git
cd NewsFlow-Bot

cp .env.example .env
nano .env     # fill in at least one token

docker compose -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml logs -f newsflow
```

When you see `Discord bot logged in as ...` or `Telegram bot started successfully`, you're live.

### 4. Try commands in your channel / chat

Discord:
```
/feed test https://feeds.bbci.co.uk/news/rss.xml
/feed add  https://feeds.bbci.co.uk/news/rss.xml
```

Telegram:
```
/test https://feeds.bbci.co.uk/news/rss.xml
/add  https://feeds.bbci.co.uk/news/rss.xml
```

Within seconds of a successful subscribe, the bot pushes one preview article. After that, new content arrives on the `FETCH_INTERVAL_MINUTES` cadence (60 min by default).

---

## 📋 Requirements

| Item | Requirement | Notes |
|---|---|---|
| **OS** | Linux (Debian 12 / Ubuntu 22+ / CentOS / etc.) | Docker path is distro-agnostic |
| **Python** | 3.11 / 3.12 / 3.13 | **3.14 not yet supported** (`lxml` has no wheel) |
| **RAM** | Minimum 256 MiB, recommended 512 MiB | 1 GiB+ for 100+ subscriptions |
| **Disk** | ~100 MiB baseline for SQLite | Grows with retention × subscription count |
| **Network** | Outbound HTTPS 443 only | Bot connects out to Discord / Telegram / RSS sources |
| **Docker** | 20.10+ with Compose v2 | Or use [systemd deployment](#advanced-deployment) |

Windows works for development (`make dev`), but Linux is recommended for production.

---

## ⚙️ Configuration

All settings go through `.env` or environment variables. Minimum:

```bash
# Either (or both) is fine
DISCORD_TOKEN=your_real_discord_token
TELEGRAM_TOKEN=your_real_telegram_token
```

### All settings

| Variable | Default | Description |
|---|---|---|
| **Platforms** | | |
| `DISCORD_TOKEN` | empty | Discord bot token; empty = Discord disabled |
| `TELEGRAM_TOKEN` | empty | Telegram bot token; empty = Telegram disabled |
| **Database** | | |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/newsflow.db` | SQLAlchemy async URL; swap with `postgresql+asyncpg://...` for Postgres |
| **Translation** | | |
| `TRANSLATION_ENABLED` | `false` | Master switch |
| `TRANSLATION_PROVIDER` | `deepl` | `deepl` / `openai` / `google` |
| `DEEPL_API_KEY` | empty | DeepL API key |
| `OPENAI_API_KEY` | empty | OpenAI (or compatible) key |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model name |
| `OPENAI_BASE_URL` | empty | Override for OpenAI-compatible endpoints (DeepSeek, Qwen, …) |
| `GOOGLE_CREDENTIALS_PATH` | empty | Path to Google Cloud service account JSON |
| `GOOGLE_PROJECT_ID` | empty | GCP project id |
| **Scheduling** | | |
| `FETCH_INTERVAL_MINUTES` | `60` | Fetch loop interval |
| `CLEANUP_INTERVAL_HOURS` | `24` | Cleanup loop interval |
| `ENTRY_RETENTION_DAYS` | `7` | How long to keep entries |
| **Cache** | | |
| `CACHE_BACKEND` | `memory` | `memory` (in-process LRU) or `redis` |
| `REDIS_URL` | empty | e.g. `redis://redis:6379/0` |
| `TRANSLATION_CACHE_TTL_DAYS` | `7` | Translation result cache TTL |
| **REST API** | | |
| `API_ENABLED` | `false` | Enables the FastAPI management server |
| `API_HOST` | `0.0.0.0` | Bind address |
| `API_PORT` | `8000` | Bind port |
| **Logging** | | |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOG_FORMAT` | `console` | `console` (human) or `json` (for Loki / ELK) |
| **Quotas (0 = unlimited)** | | |
| `MAX_FEEDS_PER_CHANNEL` | `0` | Max subscriptions per channel |
| `MAX_ENTRIES_PER_FEED` | `0` | Max retained entries per feed |

---

## 📱 Command Reference

### Discord slash commands

#### Feed management

| Command | Description |
|---|---|
| `/feed add <url>` | Subscribe; one preview article is sent within seconds |
| `/feed remove <url>` | Unsubscribe |
| `/feed pause <url>` | Pause delivery (don't delete; resume later) |
| `/feed resume <url>` | Resume a paused subscription |
| `/feed list [page]` | Paginated list (20 per page) |
| `/feed test <url>` | Check a URL without subscribing |
| `/feed status <url>` | Detailed status: health, last error, recent 5 articles |

#### Translation

| Command | Description |
|---|---|
| `/settings language <code>` | Channel-wide default target language |
| `/settings translate <on\|off>` | Channel-wide translate toggle |
| `/feed language <url> <code>` | Per-feed language (overrides channel default) |
| `/feed translate <url> <on\|off>` | Per-feed toggle (overrides channel default) |

#### OPML

| Command | Description |
|---|---|
| `/feed export` | Download this channel's subscriptions as OPML |
| `/feed import <file>` | Bulk-subscribe from an uploaded `.opml` / `.xml` file (max 1 MB) |

#### Other

| Command | Description |
|---|---|
| `/status` | Bot-wide status |

### Telegram commands

#### Feed management

| Command | Description |
|---|---|
| `/add <url>` | Subscribe |
| `/remove <url>` | Unsubscribe |
| `/pause <url>` | Pause |
| `/resume <url>` | Resume |
| `/list [page]` | Paginated list |
| `/test <url>` | Test URL |
| `/info <url>` | Feed detail (Telegram equivalent of Discord's `/feed status`) |

#### Translation

| Command | Description |
|---|---|
| `/language <code>` | Channel-wide language |
| `/translate <on\|off>` | Channel-wide toggle |
| `/setlang <url> <code>` | Per-feed language |
| `/settrans <url> <on\|off>` | Per-feed toggle |

#### OPML

| Command | Description |
|---|---|
| `/export` | Download OPML |
| `/import <url>` | Fetch and import from a hosted OPML URL |
| *upload `.opml` file* | Auto-imported when dropped into the chat — no command needed |

#### Other

| Command | Description |
|---|---|
| `/start`, `/help` | Help |
| `/status` | Bot status |

### Common language codes

`zh-CN` (Simplified Chinese) · `zh-TW` (Traditional Chinese) · `en` · `ja` · `ko` · `fr` · `de` · `es` · `ru` · `ar` · any BCP-47 tag your provider supports.

---

## 📋 OPML Import / Export

**Use cases**: migrate from Feedly / Reeder / NetNewsWire; back up subscription lists; move between channels or instances.

### Export

- **Discord**: `/feed export` → bot attaches an `.opml` file
- **Telegram**: `/export` → bot sends `.opml` as a document

### Import

- **Discord**: `/feed import file:<attach file>`
- **Telegram**: drop an `.opml` file into the chat (no command needed), or `/import <url>` to fetch a hosted file

### Curated starter list

`samples/curated-feeds.opml` in this repo contains 23 hand-picked sources (WSJ, FT, NYT, Bloomberg, Economist, Reuters, Atlantic, Foreign Affairs, Nautilus, Longreads, Cloudflare Blog, EFF, …).

**Usage**: download the file → Discord `/feed import` upload it, or drop it in your Telegram chat → all 23 feeds subscribed in one go.

> **Bulk imports don't spam previews** — rather than firing 20 preview articles at once, import uses a quiet flow; new content arrives on the next dispatch cycle like any normal subscription.

---

## 🔌 REST API (Optional)

Set `API_ENABLED=true` in `.env` to enable.

> ⚠️ **The API currently has no auth and CORS allows all origins.** Intended for local / intranet use; if exposing publicly, put nginx + Basic Auth or an API key middleware in front.

### Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness probe |
| `GET` | `/ready` | Readiness (includes DB connectivity) |
| `GET` | `/live` | K8s-style liveness |
| `GET` | `/api/feeds` | List all feeds |
| `POST` | `/api/feeds` | Add a feed |
| `GET` | `/api/feeds/{id}` | Feed detail |
| `DELETE` | `/api/feeds/{id}` | Delete a feed |
| `POST` | `/api/feeds/{id}/refresh` | Force a fetch |
| `POST` | `/api/feeds/test` | Test a URL |
| `GET` | `/api/stats` | Overall stats |
| `GET` | `/api/stats/feeds` | Per-feed stats |

When `LOG_LEVEL=DEBUG`, `/docs` (Swagger UI) is exposed.

---

## 🔧 Advanced Deployment

### Docker Compose profiles

```bash
# With Redis (multi-instance or if you want translation cache to survive restart)
docker compose -f docker/docker-compose.yml --profile with-redis up -d

# With Postgres (only needed past ~100k entries; SQLite is fine up to that)
docker compose -f docker/docker-compose.yml --profile with-postgres up -d

# Full stack
docker compose -f docker/docker-compose.yml --profile with-redis --profile with-postgres up -d
```

### systemd + venv (no Docker)

```bash
sudo apt install -y python3 python3-venv python3-pip libxml2-dev libxslt1-dev build-essential

cd /opt && sudo git clone https://github.com/Lynthar/NewsFlow-Bot.git
cd NewsFlow-Bot
sudo python3 -m venv .venv
sudo .venv/bin/pip install -e ".[all]"
sudo cp .env.example .env && sudo nano .env

sudo tee /etc/systemd/system/newsflow.service > /dev/null <<'EOF'
[Unit]
Description=NewsFlow Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=newsflow
WorkingDirectory=/opt/NewsFlow-Bot
EnvironmentFile=/opt/NewsFlow-Bot/.env
ExecStart=/opt/NewsFlow-Bot/.venv/bin/python -m newsflow.main
Restart=always
RestartSec=10
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/NewsFlow-Bot/data

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now newsflow
sudo journalctl -u newsflow -f
```

### Upgrading

```bash
cd ~/NewsFlow-Bot
git pull
docker compose -f docker/docker-compose.yml up -d --build
# alembic migrations run automatically on startup; no manual step
```

### Backup

```bash
# SQLite: single file
docker cp newsflow-bot:/app/data/newsflow.db ./backup-$(date +%F).db

# Postgres:
docker compose -f docker/docker-compose.yml exec postgres pg_dump -U newsflow > backup-$(date +%F).sql
```

---

## 🩺 Health Checks

The bot maintains 4 heartbeat files under `data/heartbeat/`, each owned by a specific task:

| File | Written by | Frequency |
|---|---|---|
| `dispatch` | dispatch loop | after every fetch cycle |
| `cleanup` | cleanup loop | after every cleanup iteration |
| `discord` | platform monitor | every 30 s while Discord is connected |
| `telegram` | platform monitor | every 30 s while Telegram is connected |

The Dockerfile `HEALTHCHECK` passes **only if every file is fresh within 120 minutes**. If any file goes stale, the container is marked `unhealthy`.

Check heartbeat state:

```bash
docker exec newsflow-bot ls -la /app/data/heartbeat/
```

Internal stats (if REST API is enabled):

```bash
curl http://localhost:8000/api/stats | jq
```

---

## 🐛 FAQ

<details>
<summary><b>Subscribed, but no new messages arriving</b></summary>

Expected behavior. The bot pushes one **preview** article within seconds of subscribing, then waits for the `FETCH_INTERVAL_MINUTES` cycle (60 min default) to check for updates. If the source hasn't published anything new, nothing gets sent.

Run `/feed status <url>` to inspect last successful fetch, error counts, etc.
</details>

<details>
<summary><b>Container keeps restarting, logs show <code>InvalidToken</code></b></summary>

Most likely your `.env` still has the placeholder (`your_discord_bot_token` or `your_telegram_bot_token`) instead of the real token, or there's a typo. Fix and restart.
</details>

<details>
<summary><b>A specific feed never works — network issue</b></summary>

Container DNS or outbound HTTPS may be broken. Test:

```bash
docker exec newsflow-bot python -c "import socket; print(socket.gethostbyname('example.com'))"
```

If that resolves, DNS is fine. Otherwise confirm `docker-compose.yml` includes:

```yaml
    dns:
      - 1.1.1.1
      - 8.8.8.8
```

Then `docker compose up -d --force-recreate newsflow`.

HTTPS timeouts on one specific host while others work usually mean the source or its CDN rate-limiting your VPS IP — try again later or switch to another source.
</details>

<details>
<summary><b>Translation isn't working</b></summary>

Check in order:
1. `.env` has `TRANSLATION_ENABLED=true`
2. The configured `TRANSLATION_PROVIDER`'s key is set (`DEEPL_API_KEY` / `OPENAI_API_KEY` / `GOOGLE_CREDENTIALS_PATH`)
3. The subscription itself has translation on: `/feed status <url>` → `Translation: On`
4. Restart the container after changing `.env`
5. `LOG_LEVEL=DEBUG` to see detailed provider calls
</details>

<details>
<summary><b>After upgrade, alembic reports "table feeds already exists"</b></summary>

Your DB was created by `init_db()` before alembic was wired in. The initial migration has an idempotent guard that should handle this automatically. If it still errors:

```bash
# Manually stamp the current schema as up-to-date
docker compose exec newsflow alembic stamp head
```
</details>

<details>
<summary><b>Reset all data</b></summary>

In SQLite mode, stop the container and delete the data volume:

```bash
docker compose -f docker/docker-compose.yml down
docker volume rm docker_newsflow-data
docker compose -f docker/docker-compose.yml up -d
```
</details>

<details>
<summary><b>Logs show <code>No adapter for platform: discord</code></b></summary>

Discord login isn't complete yet (or a transient disconnect). The dispatch loop skips this round; the next iteration recovers automatically. If it persists, check `DISCORD_TOKEN` and container networking.
</details>

---

## 🤝 Contributing

Want to fix a bug or add a feature? Read [DEVELOPMENT.md](DEVELOPMENT.md) — it covers the full architecture, layering rules, code style, extension points, and daily workflow.

### Quick dev loop

```bash
# Set up a venv (uv is 10× faster than pip if you have it)
uv venv --python 3.13
uv pip install -e ".[all]"
uv pip install pytest pytest-asyncio

# Run tests
make test         # or: .venv/bin/python -m pytest tests/ -v

# Type checks + lint
make typecheck
make lint
make format
```

### Project layout

```
NewsFlow-Bot/
├── src/newsflow/
│   ├── main.py                 # entry point
│   ├── config.py               # pydantic-settings config
│   ├── core/                   # stateless primitives
│   │   ├── feed_fetcher.py     # HTTP + feedparser
│   │   ├── content_processor.py
│   │   ├── url_security.py     # SSRF guard
│   │   ├── opml.py             # OPML parse/build
│   │   └── timeutil.py         # relative time formatting
│   ├── models/                 # SQLAlchemy ORM
│   ├── repositories/           # DB queries
│   ├── services/               # business logic
│   │   ├── dispatcher.py       # ★ central dispatch loop
│   │   ├── feed_service.py
│   │   ├── subscription_service.py
│   │   ├── cache.py
│   │   └── translation/        # provider + factory
│   ├── adapters/               # platform I/O
│   │   ├── discord/bot.py
│   │   └── telegram/bot.py
│   └── api/                    # optional FastAPI routes
├── alembic/                    # DB migrations
├── docker/                     # Dockerfile + compose
├── samples/                    # shipped OPML etc.
├── tests/                      # unit tests
├── pyproject.toml              # dependency authority
├── Makefile                    # common commands
├── DEVELOPMENT.md              # developer docs
└── README.md                   # the Chinese README
```

---

## 📄 License

[MIT](LICENSE)

---

<div align="center">

**Built for the self-hosting community ❤️**

If this helps you, a ⭐ and share is appreciated.

[Report a bug](https://github.com/Lynthar/NewsFlow-Bot/issues) · [Feature request](https://github.com/Lynthar/NewsFlow-Bot/issues) · [Pull request](https://github.com/Lynthar/NewsFlow-Bot/pulls)

</div>
