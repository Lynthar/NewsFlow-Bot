"""Update processing of the Application build_application configures, driven through
real PTB with only the Bot API transport faked."""

import asyncio
import json

from telegram import Update
from telegram.ext import Application, CommandHandler
from telegram.request import BaseRequest

from newsflow.adapters.telegram.bot import build_application


class _BotApi(BaseRequest):
    """Answers getMe; nothing else reaches the network while updates are queued by hand."""

    @property
    def read_timeout(self) -> float | None:
        return None

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def do_request(self, url, method, request_data=None, *args, **kwargs):
        bot = {"id": 1, "is_bot": True, "first_name": "b", "username": "b"}
        return 200, json.dumps({"ok": True, "result": bot}).encode()


def _command(app: Application, chat_id: int, text: str) -> Update:
    message = {
        "message_id": chat_id,
        "date": 0,
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": chat_id, "is_bot": False, "first_name": "u"},
        "text": text,
        "entities": [{"type": "bot_command", "offset": 0, "length": len(text)}],
    }
    update = Update.de_json({"update_id": chat_id, "message": message}, app.bot)
    assert update is not None
    return update


async def test_a_slow_command_does_not_hold_up_other_chats():
    # Updates used to run one at a time, so one /import of 200 feeds stalled every chat.
    api = _BotApi()
    app = (
        Application.builder()
        .token("1:x")
        .request(api)
        .get_updates_request(api)
        .concurrent_updates(build_application("1:x").update_processor)
        .build()
    )
    release, answered = asyncio.Event(), asyncio.Event()

    async def slow(update, context):
        await release.wait()

    async def quick(update, context):
        answered.set()

    app.add_handler(CommandHandler("slow", slow))
    app.add_handler(CommandHandler("quick", quick))
    await app.initialize()
    await app.start()
    try:
        await app.update_queue.put(_command(app, 1, "/slow"))
        await app.update_queue.put(_command(app, 2, "/quick"))
        await asyncio.wait_for(answered.wait(), timeout=2)
    finally:
        release.set()
        await app.stop()
        await app.shutdown()
