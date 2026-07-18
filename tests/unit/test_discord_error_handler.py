"""Regression tests for the Discord slash-command error handler.

discord.py routes an exception raised inside a command callback to
``CommandTree.on_error``, NOT to ``Client.on_error``, and its default only
logs it. Because every command in this adapter defers ephemerally first, an
unhandled error would otherwise leave the user on a perpetual "thinking…".
``_on_app_command_error`` must log the root cause and send an ephemeral
notice — via followup when the interaction was already deferred, via the
initial response otherwise — and must never raise, even if that delivery
fails (an expired interaction token must not escape the handler).
"""

from unittest.mock import AsyncMock, MagicMock

import discord
from discord import app_commands

from newsflow.adapters.discord.bot import _on_app_command_error


def _interaction(*, is_done: bool) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.command = MagicMock()
    interaction.command.qualified_name = "feed add"
    interaction.response = MagicMock()
    interaction.response.is_done = MagicMock(return_value=is_done)
    interaction.response.send_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _error() -> app_commands.CommandInvokeError:
    # Mirrors how discord.py wraps a callback exception before dispatching it.
    return app_commands.CommandInvokeError(MagicMock(), RuntimeError("boom"))


async def test_deferred_interaction_gets_ephemeral_followup():
    interaction = _interaction(is_done=True)

    await _on_app_command_error(interaction, _error())

    interaction.followup.send.assert_awaited_once()
    assert interaction.followup.send.await_args.kwargs.get("ephemeral") is True
    interaction.response.send_message.assert_not_awaited()


async def test_unresponded_interaction_gets_ephemeral_initial_response():
    interaction = _interaction(is_done=False)

    await _on_app_command_error(interaction, _error())

    interaction.response.send_message.assert_awaited_once()
    assert interaction.response.send_message.await_args.kwargs.get("ephemeral") is True
    interaction.followup.send.assert_not_awaited()


async def test_delivery_failure_is_swallowed():
    interaction = _interaction(is_done=True)
    interaction.followup.send = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), "interaction expired")
    )

    # The apology can't be delivered, but the handler must not re-raise —
    # otherwise the exception would propagate back into discord.py's dispatch.
    await _on_app_command_error(interaction, _error())

    interaction.followup.send.assert_awaited_once()
