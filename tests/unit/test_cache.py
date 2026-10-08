"""The translation cache: a key that changes with the settings that shape the output,
and a Redis backend that, against a real socket, treats a stalled server as a miss
and closes its connection on shutdown."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from newsflow.services.cache import (
    MemoryCache,
    RedisCache,
    close_cache,
    get_cache,
    init_cache,
)
from newsflow.services.translation.base import TranslationService
from newsflow.services.translation.openai import OpenAIProvider


class _Completions:
    """The OpenAI SDK boundary: every call answers with the model that was asked."""

    def __init__(self) -> None:
        self.calls = 0

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls += 1
        message = SimpleNamespace(content=f"by {kwargs['model']}")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _openai(cache: MemoryCache, completions: _Completions, **config: Any) -> TranslationService:
    provider = OpenAIProvider(api_key="k", **config)
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return TranslationService(provider, cache=cache)


@pytest.mark.parametrize(
    "change",
    [
        {"model": "m2"},
        {"base_url": "http://other.example/v1"},
        {"system_prompt_template": "Into {target_name}, from {source_desc}:"},
    ],
    ids=["model", "endpoint", "prompt"],
)
async def test_a_translation_cached_under_other_settings_is_not_served(change):
    cache, completions = MemoryCache(), _Completions()

    first = await _openai(cache, completions, model="m1").translate("hello", "zh-CN")
    again = await _openai(cache, completions, model="m1").translate("hello", "zh-CN")
    changed = await _openai(cache, completions, **{"model": "m1", **change}).translate(
        "hello", "zh-CN"
    )

    assert again.from_cache and again.translated_text == first.translated_text
    assert not changed.from_cache
    assert completions.calls == 2


class _Server:
    """A TCP server that answers every Redis command with nil, or never answers."""

    def __init__(self, *, answer: bool) -> None:
        self.answer = answer
        self.disconnected = asyncio.Event()
        self.url = ""

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Each command arrives as a RESP array whose header line starts with "*".
        while line := await reader.readline():
            if self.answer and line.startswith(b"*"):
                writer.write(b"$-1\r\n")
                await writer.drain()
        writer.close()
        self.disconnected.set()


@asynccontextmanager
async def _serve(*, answer: bool) -> AsyncIterator[_Server]:
    server = _Server(answer=answer)
    tcp = await asyncio.start_server(server.handle, "127.0.0.1", 0)
    server.url = f"redis://127.0.0.1:{tcp.sockets[0].getsockname()[1]}/0"
    async with tcp:
        yield server


async def test_a_stalled_redis_is_a_miss_not_a_hang():
    async with _serve(answer=False) as server:
        cache = RedisCache(server.url, timeout=0.2)
        try:
            assert await asyncio.wait_for(cache.get("k"), 5) is None
            assert await asyncio.wait_for(cache.set("k", "v", ttl=60), 5) is False
        finally:
            await cache.close()


async def test_close_cache_closes_the_redis_connection():
    async with _serve(answer=True) as server:
        cache = init_cache("redis", redis_url=server.url)
        try:
            assert await cache.get("k") is None
            assert not server.disconnected.is_set()
        finally:
            await close_cache()

        await asyncio.wait_for(server.disconnected.wait(), 5)
        assert get_cache() is None
