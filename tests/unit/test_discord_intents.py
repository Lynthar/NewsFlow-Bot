"""The Discord bot must not request privileged intents.

It is slash-command only: app commands arrive as interactions, which no
gateway intent gates. Requesting message_content (a privileged intent)
made every fresh deployment crash-loop with PrivilegedIntentsRequired
until the operator found the undocumented Developer Portal toggle —
for a capability the bot never used.
"""

from newsflow.adapters.discord.bot import NewsFlowBot


def test_bot_requests_no_privileged_intents():
    bot = NewsFlowBot()
    assert bot.intents.message_content is False
    assert bot.intents.members is False
    assert bot.intents.presences is False
    # Positive floor: the guild cache (channel lookups, self.guilds) needs
    # this — a regression to Intents.none() would pass the checks above
    # while silently breaking delivery.
    assert bot.intents.guilds is True
