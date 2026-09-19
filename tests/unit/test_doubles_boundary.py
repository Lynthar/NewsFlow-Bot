"""Test doubles stay at the boundary.

Replacing a name that newsflow itself owns couples the test to where the code
imports from, and a MagicMock standing in for our own object answers every
attribute with something truthy, so a renamed setting or method passes
silently. Configuration comes from the ``configure`` fixture and the database
from ``db``; what a test may still swap is the platform SDKs, HTTP, the clock
and the filesystem. Each remaining exception is listed with its reason, so
adding one is a deliberate act and a stale entry fails too.
"""

import re
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]

# patch("a.b"), patch.object(x, "b"), monkeypatch.setattr("a.b", …) and
# monkeypatch.setattr(x, "b", …), with or without the mock. / unittest.mock. prefix;
# the target is reported as the source spells it.
_DOUBLE = re.compile(
    r"(?<![\w.])(?:unittest\.)?(?:mock\.)?(?P<kind>patch\.object|patch|monkeypatch\.setattr)\(\s*"
    r'(?:(?P<obj>[A-Za-z_][\w.()]*)\s*,\s*)?"(?P<name>[^"]+)"'
)

# Names that are not ours: the platform SDKs, HTTP and the stdlib.
BOUNDARY_PREFIXES = ("telegram.", "discord.", "aiohttp.", "pathlib.")

# newsflow-owned names a test may replace, and why.
ALLOWED: dict[str, str] = {
    # The network, the LLM and the mailbox: what the code reaches through these is
    # outside the process.
    "newsflow.services.feed_service.get_fetcher": "HTTP boundary — the fetcher is the network",
    "newsflow.services.dispatcher.get_translation_service": "LLM boundary",
    "f._fetch_sync": "IMAP boundary — the synchronous imap-tools call",
    # Clocks: real waits would make the tests slow, not more truthful.
    "newsflow.services.dispatcher.asyncio.sleep": "clock — skips the 60s startup delay",
    "newsflow.adapters.webhook.bot.asyncio.sleep": "clock — records back-off waits",
    # Failure injection where the real component cannot be made to fail on cue.
    "AsyncSession.commit": "database boundary — one commit fails",
    "SubscriptionRepository.get_unsent_entries_for_subscription": (
        "one subscription's query blows up mid-round"
    ),
    "newsflow.services.dispatcher.render_template": "renderer crash — pins the fallback",
    "dispatcher._dispatch_once_inner": "concurrency probe — counts overlapping rounds",
    # The dispatcher singleton has no reset hook, and the preview task it would
    # spawn is fire-and-forget: it would outlive the test.
    "newsflow.services.get_dispatcher": "process-wide singleton",
    "newsflow.services.dispatcher.get_dispatcher": "process-wide singleton",
    "newsflow.api.routes.metrics.get_dispatcher": "process-wide singleton",
    "newsflow.adapters.telegram.bot.get_dispatcher": "process-wide singleton",
}


def _doubles() -> list[tuple[str, str]]:
    found = []
    for path in sorted(TESTS.rglob("*.py")):
        if path == Path(__file__):
            continue
        text = path.read_text(encoding="utf-8")
        for match in _DOUBLE.finditer(text):
            target = f"{match['obj']}.{match['name']}" if match["obj"] else match["name"]
            line = text.count("\n", 0, match.start()) + 1
            found.append((f"{path.relative_to(TESTS)}:{line}", target))
    return found


def test_doubles_replace_only_boundary_names():
    offenders = [
        f"{where}  {target}"
        for where, target in _doubles()
        if not target.startswith(BOUNDARY_PREFIXES) and target not in ALLOWED
    ]
    assert not offenders, (
        "these doubles replace a newsflow-owned name; use the `configure` / `db` fixtures "
        "or add the name to ALLOWED with a reason:\n" + "\n".join(offenders)
    )


def test_scanner_sees_every_spelling_of_patch():
    sample = (
        'patch("newsflow.a.b")\n'
        'mock.patch("newsflow.a.c")\n'
        'unittest.mock.patch("newsflow.a.d")\n'
        'mock.patch.object(x, "e")\n'
        'monkeypatch.setattr("newsflow.a.f", 1)\n'
    )
    names = [m["name"] for m in _DOUBLE.finditer(sample)]
    assert names == ["newsflow.a.b", "newsflow.a.c", "newsflow.a.d", "e", "newsflow.a.f"]


def test_allowed_entries_are_all_in_use():
    in_use = {target for _, target in _doubles()}
    stale = sorted(set(ALLOWED) - in_use)
    assert not stale, f"ALLOWED lists doubles no test uses any more: {stale}"


def test_patch_multiple_is_not_used():
    # Its targets are keyword names, invisible to the search above.
    users = [
        str(path.relative_to(TESTS))
        for path in sorted(TESTS.rglob("*.py"))
        if path != Path(__file__) and "patch.multiple(" in path.read_text(encoding="utf-8")
    ]
    assert not users, f"patch.multiple hides its targets; spell them out with patch(): {users}"
