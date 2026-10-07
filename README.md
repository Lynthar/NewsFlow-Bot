# NewsFlow-Bot

[![license](https://img.shields.io/github/license/Lynthar/NewsFlow-Bot)](LICENSE)
[![tests](https://img.shields.io/github/actions/workflow/status/Lynthar/NewsFlow-Bot/test.yml?branch=main&label=tests)](https://github.com/Lynthar/NewsFlow-Bot/actions/workflows/test.yml)
[![image](https://img.shields.io/github/actions/workflow/status/Lynthar/NewsFlow-Bot/docker-publish.yml?branch=main&label=image)](https://github.com/Lynthar/NewsFlow-Bot/actions/workflows/docker-publish.yml)
[![release](https://img.shields.io/github/v/release/Lynthar/NewsFlow-Bot)](https://github.com/Lynthar/NewsFlow-Bot/releases)

Self-hosted feed delivery for Discord, Telegram and webhooks — RSS, JSON APIs and IMAP, with translation and AI digests

English | [简体中文](README.zh-CN.md)

This tool takes in RSS, JSON API and IMAP sources at the same time, filters and
translates them the way you configure, and pushes the result to Discord, Telegram
or webhooks (maybe more later). My goal was to keep it as simple as possible, so
it's just one container and one SQLite file.

Most administration is done from inside Discord or Telegram with the built-in
commands. There's no web UI — I don't think it's complex enough to need one — and
a REST API is there if you'd rather script it.

| In | Out |
|---|---|
| RSS and Atom feeds | Discord bot |
| JSON APIs, addressed with JSONPath | Telegram bot |
| IMAP mailboxes, for newsletters that never got a feed | Outbound webhooks in seven wire formats — generic, Slack, ntfy, Lark, WeCom, Discord, Matrix — with optional HMAC-SHA256 signing |
| An inbound webhook endpoint anything can POST to | |

Between the in and the out: keyword and regex filters per subscription,
translation via DeepL, Google, or any OpenAI-compatible endpoint (including a
local model), daily and weekly AI digests, per-channel language and display
settings, OPML import and export, and automatic back-off that disables a feed
after ten consecutive failures instead of retrying indefinitely. The test suite
runs on Python 3.11 and 3.13 in CI.

## Install

Docker is the deployment method this was designed around. You need one bot token
to start — either Discord or Telegram; a webhook-only deployment can skip both.

```bash
git clone https://github.com/Lynthar/NewsFlow-Bot.git
cd NewsFlow-Bot
cp .env.example .env
chmod 600 .env
```

Put a `DISCORD_TOKEN` or `TELEGRAM_TOKEN` in `.env`, then:

```bash
docker compose -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml logs -f newsflow
```

That pulls `ghcr.io/lynthar/newsflow-bot`, which ships with every optional extra
already installed and runs database migrations on startup. Redis and PostgreSQL
are available as compose profiles if you want them.

Running from source needs Python 3.11 to 3.13 — 3.14 doesn't work yet, `lxml`
has no wheel for it:

```bash
uv venv --python 3.13
uv pip install -e ".[all]"
```

Before deploying, this checks your `.env` and YAML config without touching the
network or the database:

```bash
python -m newsflow.checkconfig
```

## Usage

Subscribe from any channel the bot can see:

```
/feed add https://news.ycombinator.com/rss
```

You get a preview within seconds, then updates on the polling interval.

```
/feed list                 # what this channel is subscribed to
/feed status <url>         # errors, back-off window, recent articles
/feed filter-set <url> …   # keyword or /regex/ filtering
/digest enable …           # turn on daily or weekly summaries
```

Discord has `/feed`, `/settings` and `/digest` command groups, which require
Manage Server and work only in server channels, not in DMs, plus an open
`/status`. Telegram has the same surface as flat commands (`/add`, `/remove`,
`/filter`, `/digest`, and so on).

## Configuration

Environment variables or `.env` for the process, plus two YAML files:
`webhooks.yaml` declares outbound destinations, `sources.yaml` declares non-RSS
sources.

| Variable | Default | Notes |
|---|---|---|
| `DISCORD_TOKEN` / `TELEGRAM_TOKEN` | — | At least one, unless you're webhook-only |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/newsflow.db` | Swap in `postgresql+asyncpg://…` for Postgres |
| `FETCH_INTERVAL_MINUTES` | `60` | Polling interval |
| `TRANSLATION_ENABLED` / `TRANSLATION_PROVIDER` | `false` / `deepl` | `google`, `deepl` or `openai` |
| `API_ENABLED` / `API_KEY` | `false` / — | REST API and inbound `/api/ingest` |
| `INGEST_API_KEY` | — | A key that opens `/api/ingest` alone, for the systems that push entries in |
| `OPENAI_BASE_URL` | — | Point translation or digests at a local model |

Editing `.env` needs `up -d` to take effect; `restart` won't re-read it.

## Limitations

- **Matrix works through a webhook, not natively.** There's a `matrix` wire
  format aimed at matrix-hookshot, but no Matrix adapter and no Matrix-side
  commands.
- **Microsoft Teams isn't supported.** The old O365 connector was retired in
  2026-05, and the replacement path needs a tenant to test against.
- **Single instance only.** Redis is a translation cache, not a coordination
  layer; running two copies against one database is out of scope by design.
- **Webhooks are output only.** They can't manage subscriptions; changing them
  means editing `webhooks.yaml` and restarting.
- **Configuration can shift between minor versions** while this is 0.x. What is
  and isn't inside the compatibility promise is spelled out in the compatibility
  document.

## Documentation

- [User guide](docs/user-guide.md) — every command, every setting, FAQ. Written
  in Chinese.
- [Compatibility](docs/compatibility.md) — what 0.x guarantees and what it
  doesn't.
- [Changelog](CHANGELOG.md)

## Security

The `.env` file holds bot tokens and your API key; remember to `chmod 600` it.

The REST API binds to `127.0.0.1` in the shipped compose file. If you set
`API_ENABLED=true` and move that binding, note that read endpoints are
unauthenticated unless `API_KEY` is also set — and on a VPS there is no "local
network" boundary to rely on.

Feed URLs submitted by users are checked against internal address ranges, and
every redirect hop is re-checked. That isn't a substitute for egress filtering:
DNS rebinding isn't covered, and hostnames like `localhost` or cloud metadata
endpoints are deliberately allowed through.

## License

GNU Affero General Public License v3.0 only — see [LICENSE](LICENSE).
Copyright (c) 2026 Lynthar.

### Third-party licenses

This project uses **[python-telegram-bot](https://python-telegram-bot.org/)**
under the **LGPL v3** — the library is dual-licensed GPL v3 / LGPL v3 at the
recipient's option. It is used unmodified, and you may replace it with an
interface-compatible build of your own.

Both license texts travel with the library itself, in
`python_telegram_bot-*.dist-info/` (`LICENSE.lesser` for the LGPL, `LICENSE`
for the GPL) — including inside the `ghcr.io/lynthar/newsflow-bot` image, under
`/opt/venv`.
