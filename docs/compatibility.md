# Compatibility and versioning

## The promise

Since 1.0, NewsFlow-Bot follows Semantic Versioning for everything under
"What is covered" below: changing any of it in a way that breaks a working setup
takes a new major version, and that release's entry in
[CHANGELOG.md](../CHANGELOG.md) says what to change.

Two kinds of change can still reach you in a minor release: a change to one of
the experimental surfaces, and a move of a default model. Both are named under
**Before you upgrade** in the changelog.

## What is covered

A breaking change to any of the following requires a new major version.

**Declarative configuration** — the keys and value shapes of `webhooks.yaml` and
`sources.yaml`, the experimental surfaces below excepted. Unknown keys are a hard
startup error, so a rename is never a silent change: it stops the bot. Adding an
optional key is additive.

**Environment configuration** — the names and accepted values of the variables in
`.env`, **and their defaults**, the default models excepted. Moving a default
silently changes behaviour for everyone who never set it, with no error to warn
them, so a default change counts as breaking rather than as a tweak.

**The REST API** — paths, request and response shapes, status codes and
authentication rules for everything under `/api`, as published in the OpenAPI
document. New endpoints and new optional response fields are additive.

**Bot commands** — command names and their required arguments on Discord and
Telegram. Adding an optional argument is additive; renaming a command or making a
new argument required is breaking.

**In-place upgrades** — migrating from any 1.x release to a later 1.x release
always works in place, on both SQLite and PostgreSQL, with the migrations that
run automatically at startup.

**Python support** — the supported interpreter range (3.11–3.13 today). Dropping
a version is breaking. This mostly matters for bare-metal installs; the Docker
image pins its own interpreter.

## Experimental

These have only run against local stand-ins, never against the services they are
built for, so their configuration may still have to change when they do. Until
then a minor release can change them.

- **`email_imap` sources** — the `email_imap` type in `sources.yaml` and the keys
  of its `config:`. Tested against a local mail server, not a real provider.
- **The `matrix` webhook format** — tested by the shape of its payload, not
  against a running matrix-hookshot.

An experimental surface joins "What is covered" in a minor release, and the
changelog says so.

## What is never covered

**The `generic` webhook payload.** It is an event stream, not an API: fields may
be added, renamed or removed in any release. If you parse it in n8n, Zapier or
your own receiver, pin the version you tested against and read the changelog
before upgrading — your automation can otherwise start missing a field with no
error on either side. The named formats (`slack`, `discord`, `matrix`, `ntfy`,
`lark`, `wecom`) follow their receivers' contracts, which are not ours to
promise either.

**Internal Python APIs.** `newsflow.*` is an application, not a library. Importing
from it works until it doesn't.

**The database schema.** Tables and columns change freely; only the upgrade path
above is promised.

**Log message text and formatting**, including the structured-log field names.

**AI-generated output.** Digest wording depends on the model you point the bot at.

**The default models.** `OPENAI_MODEL` and `DIGEST_MODEL` default to specific
models, and providers retire models on their own schedule; a default that points
at a retired model fails every call. So a minor release can move a default model,
together with the `OPENAI_REASONING_EFFORT` or `DIGEST_REASONING_EFFORT` default
paired with it, since a model can refuse an effort another one accepts. To keep a
model across upgrades, set both its model and its effort variable.

## The one carve-out: security fixes

A fix for a vulnerability may land in a minor or patch release **even when it
breaks a configuration that used to work**. The alternative is shipping a known
credential-exposure or data-loss defect until the next major version, which is
worse for everyone it affects.

When that happens, the release's changelog entry leads with a **Security**
section naming exactly which configurations stop working and what to change. The
IMAP certificate verification change is the first example: connections were
encrypted but never verified, and turning verification on breaks self-hosted mail
servers that use a self-signed certificate.

If you cannot absorb that risk on your own schedule, pin an exact version and
read the changelog before every upgrade.

## Reading a version number

| Bump | What it means |
|---|---|
| Major | Something under "What is covered" changed. Read the changelog before upgrading. |
| Minor | New features and new optional configuration. Safe to upgrade unless the changelog has a **Before you upgrade** or **Security** section. |
| Patch | Fixes only. |
