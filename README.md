# NewsFlow Bot

<div align="center">

**Self-hosted RSS push backend — subscriptions, translation, keyword filtering, AI digests for Discord / Telegram bots**

[![Python](https://img.shields.io/badge/Python-3.11%20–%203.13-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![discord.py](https://img.shields.io/badge/discord.py-2.3+-7289da.svg)](https://github.com/Rapptz/discord.py)
[![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-20.7+-0088cc.svg)](https://python-telegram-bot.org/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ed.svg)](https://www.docker.com/)

English | [简体中文](README_CN.md)

</div>

> 📖 **This is the quick-start.** Full command reference, configuration, advanced deployment, architecture, extension guide — all in **[GUIDE.md](GUIDE.md)** (currently in Chinese; English translation welcome as a contribution).

---

## 🎯 What is this?

An **RSS push backend you run on your own server**. Hand it a Discord or Telegram bot token, run `/feed add <url>` in a channel, and the bot pushes new articles there as they appear — optionally translating, filtering by keyword, and rolling them up into periodic AI-generated digests.

**Design principles**: self-hosted first · zero-config start · progressive complexity · swappable components.

---

## ✨ Features

| Feature | Notes |
|---|---|
| 📡 **RSS fetching** | `feedparser` + `aiohttp`, conditional requests, concurrent fetch, SSRF guard, size cap |
| 🌐 **Multi-platform** | Discord slash commands + Telegram prefix commands in one process |
| 🌍 **Auto-translation** | DeepL / OpenAI / Google, two-tier cache (DB + memory/Redis) |
| 🎯 **Keyword filter** | Per-subscription include/exclude; filtered entries skip translate |
| 📰 **AI digest** | Optional LLM-generated daily / weekly briefings |
| 📋 **OPML import/export** | Migrate from Feedly / Reeder; repo ships a curated 23-feed OPML |
| 🔁 **Exponential backoff** | Dying sources auto-stretch retries; 10 fails → auto-disable + notify |
| ⏸ **Pause / resume** | Temporarily stop without deleting the subscription |
| 🩺 **Health visible** | `/feed status` shows errors, backoff window, recent articles; container HEALTHCHECK wired in |
| 🐳 **Docker ready** | One `docker compose up`; alembic auto-migrates on start |

---

## 🏗️ Architecture at a glance

```
             Single asyncio process
   ┌─────────────────────────────────────────┐
   │  Discord / Telegram adapter              │
   │  Dispatch loop   ← fetch→translate→send  │
   │  Cleanup loop    ← prune old entries     │
   │  Digest loop     ← AI daily/weekly       │
   │  Platform monitor ← heartbeat             │
   └─────────────────────────────────────────┘
                      │
                      ▼
          SQLite file / Postgres (optional)
```

Full layered breakdown and module responsibilities in [GUIDE.md §9](GUIDE.md#九架构总览).

---

## 🚀 Quick Start (Docker)

For **Debian 12 / Ubuntu 22+** — swap the Docker install commands for your distro if needed:

```bash
# 1. Install Docker (skip if you already have it)
sudo apt update && sudo apt install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg | \
    sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/debian $(lsb_release -cs) stable" | \
    sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update && sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# 2. Clone, configure
git clone https://github.com/Lynthar/NewsFlow-Bot.git
cd NewsFlow-Bot
cp .env.example .env
nano .env     # fill in at least one DISCORD_TOKEN or TELEGRAM_TOKEN

# 3. Run
docker compose -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml logs -f newsflow
```

You're live when you see `Discord bot logged in as ...` or `Telegram bot started successfully`.

**Getting a token**: Discord via [Developer Portal](https://discord.com/developers/applications); Telegram via [@BotFather](https://t.me/BotFather).

---

## 📋 Requirements

| Item | Requirement |
|---|---|
| OS | Linux (Debian 12 / Ubuntu 22+ / CentOS etc.) |
| Python | 3.11 / 3.12 / 3.13 (3.14 not yet — `lxml` has no wheel) |
| Memory | 256 MiB minimum, 512 MiB recommended |
| Network | Outbound HTTPS 443 only |
| Docker | 20.10+ with Compose v2 (or [systemd deployment](GUIDE.md#六高级部署与运维)) |

---

## 📱 Command cheat sheet

**Discord**:

```
/feed add <url>          subscribe (one preview pushed within seconds)
/feed remove <url>       unsubscribe
/feed list               show channel subscriptions
/feed filter-set ...     keyword filter
/digest enable ...       turn on daily / weekly digest
```

**Telegram**:

```
/add <url>               subscribe
/list                    show subscriptions
/filter <url> ...        keyword filter
/digest enable daily 9   turn on daily digest
```

Full reference (30+ commands across both platforms): [GUIDE.md §1](GUIDE.md#一完整命令参考).

---

## ⚙️ Key configuration

Minimum `.env`:

```bash
DISCORD_TOKEN=your_real_token
# or
TELEGRAM_TOKEN=your_real_token
```

Common extras:

```bash
FETCH_INTERVAL_MINUTES=30                # How often to poll feeds
TRANSLATION_ENABLED=true                 # Turn on auto-translation
TRANSLATION_PROVIDER=openai              # or deepl / google
OPENAI_API_KEY=sk-xxx
OPENAI_BASE_URL=https://api.deepseek.com # Any OpenAI-compatible endpoint
DIGEST_MODEL=gpt-5.4-mini                # LLM for digest generation
```

Full 30+ variables: [GUIDE.md §2](GUIDE.md#二完整配置项).

---

## 🐛 FAQ

<details>
<summary><b>Subscribed but not receiving messages</b></summary>

Expected. A single preview article is pushed within seconds of subscribing; after that, new content arrives at the `FETCH_INTERVAL_MINUTES` cadence (60 min by default). Source hasn't published anything new → no push. Run `/feed status <url>` to inspect.
</details>

<details>
<summary><b>Container keeps restarting, logs show <code>InvalidToken</code></b></summary>

Your `.env` still has the placeholder or a typo. Fix and `docker compose restart newsflow`.
</details>

<details>
<summary><b>How do I customize the AI digest style / translation tone?</b></summary>

Set `TRANSLATION_SYSTEM_PROMPT=` or `DIGEST_SYSTEM_PROMPT=` in `.env` to override the default prompts. Details: [GUIDE.md §3.3](GUIDE.md#33-自定义-ai-提示词).
</details>

More FAQ (DNS / translation not working / data reset / upgrade errors / …): [GUIDE.md §14](GUIDE.md#十四常见陷阱--faq).

---

## 🤝 Contributing

Architecture, layering rules, code style, extension points (adding a new platform / translation provider / API endpoint) are all in **[GUIDE.md §7-16](GUIDE.md#开发--架构)**.

Fast dev loop:

```bash
uv venv --python 3.13
uv pip install -e ".[all]"
uv pip install pytest pytest-asyncio
make test      # 134 tests
make lint
```

---

## 📄 License

[MIT](LICENSE)

---

<div align="center">

**Built for the self-hosting community ❤️**

If it's useful, a ⭐ and share is appreciated.

[Report a bug](https://github.com/Lynthar/NewsFlow-Bot/issues) · [Feature request](https://github.com/Lynthar/NewsFlow-Bot/issues) · [Pull request](https://github.com/Lynthar/NewsFlow-Bot/pulls)

</div>
