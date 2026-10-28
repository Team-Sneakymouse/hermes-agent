"""Discord delivery receipts come from confirmed REST writes (``adapter_receipts``), driven
through a real discord.py client against a loopback Discord API."""
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("discord")

import gateway.conversation_plugins as api
from gateway.config import Platform, PlatformConfig
from gateway.session import SessionSource
from plugins.platforms.discord.adapter_receipts import delivery_receipt_trace
from tests.plugins.platforms.fake_discord_api import discord_client


@pytest.fixture
def adapter():
    from plugins.platforms.discord.adapter import DiscordAdapter
    return DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))


@pytest.fixture
def receipts(monkeypatch):
    import hermes_cli.plugins as sdk
    seen = []

    async def hook(name, **kwargs):
        if name == "gateway_delivery_receipt":
            seen.append(kwargs["receipt"])
        return []
    monkeypatch.setattr(sdk, "ainvoke_hook", hook)
    return seen


@pytest.mark.asyncio
async def test_connected_bot_reports_confirmed_message_writes_only_inside_a_turn(monkeypatch, receipts):
    import discord
    import plugins.platforms.discord.adapter as discord_platform
    from plugins.platforms.discord.adapter import DiscordAdapter
    from tests.gateway.test_discord_connect import FakeBot
    built = []

    def bot_factory(**kwargs):
        built.append(kwargs)
        return FakeBot(intents=kwargs["intents"])
    monkeypatch.setattr("gateway.status.acquire_scoped_lock", lambda scope, identity, metadata=None: (True, None))
    monkeypatch.setattr("gateway.status_scoped_locks.release_scoped_lock", lambda scope, identity: None)
    monkeypatch.setattr(discord_platform.commands, "Bot", bot_factory)
    connected = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    monkeypatch.setattr(connected, "_resolve_allowed_usernames", AsyncMock())
    assert await connected.connect() is True
    await connected.disconnect()

    async with discord_client(monkeypatch, http_trace=built[0]["http_trace"]) as client:
        channel = client.get_partial_messageable(1)
        with api.delivery_scope(SessionSource(Platform.DISCORD, "1", message_id="123"), "key", "sid"):
            sent = await channel.send("reply")
            await channel.get_partial_message(sent.id).edit(content="edited")
            await client.http.request(discord.http.Route("POST", "/channels/{channel_id}/typing", channel_id=1))
        await channel.send("proactive, no turn")
    assert [(r.channel_id, r.message_ids, r.operation, r.session_id) for r in receipts] == [
        ("1", ("501",), "send", "sid"), ("1", ("501",), "edit", "sid")]


@pytest.mark.asyncio
async def test_each_successful_chunk_has_receipt_even_when_later_send_fails(adapter, monkeypatch, receipts):
    async with discord_client(monkeypatch, http_trace=delivery_receipt_trace(), failing_creates={2}) as client:
        adapter._client = client
        adapter._resolve_channel = AsyncMock(return_value=client.get_partial_messageable(1))
        adapter.format_message = lambda text: text
        adapter.truncate_message = lambda *args: ["first", "second"]
        adapter._record_response_async = AsyncMock(side_effect=lambda reply, result, *args: result)
        source = SessionSource(Platform.DISCORD, "1", chat_type="group", message_id="123", conversation_lane="a")
        with api.delivery_scope(source, "canonical-key", "session-id"):
            result = await adapter.send("1", "body", reply_to="123")
    assert not result.success
    assert len(receipts) == 1
    assert receipts[0].message_ids == ("501",)
    assert receipts[0].session_id == "session-id"
    assert receipts[0].session_key == "canonical-key"
    assert receipts[0].origin_message_id == "123"
    assert receipts[0].channel_id == "1"
    assert receipts[0].base_session_key == "canonical-key"


@pytest.mark.asyncio
@pytest.mark.parametrize("finalize", [False, True])
async def test_stream_edits_and_partial_overflow_continuations_report_ids(adapter, monkeypatch, receipts, finalize):
    async with discord_client(monkeypatch, http_trace=delivery_receipt_trace(), failing_creates={2, 3}) as client:
        adapter._client = client
        adapter._resolve_channel = AsyncMock(return_value=client.get_partial_messageable(1))
        adapter.format_message = lambda text: text
        adapter.truncate_message = lambda *args: ["first", "second", "third"]
        with api.delivery_scope(SessionSource(Platform.DISCORD, "1", message_id="123"), "key", "sid"):
            result = await adapter.edit_message("1", "500", "x" * 3000, finalize=finalize)
    assert result.success
    assert [(r.message_ids[0], r.operation) for r in receipts] == (
        [("500", "edit"), ("501", "send")] if finalize else [("500", "edit")])


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["send_document", "send_video", "send_image_file", "send_multiple_images",
                                    "send_voice", "send_voice_native", "send_image", "send_animation"])
async def test_native_media_replies_also_emit_receipts(adapter, monkeypatch, receipts, tmp_path, method):
    import plugins.platforms.discord.adapter as discord_adapter
    path = tmp_path / "attachment.bin"
    path.write_bytes(b"test attachment")
    monkeypatch.setattr(discord_adapter, "_read_url_image_with_redirect_guard",
                        AsyncMock(return_value=(200, b"image", {"content-type": "image/png"})))
    # A native voice message needs Ogg/Opus; otherwise the adapter sends an audio attachment.
    voice = (b"OggS", 1.0) if method == "send_voice_native" else None
    monkeypatch.setattr(adapter, "_ogg_opus_voice_payload", AsyncMock(return_value=voice))
    async with discord_client(monkeypatch, http_trace=delivery_receipt_trace()) as client:
        adapter._client = client
        adapter._resolve_channel = AsyncMock(return_value=client.get_partial_messageable(1))
        args = str(path)
        if method == "send_multiple_images":
            args = [("https://example.org/image.png", "caption")]
        elif method in {"send_image", "send_animation"}:
            args = "https://example.org/image.png"
        with api.delivery_scope(SessionSource(Platform.DISCORD, "1", message_id="123"), "key", "sid"):
            result = await getattr(adapter, "send_voice" if method == "send_voice_native" else method)("1", args)
    assert result.success
    assert [r.message_ids for r in receipts] == [("501",)]
