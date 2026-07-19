# NewsFlow Bot

<div align="center">

**Self-hosted feed delivery for Discord & Telegram — built-in translation, AI digests, beyond-RSS sources, message templates & mentions**

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

A **feed-delivery backend you run on your own server**. Hand it a Discord or Telegram bot token, run `/feed add <url>` in a channel, and new articles arrive as they're published — translated into your language, filtered by keyword, laid out with your own message template, and rolled up into AI daily/weekly digests if you want them. Sources aren't limited to RSS: the same pipeline ingests JSON APIs, IMAP newsletters, and inbound webhooks.

**Design principles**: self-hosted first · zero-config start · progressive complexity · swappable components.

---

## 🧭 Why NewsFlow?

Most feed bots do one thing: new post → channel. NewsFlow keeps that part boring and reliable, then adds the layer the established bots don't have:

- 🌍 **The feed is in one language, your channel reads another.** Built-in translation (DeepL / OpenAI / Google) with per-feed target languages — and bilingual layouts via message templates.
- 📰 **It can summarize, not just relay.** Optional AI daily/weekly digests turn a firehose channel into one readable briefing.
- 🧩 **It ingests more than RSS.** JSON APIs, IMAP newsletters, and inbound webhook pushes flow through the same filter → translate → template → digest pipeline.

|  | NewsFlow | [MonitoRSS](https://github.com/synzen/MonitoRSS) | [RSStT](https://github.com/Rongronggg9/RSS-to-Telegram-Bot) | [flowerss](https://github.com/indes/flowerss-bot) |
|---|:---:|:---:|:---:|:---:|
| Built-in translation | ✅ | ❌ | ❌ | ❌ |
| AI daily / weekly digest | ✅ | ❌ | ❌ | ❌ |
| Beyond-RSS sources (JSON API / newsletter / webhook-in) | ✅ | ❌ | ❌ | ❌ |
| Message templates (`{title}` placeholders) | ✅ | ✅ | toggles only | ❌ |
| Per-feed role / user mentions | ✅ | ✅ | — | — |
| Platforms in one process | Discord + Telegram + webhook | Discord | Telegram | Telegram |
| Self-hosted | ✅ | ✅ (hosted option too) | ✅ | ✅ |

<sub>Feature comparison as of 2026-07, from each project's public docs — corrections welcome. All three are solid projects; if classic RSS-to-channel on a single platform is all you need, they serve that well.</sub>

---

## ✨ Features

| Feature | Notes |
|---|---|
| 🌍 **Auto-translation** | DeepL / OpenAI-compatible / Google, per-feed target language, two-tier cache (DB + memory/Redis), same-language short-circuit — no translation API calls wasted |
| 📰 **AI digest** | Optional LLM daily / weekly briefings per channel, scheduled in your timezone (or `/digest now`) |
| 🧩 **Non-RSS sources** | Declarative `sources.yaml`: poll any **JSON API** (JSONPath) or **IMAP mailbox / newsletter**, or receive **inbound webhook** pushes — all through the same filter/translate/digest/deliver pipeline |
| 🎨 **Message templates** | Per-feed `{title}` `{summary}` `{translated_title}` … placeholder layouts — bilingual output, compact mode, your own footer (`/feed template` · `/template`) |
| 🔔 **Mentions & topics** | Per-feed Discord role/user pings that actually notify (ping-safe baseline — feed content can never `@everyone`); Telegram forum-topic routing (`/feed mention` · `/settopic`) |
| 📡 **RSS / Atom / JSON Feed** | `feedparser` + `aiohttp`, conditional requests, concurrent fetch, SSRF guard, size cap; paste a site homepage to auto-discover its feed, plus `gh:` / `gnews:` / `yt:` … shortcuts |
| 🌐 **Multi-platform** | Discord slash commands + Telegram prefix commands in one process |
| 🔌 **Webhook (outbound)** | Push to Slack / ntfy / Feishu / Work-WeChat / n8n / Zapier / any HTTP endpoint via declarative `webhooks.yaml`; HMAC-SHA256 signing supported |
| 📥 **Inbound ingest API** | `POST /api/ingest/{source}` (API-key auth) lets n8n / CI / scripts push entries into NewsFlow |
| 🎯 **Keyword filter** | Per-subscription include/exclude keywords or `/regex/`; filtered entries skip translate |
| 📋 **OPML import/export** | Migrate from Feedly / Reeder; repo ships a curated 22-feed OPML |
| 🔁 **Exponential backoff** | Dying sources auto-stretch retries; 10 fails → auto-disable + notify |
| ⏸ **Pause / resume** | Temporarily stop without deleting the subscription |
| 🔇 **Silent (digest-only)** | Skip instant push but keep entries flowing into the digest — for channels that only want the rollup |
| 🩺 **Health visible** | `/feed status` shows errors, backoff window, recent articles; container HEALTHCHECK wired in |
| 🐳 **Docker ready** | One `docker compose up`; alembic auto-migrates on start |

---

## 🏗️ Architecture at a glance

```
                 Single asyncio process
   ┌──────────────────────────────────────────────┐
   │  Discord / Telegram / Webhook adapter        │
   │  Dispatch loop   ← fetch→translate→send      │
   │  Cleanup loop    ← prune old entries         │
   │  Digest loop     ← AI daily/weekly           │
   │  Platform monitor ← heartbeat                │
   └──────────────────────────────────────────────┘
                      │
                      ▼
          SQLite file / Postgres (optional)
```

Full layered breakdown and module responsibilities in [GUIDE.md §10](GUIDE.md#十架构总览).

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

# 3. Run (pulls the prebuilt multi-arch image from GHCR — no local build)
docker compose -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml logs -f newsflow
```

You're live when you see `Discord bot logged in as ...` or `Telegram bot started successfully`.

> Prefer to build the image yourself instead of pulling? `make docker-up-local` (or set `NEWSFLOW_IMAGE=newsflow-bot:latest` for the `up` command).

**Getting a token**: Discord via [Developer Portal](https://discord.com/developers/applications); Telegram via [@BotFather](https://t.me/BotFather).

---

## 📋 Requirements

| Item | Requirement |
|---|---|
| OS | Linux (Debian 12 / Ubuntu 22+ / CentOS etc.) |
| Python | 3.11 / 3.12 / 3.13 (3.14 not yet — `lxml` has no wheel) |
| Memory | 256 MiB minimum, 512 MiB recommended |
| Network | Outbound HTTPS 443 only |
| Docker | 20.10+ with Compose v2 (or [systemd deployment](GUIDE.md#七高级部署与运维)) |

---

## 📱 Command cheat sheet

**Discord**:

```
/feed add <url>          subscribe (one preview pushed within seconds)
/feed remove <url>       unsubscribe
/feed list               show channel subscriptions
/feed template <url> ... custom message layout ({title}, {url}, …)
/feed mention <url> ...  ping a role / user on new entries
/feed filter-set ...     keyword filter
/digest enable ...       turn on daily / weekly digest
```

**Telegram**:

```
/add <url>               subscribe
/list                    show subscriptions
/template <url> ...      custom message layout
/settopic <url>          deliver a feed to the current forum topic
/filter <url> ...        keyword filter
/digest enable daily 9   turn on daily digest
```

Full reference (30+ commands across both platforms): [GUIDE.md §1](GUIDE.md#一完整命令参考).

**Webhook delivery** is output-only (no bot commands) — under Docker, `cp samples/webhooks.example.yaml config/webhooks.yaml` (the `config/` dir is mounted into the container), edit, and restart; see [GUIDE.md §4](GUIDE.md#四webhook-推送) or the annotated [`samples/webhooks.example.yaml`](samples/webhooks.example.yaml).

**Non-RSS sources** (JSON API, IMAP newsletters, inbound webhook push) are declared the same way in `config/sources.yaml` — see [GUIDE.md §4B](GUIDE.md#四b非-rss-信息源sourcesyaml) or [`samples/sources.example.yaml`](samples/sources.example.yaml). Extras are already in the Docker image; for a bare-metal run use `make install-all`.

> Running bare-metal (not Docker)? These files default to `./data/` instead — override with `WEBHOOKS_CONFIG_PATH` / `SOURCES_CONFIG_PATH`.

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
API_ENABLED=true                         # REST API + inbound /api/ingest
API_KEY=long-random-string               # required for API writes / inbound push
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

Your `.env` still has the placeholder or a typo. Fix and **`docker compose -f docker/docker-compose.yml up -d newsflow`** — `restart` alone does **not** re-read `.env`; compose only reads env_file at `up` time and caches it into the container config. For any `.env` change you need `up -d` (which will recreate the container when values changed). More on this in [GUIDE.md §7.6](GUIDE.md#76-部署后在线改配置env-的正确姿势).
</details>

<details>
<summary><b>How do I customize the AI digest style / translation tone?</b></summary>

Set `TRANSLATION_SYSTEM_PROMPT=` or `DIGEST_SYSTEM_PROMPT=` in `.env` to override the default prompts. Details: [GUIDE.md §3.3](GUIDE.md#33-自定义-ai-提示词).
</details>

More FAQ (DNS / translation not working / data reset / upgrade errors / …): [GUIDE.md §15](GUIDE.md#十五常见陷阱--faq).

---

## 🤝 Contributing

Architecture, layering rules, code style, extension points (adding a new platform / translation provider / API endpoint) are all in **[GUIDE.md §8-17](GUIDE.md#开发--架构)**.

Fast dev loop:

```bash
uv venv --python 3.13
uv pip install -e ".[all]"
uv pip install pytest pytest-asyncio
make test      # 650 tests
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
