# NewsFlow Bot

<div align="center">

**Self-hosted RSS to Discord/Telegram Bot with Translation Support**

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Discord.py](https://img.shields.io/badge/discord.py-2.3+-7289da.svg)](https://github.com/Rapptz/discord.py)
[![Telegram](https://img.shields.io/badge/python--telegram--bot-20.7+-0088cc.svg)](https://python-telegram-bot.org/)

English | [简体中文](README.md)

</div>

---

## ✨ Features

- 🌐 **Multi-Platform Support** - Push RSS updates to Discord and Telegram simultaneously
- 🏠 **Self-Hosted First** - Run on your own VPS with full data control
- 🌍 **Automatic Translation** - Support for DeepL, OpenAI, and Google translation services
- ⚡ **Smart Caching** - Conditional requests + translation caching to save bandwidth and API calls
- 👥 **Channel Isolation** - Each channel manages its own subscriptions independently
- 🔌 **REST API** - Optional management interface for external integrations
- 🐳 **Docker Ready** - One-command deployment, ready to use

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        NewsFlow Core Engine                      │
│                                                                   │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐   │
│  │ RSS Fetcher  │─▶│   Content    │─▶│ Translation Service  │   │
│  │ (feedparser) │  │  Processor   │  │ (DeepL/OpenAI/Google)│   │
│  └──────────────┘  └──────────────┘  └──────────────────────┘   │
│          │                                      │                 │
│          ▼                                      ▼                 │
│  ┌──────────────┐                    ┌──────────────────────┐   │
│  │   Database   │◀──────────────────▶│     Dispatcher       │   │
│  │   (SQLite)   │                    │   (Message Router)   │   │
│  └──────────────┘                    └──────────────────────┘   │
│                                                 │                 │
└─────────────────────────────────────────────────┼─────────────────┘
                                                  │
                    ┌─────────────────────────────┼─────────────────────────────┐
                    │                             │                             │
                    ▼                             ▼                             ▼
           ┌──────────────┐             ┌──────────────┐             ┌──────────────┐
           │   Discord    │             │   Telegram   │             │   REST API   │
           │   Adapter    │             │   Adapter    │             │  (Optional)  │
           └──────────────┘             └──────────────┘             └──────────────┘
```

---

## 🛠️ Tech Stack

| Component | Technology | Purpose |
|-----------|------------|---------|
| **Runtime** | Python 3.11+ | Native async/await support |
| **Discord** | discord.py 2.3+ | Slash Commands |
| **Telegram** | python-telegram-bot 20.7+ | Bot API wrapper |
| **Database** | SQLAlchemy 2.0 + SQLite | Zero-config, single file storage |
| **RSS** | feedparser + aiohttp | Async fetching and parsing |
| **Translation** | DeepL / OpenAI / Google | Multi-provider support |
| **API** | FastAPI + Uvicorn | Optional REST interface |
| **Cache** | In-memory LRU / Redis | Translation result caching |

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11 or higher
- Discord Bot Token and/or Telegram Bot Token
- (Optional) Translation API key

### Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/NewsFlow-Bot.git
cd NewsFlow-Bot

# Install dependencies
pip install -e .

# Or install with all features
pip install -e ".[all]"
```

### Configuration

Create a `.env` file in the project root:

```bash
# ===== Required (at least one) =====
DISCORD_TOKEN=your_discord_bot_token
TELEGRAM_TOKEN=your_telegram_bot_token

# ===== Translation (Optional) =====
TRANSLATION_ENABLED=true
TRANSLATION_PROVIDER=deepl  # Options: deepl, openai, google
DEEPL_API_KEY=your_deepl_api_key

# ===== Scheduling =====
FETCH_INTERVAL_MINUTES=60

# ===== REST API (Optional) =====
API_ENABLED=false
API_PORT=8000
```

### Run

```bash
python -m newsflow.main
```

---

## 🐳 Docker Deployment

### Using Docker Compose (Recommended)

```bash
# Create and edit .env file first

# Start services
docker-compose -f docker/docker-compose.yml up -d

# View logs
docker-compose -f docker/docker-compose.yml logs -f
```

---

## 📱 Bot Commands

### Discord (Slash Commands)

| Command | Description |
|---------|-------------|
| `/feed add <url>` | Subscribe to an RSS feed |
| `/feed remove <url>` | Unsubscribe from a feed |
| `/feed list` | List subscribed feeds |
| `/feed test <url>` | Test if an RSS feed is valid |
| `/settings language <code>` | Set translation language |
| `/settings translate <on/off>` | Enable/disable translation |
| `/status` | Show bot status |

### Telegram

| Command | Description |
|---------|-------------|
| `/add <url>` | Subscribe to an RSS feed |
| `/remove <url>` | Unsubscribe from a feed |
| `/list` | List subscribed feeds |
| `/test <url>` | Test if an RSS feed is valid |
| `/language <code>` | Set translation language |
| `/translate <on/off>` | Enable/disable translation |
| `/status` | Show bot status |

---

## 🔌 REST API Endpoints

When `API_ENABLED=true`:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Service health status |
| `/ready` | GET | Readiness check |
| `/api/feeds` | GET | List all feeds |
| `/api/feeds` | POST | Add a new feed |
| `/api/feeds/{id}` | DELETE | Delete a feed |
| `/api/stats` | GET | Overall statistics |

---

## 📄 License

[MIT License](LICENSE)

---

<div align="center">

**Made with ❤️ for the self-hosted community**

</div>
