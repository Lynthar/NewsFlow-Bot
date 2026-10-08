# Changelog

Notable changes to NewsFlow-Bot. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and version numbers
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**The project is still `0.x`**, so the configuration surface can still change
between minor releases. [docs/compatibility.md](docs/compatibility.md) states
what is covered, what never will be, and what 1.0 is going to freeze — read it
before you pin a version.

## [Unreleased]

### Before you upgrade

- **The default models are now `gpt-6-luna` (translation) and `gpt-6.1-sol`
  (digests).** Both default to reasoning, where OpenAI accepts only the default
  temperature, so requests now carry `reasoning_effort`: `none` for translation
  and `low` for digests, set by the new `OPENAI_REASONING_EFFORT` and
  `DIGEST_REASONING_EFFORT`. Any effort other than `none` stops sending
  `temperature` and raises the output cap to leave room for the reasoning.
  A value that is not an OpenAI effort stops startup.
- **Endpoints that reject `reasoning_effort` need both keys set empty.** This
  covers most local servers and older models behind `OPENAI_BASE_URL`; without
  it every translation is delivered untranslated and no digest is generated.
  If you set `OPENAI_MODEL` or `DIGEST_MODEL` yourself, check that the model
  accepts the effort it is now sent: `gpt-6.1-sol` and `gpt-6-astra` refuse
  `none`.

## [0.9.9] - 2026-10-08

The version moves only in its last field, but this release changes behaviour.
If you follow the `:0.9` image or pin `~=0.9.5`, read the next section first.

### Before you upgrade

- **Configuration mistakes now stop startup.** The bot refuses to start when
  `SENT_ENTRY_RETENTION_DAYS` does not outlive `ENTRY_RETENTION_DAYS`, when a
  `webhooks.yaml` URL is not http(s), when a `sources.yaml` webhook subscriber
  names a destination `webhooks.yaml` does not declare (or there is no
  `webhooks.yaml` at all), when a `language` in either file is not a language
  code, or when a YAML value is wider than its database column. Run
  `make checkconfig` (or `python -m newsflow.checkconfig`) before restarting.
- **`webhooks.yaml` destinations default to `translate: false`.** A destination
  that never set `translate` stops translating at the first sync after the
  upgrade; add `translate: true` to keep it.
- **The Docker health check requires the `dispatch` heartbeat**, which is now
  written before the first round. If you override the health check, follow
  section 7.5 of the user guide.
- **Subscriptions declared in `webhooks.yaml` or `sources.yaml` are changed
  only in that file.** Pausing, removing, or setting language, translation or
  silent mode on one from a chat command, `/manage` or the REST API is refused
  with a pointer to the file.
- **Discord management commands are server-only** and no longer appear in
  direct messages with the bot.
- **`/digest now` is a preview.** It no longer pins, pings `@here`, or counts
  as the scheduled delivery.
- With the `api` extra, uvicorn 0.29 or newer is required. Older versions keep
  SIGTERM for themselves, so the bot never shut down while the API was on.
- Two database migrations run automatically at startup.

### Security

- Feed bodies reach feedparser as a byte stream. Given a string, feedparser
  fetches it as a URL or opens it as a file, bypassing every check the fetcher
  applies.
- IP addresses written in non-canonical forms no longer pass the URL check.
- URL userinfo, SQL bind parameters (which carried webhook secrets and auth
  headers) and YAML error snippets are kept out of logs and error text.
  `DB_ECHO` is the explicit opt-in to seeing bind parameters.
- `webhooks.yaml` `url`, `secret` and `headers` accept `${VAR}`, stored as the
  reference and resolved at send time, so credentials stay out of the
  database and its backups.
- `INGEST_API_KEY` opens `/api/ingest` alone. A key handed to an external
  system no longer grants management access; without it, `API_KEY` still
  works there.
- Slack, WeCom and Lark payloads escape feed text. Lark digests and notices
  are posted as rich text, where feed or model text can no longer mention
  everyone. Discord embeds drop non-http(s) image URLs, and Telegram links
  only http(s) entry URLs, since a `tg://` link can mention a user.
- Locked dependencies with published advisories are updated: anyio,
  multidict, PyJWT, soupsieve and urllib3.

### Fixed

- **Duplicate and lost deliveries.** Each delivery is recorded as soon as the
  platform accepts it, and no database write stays open across a network
  call. A crash now resends at most one entry, and a slow send no longer
  holds SQLite's lock against webhook and digest work. Two digest runs for
  one channel can no longer both send.
- SQLite migrations run with foreign keys off. A table rebuild could
  otherwise cascade and empty the delivery history.
- An entry a platform keeps refusing no longer blocks its subscription. The
  bot first retries it as a bare title and link, then gives it up after three
  failed rounds, but only once a notice to the same channel goes through.
  Webhook 400, 413 and 422 responses no longer trip the destination's breaker.
- Filtered and silenced entries no longer use up a subscription's per-round
  send budget, which starved keyword-filtered subscriptions on busy feeds.
- **Weekly digests lost articles processed late.** Cleanup went by when an
  entry was stored, while weekly digests select by when it was processed, so
  an article processed days after it arrived could be deleted before its
  first weekly digest. Cleanup now keeps anything processed within the
  weekly window.
- Scheduled digests no longer drift off their hour after an off-schedule
  delivery, and a failed delivery record is retried without resending.
- A subscription declared in either YAML file no longer receives entries
  published before it existed, and a `webhooks.yaml` source whose first fetch
  fails still gets its subscription.
- A delivery record ages from the last full fetch that still listed its entry,
  so an undated entry that a feed keeps listing is not sent again once the
  record expires. Over-long guids no longer collide.
- One malformed entry rolls back only its own feed. JSON Feed fields of the
  wrong type are coerced, and oversized JSON Feed dates no longer fail the
  feed. Publish dates with a UTC offset keep their instant. Feeds are parsed
  off the event loop.
- Undated entries are delivered oldest first.
- 401, 403 and 410 responses retry at the normal interval instead of backing
  off; they still count toward automatic deactivation.
- Telegram runs on Python 3.13 (python-telegram-bot 22.8), waits 25 seconds
  before treating a send as timed out, previews each entry's own link, and
  handles up to eight commands at once, so one slow `/import` or `/add` no
  longer stalls every chat. A group upgraded to a supergroup keeps its
  channel defaults.
- A Discord server that removed the bot has its subscriptions and digest
  deactivated. Plain-text Discord messages stay within 2000 characters.
- Platform heartbeats stop while Discord is reconnecting or after Telegram
  polling has died, so the health check reports them.
- Config reload reports database failures instead of answering 500, and says
  when a newly created `webhooks.yaml` needs a restart. An unreadable `.env`
  is skipped with a warning.
- A failed `POST /api/feeds/{id}/refresh` now counts toward the feed's error
  count and deactivation. `POST /api/feeds` and `POST /api/feeds/test` accept
  the `gh:` and `pypi:` shortcuts, and `POST /api/subscriptions` rejects
  unknown platforms.
- Re-adding a paused feed says it was resumed. An OPML import leaves paused
  subscriptions paused.
- A message template must use at least one placeholder; `/template <url> show`
  used to store the word "show" as the whole message.
- IMAP connections time out. DeepL gets the regional target codes it expects
  (EN-US, PT-BR, ZH-HANT).

### Added

- Telegram `/digest enable` accepts `max_articles=` and `include_filtered=`.
- `newsflow_entries_undeliverable_total` and
  `newsflow_entries_dropped_unsent_total` in `/metrics`; queued entries lost
  to cleanup also show in `/feed status`.
- Warnings for misspelled settings, `CACHE_BACKEND=redis` without
  `REDIS_URL`, and `json_api` sources without a guid mapping.

### Changed

- The Docker image installs the exact versions in `poetry.lock`, the same set
  CI tests.

## [0.9.5] - 2026-09-10

### Security

- aiohttp 3.14.3, cryptography 50.0.0 and pyasn1 0.6.4, clearing the security
  advisories published against the versions 0.9.4 pinned.

### Fixed

- `/import` read the OPML file with `StreamReader.read(n)` — the same call
  that truncated large feeds before 0.9.4 — so a chunked OPML silently
  imported only its first part. It now goes through the fetcher's capped,
  redirect-validated read.
- Discord `/feed list` and `/feed status`, and Telegram `/info`, built their
  replies from unclipped titles and URLs and could exceed the platform's
  message limit. Replies are now packed to a budget by whole rows, so no
  title or link is cut in half.
- The readiness probe logs why the database check failed instead of only
  reporting unhealthy.

### Changed

- Scheduled digests on Telegram use the same 3800-character chunks as
  `/digest now`. They were chunked at Discord's 1900 before, so a long
  digest arrived as twice as many messages.
- The Docker image includes `LICENSE`.

## [0.9.4] - 2026-08-25

Version numbering: this release follows 0.10.1. The minor field in this project
does not go past 9, so 0.10.0 and 0.10.1 were a misstep — both are withdrawn,
their tags and their images are gone, and numbering picks up at 0.9.4. Nothing
they shipped was reverted; templates, mentions and topic delivery are all here.
Their source stays reachable at `ca2a8db` and `5d8d43c`, but don't rebuild from
it: both carry the article-truncation bug fixed below. Pin `0.9.4`.

### Security

- `email_imap` sources now verify the IMAP server's TLS certificate. It encrypted the
  connection before but checked nothing, leaving the mailbox password and every
  message readable to anyone in the middle. **This can break an existing setup:**
  a self-hosted mail server with a self-signed certificate now fails to fetch
  until you replace the certificate or set `tls: insecure` on that source.
  Mainstream providers (Gmail, Outlook, Fastmail, Yahoo, Zoho) are unaffected.
- User-supplied filter patterns now run on the `regex` engine with a match
  timeout. The standard library's `re` cannot be interrupted, so a catastrophic
  pattern wedged the event loop for the whole process. Timeouts and invalid
  patterns fail open with a warning, as before.
- The Docker Compose file publishes the API port on `127.0.0.1` instead of
  `0.0.0.0`. A VPS has no private interface, and read endpoints are unauthenticated
  unless `API_KEY` is set. Your existing compose file is untouched; put a
  reverse proxy in front, or change it back on purpose.

### Fixed

- **Large feeds silently lost most of their articles.** The size cap on response bodies
  used `StreamReader.read(n)`, which hands back only what is already buffered, so
  any body arriving in more than one chunk got cut off with no error anywhere.
  BBC News parsed as 4 of its 37 entries and the Guardian as 8 of 45, while feeds
  small enough to arrive in one chunk were unaffected. Present in every release
  up to 0.10.1, in both the RSS path and `json_api` sources.
- Rate-limited webhook deliveries retry once instead of counting toward the
  destination's circuit breaker, which could disable a healthy endpoint after ten
  rate limits in a row. The wait comes from `X-RateLimit-Reset-After`, because
  Discord answers webhook 429s with a `Retry-After` in milliseconds.
- An `email_imap` message whose `Date` header won't parse now stores no
  publication date instead of 1900-01-01, which had placed it outside
  `MAX_ENTRY_PUBLISH_AGE_DAYS` and dropped it from every dispatch round.

### Added

- `discord` and `matrix` outbound webhook formats. A Discord channel webhook
  needs no bot token, no server invite and no gateway connection; append
  `?thread_id=<id>` to the URL to post into a thread. `matrix` targets
  matrix-hookshot's generic webhook and must not have a transformation function
  configured.
- `tls` option on `email_imap` sources: `verify` (default) or `insecure`.
- Guidance for running translation and digests against a local LLM
  (Ollama, vLLM, LM Studio, LocalAI) in the user guide.

### Changed

- `ENTRY_RETENTION_DAYS` default raised from 7 to 10 days, so cleanup cannot
  delete the oldest day a weekly digest still needs.
- A `webhooks.yaml` on its own now satisfies the startup configuration check: a
  headless RSS-to-webhook deployment no longer needs a Discord or Telegram token.
- `GUIDE.md` is now `docs/user-guide.md`, and `README_CN.md` is now
  `README.zh-CN.md`.

## [0.10.1] - 2026-08-06 — withdrawn

### Fixed

- Feed delivery hardening and DEBUG log hygiene: `DB_ECHO` is decoupled from
  `LOG_LEVEL` so raising the log level cannot flush stored secrets into the log.

### Changed

- Documentation reconciled with the code after drift.

## [0.10.0] - 2026-07-18 — withdrawn

### Added

- Per-subscription message templates with `{placeholder}` substitution. A
  template that fails to render falls back to the default layout rather than
  losing the article.
- Discord mentions per subscription, built from a native role or user picker and
  whitelisted through `allowed_mentions` at delivery.
- Telegram forum-topic delivery: the topic is captured at subscribe time and
  delivery self-heals to the default view if the topic is later deleted.

## [0.9.3] - 2026-07-17

### Added

- Offline configuration validation (`make checkconfig`) covering `.env` and both
  YAML files, plus cross-file target references.
- Hot reload of `webhooks.yaml` and `sources.yaml` via SIGHUP or
  `POST /api/admin/reload`. A file that fails to parse keeps the previous state
  instead of killing the process.
- REST API completeness: subscription CRUD, OPML export, and `/metrics`.

### Changed

- Unknown keys in either YAML file are now a hard startup error. A typo used to
  be ignored silently, which is how an HMAC signing secret once disappeared.
- ruff upgraded from 0.1 to 0.15.

## [0.9.2] - 2026-07-17

### Added

- Channel management commands, digest scheduling with per-channel timezones, and
  a `/manage` button panel on Telegram.
- Filter matching semantics: an ASCII-only keyword matches on word boundaries, so
  `ai` stops firing on "brain" while still hitting "AI芯片"; CJK and punctuated
  keywords keep substring matching, which is the natural unit there.
- Same-language short-circuit, so an entry already in the target language is not
  sent to the translation provider.

### Security

- State-changing commands are gated behind administrator permissions. Button
  visibility was never access control.

## [0.9.1] - 2026-07-16

### Added

- Telegram command menu and inline keyboards.
- `FEED_MAX_CONCURRENT` and `LOG_FORMAT` wired through to the runtime.
- End-to-end delivery pipeline integration test.

### Fixed

- The dispatch loop survives a failed per-subscription commit instead of
  aborting the whole round.
- Subscriptions owned by another source are kept when a source leaves
  `sources.yaml`.
- httpx no longer logs bot tokens; JSON tracebacks render correctly.

### Changed

- mypy (`disallow_untyped_defs`) and ruff became blocking CI gates.
- `asyncpg` raised to 0.30 so installs succeed on Python 3.13.

## [0.9.0] - 2026-06-01

First release to carry a version number in the tree. RSS, JSON-API, IMAP and
inbound-webhook sources feeding a platform-agnostic dispatch loop that delivers
to Discord, Telegram and declarative webhook destinations, with keyword and
regex filtering, optional translation, silent mode, AI daily and weekly digests,
an optional REST API, and Docker images published to GHCR.

Tags `v0.1.0` through `v0.8.0` were added retroactively to mark development
milestones that predate versioning. They have no changelog entries and no
published Docker images.

[0.9.9]: https://github.com/Lynthar/NewsFlow-Bot/compare/v0.9.5...v0.9.9
[0.9.5]: https://github.com/Lynthar/NewsFlow-Bot/compare/v0.9.4...v0.9.5
[0.9.4]: https://github.com/Lynthar/NewsFlow-Bot/compare/v0.9.3...v0.9.4
[0.10.1]: https://github.com/Lynthar/NewsFlow-Bot/compare/ca2a8db...5d8d43c
[0.10.0]: https://github.com/Lynthar/NewsFlow-Bot/compare/v0.9.3...ca2a8db
[0.9.3]: https://github.com/Lynthar/NewsFlow-Bot/compare/v0.9.2...v0.9.3
[0.9.2]: https://github.com/Lynthar/NewsFlow-Bot/compare/v0.9.1...v0.9.2
[0.9.1]: https://github.com/Lynthar/NewsFlow-Bot/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/Lynthar/NewsFlow-Bot/compare/v0.8.0...v0.9.0
