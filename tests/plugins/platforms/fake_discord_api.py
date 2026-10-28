"""Loopback stand-in for Discord's REST API, for driving a real discord.py client end to end."""
import itertools
import json
from contextlib import asynccontextmanager

import discord
from aiohttp import web
from aiohttp.test_utils import TestServer

BOT = {"id": "999", "username": "hermes", "discriminator": "0", "global_name": None, "avatar": None, "bot": True}


def _json(payload, status=200):
    # Exactly Discord's header: discord.py parses JSON only for an exact ``application/json``.
    return web.Response(body=json.dumps(payload).encode(), status=status,
                        headers={"Content-Type": "application/json"})


def message_payload(message_id, channel_id):
    return {
        "id": str(message_id), "channel_id": str(channel_id), "type": 0, "content": "", "author": BOT,
        "timestamp": "2026-01-01T00:00:00+00:00", "edited_timestamp": None, "tts": False,
        "mention_everyone": False, "mentions": [], "mention_roles": [], "embeds": [], "pinned": False,
        "attachments": [{"id": "1", "filename": "attachment.bin", "size": 1,
                         "url": "https://cdn.invalid/a", "proxy_url": "https://cdn.invalid/a"}],
    }


class FakeDiscordAPI:
    """Message creates get ids 501, 502, ...; ``failing_creates`` holds 1-based create indexes
    answered with 403 Missing Permissions (no discord.py retry, no reply-reference fallback)."""

    def __init__(self, failing_creates=()):
        self.failing_creates = set(failing_creates)
        self._creates = itertools.count(1)
        self._ids = itertools.count(501)
        self.app = web.Application()
        self.app.router.add_get("/api/v10/users/@me", lambda request: _json(BOT))
        self.app.router.add_post("/api/v10/channels/{channel_id}/messages", self._create)
        self.app.router.add_patch("/api/v10/channels/{channel_id}/messages/{message_id}", self._edit)
        self.app.router.add_post("/api/v10/channels/{channel_id}/typing", lambda request: web.Response(status=204))

    async def _create(self, request):
        await request.read()
        if next(self._creates) in self.failing_creates:
            return _json({"code": 50013, "message": "Missing Permissions"}, status=403)
        return _json(message_payload(next(self._ids), request.match_info["channel_id"]))

    async def _edit(self, request):
        await request.read()
        return _json(message_payload(request.match_info["message_id"], request.match_info["channel_id"]))


@asynccontextmanager
async def discord_client(monkeypatch, *, http_trace, failing_creates=()):
    """A logged-in ``discord.Client`` whose REST calls reach a ``FakeDiscordAPI`` on loopback."""
    api = FakeDiscordAPI(failing_creates)
    server = TestServer(api.app)
    await server.start_server()
    monkeypatch.setattr(discord.http.Route, "BASE", str(server.make_url("/api/v10")))
    client = discord.Client(intents=discord.Intents.none(), http_trace=http_trace)
    await client.http.static_login("token")
    try:
        yield client
    finally:
        await client.http.close()
        await server.close()
