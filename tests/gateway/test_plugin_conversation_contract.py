"""Public conversation extension contracts (no Discord SDK/network required)."""
from dataclasses import replace
from gateway.config import Platform
from gateway.session import SessionSource, build_session_key
import pytest
from unittest.mock import AsyncMock
try:
    import discord  # exercise the real SDK when the Discord PM extra is installed
except ImportError:
    pass
from tests.gateway.test_discord_free_response import adapter, make_message, FakeTextChannel


def test_lane_is_durable_profile_scoped_and_not_a_delivery_identifier():
    source = SessionSource(Platform.DISCORD, "123", chat_type="group", user_id="456")
    routed = replace(source, conversation_lane="plugin/lane:one")
    assert routed.chat_id == source.chat_id
    assert routed.thread_id is None
    assert build_session_key(routed, False, profile="work") != build_session_key(source, False, profile="work")
    assert build_session_key(routed, False, profile="work") != build_session_key(routed, False, profile="other")
    assert SessionSource.from_dict(routed.to_dict()).conversation_lane == routed.conversation_lane
    assert build_session_key(routed, False) == build_session_key(SessionSource.from_dict(routed.to_dict()), False)


@pytest.mark.asyncio
async def test_admission_veto_precedes_content_mutation_and_thread_creation(adapter, monkeypatch):
    import hermes_cli.plugins as sdk
    seen = []
    async def veto(name, **kwargs):
        if name == "gateway_message_admission":
            seen.append(kwargs["context"])
            return [{"action": "block"}]
        return []
    monkeypatch.setattr(sdk, "ainvoke_hook", veto)
    adapter._auto_create_thread = AsyncMock()
    message = make_message(channel=FakeTextChannel(), content="  <@999> original  ")
    assert await adapter._handle_message(message) is False
    assert message.content == "  <@999> original  "
    assert seen[0].original_content == message.content
    assert seen[0].bot_id == "999"
    adapter._auto_create_thread.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_routing_runs_before_batching_with_full_unfiltered_history(adapter, monkeypatch):
    import hermes_cli.plugins as sdk
    from types import SimpleNamespace
    adapter.config.extra.update(auto_thread=False, history_backfill=False, history_backfill_limit=20, group_sessions_per_user=False)
    adapter._text_batch_delay_seconds = 1
    seen = []
    def batch(event):
        seen.append(event)
    adapter._enqueue_text_event = batch
    channel = FakeTextChannel()
    calls = []
    def history(**kwargs):
        calls.append(kwargs)
        async def items():
            for mid in range(100, 80, -1):
                yield SimpleNamespace(id=mid)
        return items()
    channel.history = history
    async def hook(name, **kwargs):
        if name == "gateway_session_route":
            assert await kwargs["services"].preceding_message_ids() == tuple(str(i) for i in range(100, 80, -1))
            assert kwargs["context"].original_content == "<@999> hello"
            return [{"lane": "custom/chosen"}]
        return []
    monkeypatch.setattr(sdk, "ainvoke_hook", hook)
    message = make_message(channel=channel, content="<@999> hello")
    assert await adapter._handle_message(message)
    assert seen[0].source.conversation_lane == "custom/chosen"
    assert seen[0].source.chat_id == str(channel.id)
    assert calls[0]["limit"] == 20
    assert calls[0]["before"] is message
    assert calls[0]["oldest_first"] is False


@pytest.mark.asyncio
async def test_each_successful_chunk_has_receipt_even_when_later_send_fails(adapter, monkeypatch):
    import gateway.conversation_plugins as api
    import hermes_cli.plugins as sdk
    from types import SimpleNamespace
    assert hasattr(api, "delivery_scope"), "public delivery scope is missing"
    receipts = []
    async def hook(name, **kwargs):
        if name == "gateway_delivery_receipt":
            receipts.append(kwargs["receipt"])
        return []
    monkeypatch.setattr(sdk, "ainvoke_hook", hook)
    channel = SimpleNamespace(id=1, send=AsyncMock(side_effect=[SimpleNamespace(id=501), RuntimeError("offline")]))
    adapter._resolve_channel = AsyncMock(return_value=channel)
    adapter._is_forum_parent = lambda c: False
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
async def test_background_turn_supplies_delivery_identity(adapter, monkeypatch):
    import gateway.conversation_plugins as api
    import hermes_cli.plugins as sdk
    from gateway.platforms.base import MessageEvent, MessageType
    receipts = []
    async def hook(name, **kwargs):
        if name == "gateway_delivery_receipt":
            receipts.append(kwargs["receipt"])
        return []
    monkeypatch.setattr(sdk, "ainvoke_hook", hook)
    async def handler(event):
        if hasattr(api, "bind_delivery_session"):
            api.bind_delivery_session("sid")
        await api.report_delivery("1", ("500",))
    adapter._message_handler = handler
    adapter._run_processing_hook = AsyncMock()
    adapter._start_typing_refresh = lambda *args: None
    adapter._stop_typing_refresh = AsyncMock()
    source = SessionSource(Platform.DISCORD, "1", message_id="123")
    event = MessageEvent(text="hello", source=source, message_type=MessageType.TEXT)
    await adapter._process_message_background(event, "key")
    assert len(receipts) == 1
    assert receipts[0].session_id == "sid"
    assert receipts[0].origin_message_id == "123"


@pytest.mark.asyncio
@pytest.mark.parametrize("finalize", [False, True])
async def test_stream_edits_and_partial_overflow_continuations_report_ids(adapter, monkeypatch, finalize):
    import gateway.conversation_plugins as api
    import hermes_cli.plugins as sdk
    from types import SimpleNamespace
    receipts = []
    async def hook(name, **kwargs):
        if name == "gateway_delivery_receipt":
            receipts.extend(kwargs["receipt"].message_ids)
        return []
    monkeypatch.setattr(sdk, "ainvoke_hook", hook)
    msg = SimpleNamespace(id=500, edit=AsyncMock())
    channel = SimpleNamespace(id=1, get_partial_message=lambda mid: msg,
                              send=AsyncMock(side_effect=[SimpleNamespace(id=501), RuntimeError("offline"), RuntimeError("offline")]))
    adapter._resolve_channel = AsyncMock(return_value=channel)
    adapter.format_message = lambda text: text
    adapter.truncate_message = lambda *args: ["first", "second", "third"]
    source = SessionSource(Platform.DISCORD, "1", message_id="123")
    with api.delivery_scope(source, "key", "sid"):
        result = await adapter.edit_message("1", "500", "x" * 3000, finalize=finalize)
    assert result.success
    assert receipts == (["500", "501"] if finalize else ["500"])


@pytest.mark.asyncio
async def test_reset_observer_has_source_channel_and_canonical_keys(monkeypatch):
    from tests.gateway.test_session_model_reset import _make_runner, _make_event
    import hermes_cli.plugins as sdk
    runner = _make_runner()
    event = _make_event("/new")
    import hermes_cli.lifecycle as lifecycle
    observed = []
    monkeypatch.setattr(lifecycle, "_observe", lambda name, **kwargs: observed.append(name))
    seen = []
    async def hook(name, **kwargs):
        if name == "on_session_reset":
            seen.append(kwargs)
        return []
    monkeypatch.setattr(sdk, "ainvoke_hook", hook)
    await runner._handle_reset_command(event)
    assert len(seen) == 1
    assert seen[0]["source"].chat_id == event.source.chat_id
    assert seen[0]["channel_id"] == event.source.chat_id
    assert seen[0]["session_key"] == build_session_key(event.source)
    assert seen[0]["base_session_key"] == build_session_key(event.source)
    assert seen[0]["new_session_id"] == "sess-1"
    assert "on_session_reset" in observed


@pytest.mark.asyncio
async def test_resolved_session_id_is_bound_before_streaming(monkeypatch):
    import gateway.conversation_plugins as api
    import hermes_cli.plugins as sdk
    from gateway.run_turn import GatewayTurnMixin
    from types import SimpleNamespace
    receipts = []
    async def hook(name, **kwargs):
        if name == "gateway_delivery_receipt":
            receipts.append(kwargs["receipt"])
        return []
    monkeypatch.setattr(sdk, "ainvoke_hook", hook)
    source = SessionSource(Platform.DISCORD, "1", message_id="10")
    entry = SimpleNamespace(session_key="key", session_id="compressed-tip")
    runner = SimpleNamespace(_recover_telegram_topic_thread_id=lambda source: None,
                             async_session_store=SimpleNamespace(get_or_create_session=AsyncMock(return_value=entry)),
                             _cache_session_source=lambda *args: None, _is_telegram_topic_lane=lambda source: False)
    event = SimpleNamespace(metadata={}, internal=False)
    with api.delivery_scope(source, "key"):
        await GatewayTurnMixin._hmwa_resolve_session(runner, event, source)
        await api.report_delivery("1", ("500",))
    assert receipts[0].session_id == "compressed-tip"


@pytest.mark.asyncio
async def test_delivery_dispatch_retains_routed_profile_identity(tmp_path, monkeypatch):
    import gateway.conversation_plugins as api
    import hermes_cli.plugins as sdk
    from hermes_constants import get_hermes_home
    from gateway.session_identity import RoutingIdentity
    source = SessionSource(Platform.DISCORD, "1", message_id="10", profile="secondary")
    source._identity = RoutingIdentity("default", "secondary", tmp_path / "primary", tmp_path / "secondary")
    homes = []
    async def hook(name, **kwargs):
        if name == "gateway_delivery_receipt":
            homes.append(get_hermes_home())
        return []
    monkeypatch.setattr(sdk, "ainvoke_hook", hook)
    with api.delivery_scope(source, "key", "sid"):
        await api.report_delivery("1", ("500",))
    assert homes == [tmp_path / "secondary"]


@pytest.mark.asyncio
async def test_thread_admission_uses_parent_and_thread_for_profile_routing(adapter, monkeypatch):
    from tests.gateway.test_discord_free_response import FakeThread
    import hermes_cli.plugins as sdk
    built = []
    build = adapter.build_source
    def capture(**kwargs):
        source = build(**kwargs)
        built.append(source)
        return source
    adapter.build_source = capture
    async def block(name, **kwargs):
        return [{"action": "block"}] if name == "gateway_message_admission" else []
    monkeypatch.setattr(sdk, "ainvoke_hook", block)
    message = make_message(channel=FakeThread(2, parent=FakeTextChannel(1)), content="hello")
    assert not await adapter._handle_message(message)
    assert built[0].thread_id == "2"
    assert built[0].parent_chat_id == "1"


@pytest.mark.asyncio
async def test_inline_bypass_commands_also_supply_delivery_identity(adapter, monkeypatch):
    import gateway.conversation_plugins as api
    import hermes_cli.plugins as sdk
    from gateway.platforms.base import MessageEvent
    receipts = []
    async def hook(name, **kwargs):
        if name == "gateway_delivery_receipt":
            receipts.append(kwargs["receipt"])
        return []
    monkeypatch.setattr(sdk, "ainvoke_hook", hook)
    async def handler(event):
        api.bind_delivery_session("reset-successor")
        return "new session"
    async def send(**kwargs):
        await api.report_delivery("1", ("500",))
        from gateway.platforms.base import SendResult
        return SendResult(success=True, message_id="500")
    adapter._message_handler = handler
    adapter._send_with_retry = send
    source = SessionSource(Platform.DISCORD, "1", message_id="123", conversation_lane="a")
    await adapter._dispatch_inline_reply(MessageEvent(text="/new", source=source))
    assert len(receipts) == 1
    assert receipts[0].session_id == "reset-successor"


def test_sdk_declares_live_service_hooks_as_in_process_contracts():
    from hermes_cli.plugin_isolation import HOST_DEGRADED_HOOKS
    assert "gateway_session_route" in HOST_DEGRADED_HOOKS
    assert "gateway_message_admission" in HOST_DEGRADED_HOOKS
    assert "gateway_delivery_receipt" in HOST_DEGRADED_HOOKS


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["send_document", "send_video", "send_image_file", "send_multiple_images", "send_voice", "send_voice_native", "send_image", "send_animation"])
async def test_native_media_replies_also_emit_receipts(adapter, monkeypatch, tmp_path, method):
    import gateway.conversation_plugins as api
    import hermes_cli.plugins as sdk
    from types import SimpleNamespace
    seen = []
    async def hook(name, **kwargs):
        if name == "gateway_delivery_receipt":
            seen.extend(kwargs["receipt"].message_ids)
        return []
    monkeypatch.setattr(sdk, "ainvoke_hook", hook)
    path = tmp_path / "attachment.bin"
    path.write_bytes(b"test attachment")
    channel = SimpleNamespace(id=1, send=AsyncMock(return_value=SimpleNamespace(id=500, attachments=[object()])))
    adapter._resolve_channel = AsyncMock(return_value=channel)
    adapter._is_forum_parent = lambda c: False
    adapter._client.http = SimpleNamespace(request=AsyncMock(side_effect=RuntimeError("use file fallback")))
    if method == "send_voice_native":
        adapter._client.http.request = AsyncMock(return_value={"id": "500"})
    import plugins.platforms.discord.adapter as discord_adapter
    monkeypatch.setattr(discord_adapter, "_read_url_image_with_redirect_guard", AsyncMock(return_value=(200, b"image", {"content-type": "image/png"})))
    source = SessionSource(Platform.DISCORD, "1", message_id="123")
    with api.delivery_scope(source, "key", "sid"):
        args = [(path.as_uri(), "caption")] if method == "send_multiple_images" else str(path)
        if method in {"send_image", "send_animation"}:
            args = "https://example.org/image.png"
        result = await getattr(adapter, "send_voice" if method == "send_voice_native" else method)("1", args)
    assert result.success
    assert seen == ["500"]


@pytest.mark.asyncio
async def test_native_interaction_routes_before_busy_guard(adapter, monkeypatch):
    from gateway.platforms.base import BasePlatformAdapter, MessageEvent
    from types import SimpleNamespace
    import hermes_cli.plugins as sdk
    source = SessionSource(Platform.DISCORD, "1", chat_type="group")
    adapter.config.extra["group_sessions_per_user"] = False
    calls = []
    def history(**kwargs):
        calls.append(kwargs)
        async def items():
            yield SimpleNamespace(id=100)
        return items()
    interaction = SimpleNamespace(id=123, channel=SimpleNamespace(id=1, history=history))
    async def hook(name, **kwargs):
        if name == "gateway_session_route":
            assert await kwargs["services"].preceding_message_ids() == ("100",)
            assert kwargs["context"].source.message_id == "123"
            return [{"lane": "native-command-lane"}]
        return []
    monkeypatch.setattr(sdk, "ainvoke_hook", hook)
    claimed = []
    adapter._message_handler = AsyncMock()
    adapter._start_session_processing = lambda event, key: claimed.append(key) or True
    await BasePlatformAdapter.handle_message(adapter, MessageEvent(text="/new", source=source, raw_message=interaction))
    assert source.conversation_lane == "native-command-lane"
    assert claimed == [build_session_key(source, False)]
    assert calls[0]["before"] is interaction
