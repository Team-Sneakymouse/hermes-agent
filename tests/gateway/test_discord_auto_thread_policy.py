"""Channel policy through installed plugin discovery and actual Discord ingress."""
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from dataclasses import FrozenInstanceError

import pytest
import discord  # Load the real SDK before the shared fake-transport fixture.

from tests.gateway.test_discord_free_response import (
    adapter, make_message, FakeTextChannel, FakeDMChannel, FakeThread,
)
from gateway.conversation_plugins import AutoThreadContext, auto_thread_preference
from gateway.session import SessionSource
from gateway.config import Platform


@pytest.fixture
def installed_policy(tmp_path, monkeypatch):
    import hermes_cli.plugins as sdk
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    home = tmp_path / "home"
    root = Path(__file__).resolve().parents[2]
    shutil.copytree(root / "custom_plugins/discord-conversations", home / "plugins/discord-conversations")
    (home / "config.yaml").write_text(
        "plugins:\n  enabled: [discord-conversations]\n  entries:\n    discord-conversations:\n      settings:\n"
        "        recent_channel_sessions: false\n        auto_thread_channels: ['1556915401709850704']\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    token = set_hermes_home_override(home)
    manager = sdk.PluginManager()
    manager.discover_and_load()
    assert manager.has_hook("gateway_auto_thread")
    monkeypatch.setattr(sdk, "ainvoke_hook", manager.ainvoke_hook)
    try:
        yield manager
    finally:
        reset_hermes_home_override(token)


@pytest.mark.asyncio
async def test_installed_allowlist_suppresses_other_channels_even_with_global_true(adapter, installed_policy):
    adapter.config.extra.update(auto_thread=True, history_backfill=False)
    adapter._auto_create_thread = AsyncMock(return_value=None)
    message = make_message(channel=FakeTextChannel(), content="<@999> hello")
    assert await adapter._handle_message(message)
    adapter._auto_create_thread.assert_not_awaited()
    assert adapter.handle_message.call_args.args[0].source.chat_type == "group"


def test_auto_thread_hook_reports_host_degradation(tmp_path):
    from hermes_cli.plugin_isolation_audit import audit_plugin_dir
    (tmp_path / "__init__.py").write_text(
        'def register(ctx):\n    ctx.register_hook("gateway_auto_thread", lambda **kw: True)\n'
    )
    report = audit_plugin_dir(tmp_path)
    assert any("gateway_auto_thread" in note and "placeholders" in note for note in report.notes)


@pytest.mark.asyncio
@pytest.mark.parametrize("decisions", [[1], ["true"], [{"action": "block"}], [True, False]])
async def test_invalid_or_conflicting_preferences_fail_closed(monkeypatch, decisions):
    import hermes_cli.plugins as sdk
    monkeypatch.setattr(sdk, "ainvoke_hook", AsyncMock(return_value=decisions))
    with pytest.raises(ValueError):
        await auto_thread_preference(AutoThreadContext("discord", "1", None, True),
                                     SessionSource(Platform.DISCORD, "1"))


@pytest.mark.asyncio
async def test_callback_error_does_not_fall_back_to_native_true(monkeypatch):
    import hermes_cli.plugins as sdk
    manager = sdk.PluginManager()
    ctx = sdk.PluginContext(SimpleNamespace(name="broken-policy", key="broken-policy"), manager)
    def broken(**kwargs):
        raise RuntimeError("broken policy")
    ctx.register_hook("gateway_auto_thread", broken)
    monkeypatch.setattr(sdk, "ainvoke_hook", manager.ainvoke_hook)
    with pytest.raises(ValueError):
        await auto_thread_preference(AutoThreadContext("discord", "1", None, True),
                                     SessionSource(Platform.DISCORD, "1"))


@pytest.mark.asyncio
async def test_allowlisted_parent_uses_native_thread_routing_and_dedup_with_global_false(adapter, installed_policy):
    adapter.config.extra.update(auto_thread=False, history_backfill=False)
    channel = FakeTextChannel(channel_id=1556915401709850704)
    thread = FakeThread(channel_id=888, parent=channel)
    adapter._auto_create_thread = AsyncMock(return_value=thread)
    adapter._threads.mark_async = AsyncMock()
    assert await adapter._handle_message(make_message(channel=channel, content="<@999> hello"))
    adapter._auto_create_thread.assert_awaited_once()
    source = adapter.handle_message.call_args.args[0].source
    assert source.chat_id == "888"
    assert source.thread_id == "888"
    assert source.parent_chat_id == "1556915401709850704"
    assert source.auto_thread_created
    assert adapter._dedup.contains("888")
    adapter._threads.mark_async.assert_any_await("888")


@pytest.mark.asyncio
@pytest.mark.parametrize("excluded", ["dm", "thread", "reply", "voice", "no_thread", "free"])
async def test_allowlist_cannot_override_native_safety_exclusions(adapter, installed_policy, excluded):
    adapter.config.extra.update(auto_thread=False, history_backfill=False)
    channel = FakeTextChannel(channel_id=1556915401709850704)
    if excluded == "dm":
        channel = FakeDMChannel(channel_id=channel.id)
    elif excluded == "thread":
        channel = FakeThread(channel_id=channel.id, parent=FakeTextChannel())
    elif excluded == "voice":
        adapter._voice_text_channels[1] = channel.id
        adapter.config.extra["free_response_auto_thread"] = True
    elif excluded == "no_thread":
        adapter.config.extra["no_thread_channels"] = [str(channel.id)]
    elif excluded == "free":
        adapter.config.extra["free_response_channels"] = [str(channel.id)]
    adapter._auto_create_thread = AsyncMock(return_value=None)
    message = make_message(channel=channel, content="<@999> hello",
                           msg_type=discord.MessageType.reply if excluded == "reply" else None)
    assert await adapter._handle_message(message)
    adapter._auto_create_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_allowlist_does_not_admit_implicit_mentions(adapter, installed_policy):
    adapter._auto_create_thread = AsyncMock()
    message = make_message(channel=FakeTextChannel(channel_id=1556915401709850704),
                           content="implicit reply", mentions=[adapter._client.user])
    assert not await adapter._handle_message(message)
    adapter._auto_create_thread.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_allowlisted_creation_failure_never_dispatches_inline(adapter, installed_policy):
    adapter.config.extra.update(auto_thread=False, history_backfill=False)
    channel = FakeTextChannel(channel_id=1556915401709850704)
    channel.send = AsyncMock()
    adapter._auto_create_thread = AsyncMock(return_value=None)
    assert not await adapter._handle_message(make_message(channel=channel, content="<@999> hello"))
    channel.send.assert_awaited_once()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("default", [False, True])
async def test_none_preserves_native_preference_and_context_is_immutable(monkeypatch, default):
    import hermes_cli.plugins as sdk
    manager = sdk.PluginManager()
    ctx = sdk.PluginContext(SimpleNamespace(name="observer", key="observer"), manager)
    def observe(context):
        with pytest.raises(FrozenInstanceError):
            context.channel_id = "other"
        return None
    ctx.register_hook("gateway_auto_thread", observe)
    monkeypatch.setattr(sdk, "ainvoke_hook", manager.ainvoke_hook)
    assert await auto_thread_preference(AutoThreadContext("discord", "1", None, default),
                                        SessionSource(Platform.DISCORD, "1")) is default


@pytest.mark.parametrize("channels", ["1556915401709850704", [1556915401709850704], ["*"], [""], {}])
def test_malformed_allowlist_rejected_at_registration(channels):
    from tests.gateway.test_discord_conversation_plugin import load_plugin
    ctx = SimpleNamespace(get_config=lambda key, default=None: channels if key == "auto_thread_channels" else default,
                          register_hook=lambda *args: None)
    with pytest.raises(ValueError, match="auto_thread_channels"):
        load_plugin().register(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize("plugin_enabled", [False, True])
@pytest.mark.parametrize("default", [False, True])
@pytest.mark.parametrize("allowlist", [None, []], ids=["null", "empty"])
async def test_absent_allowlist_retains_native_ingress_behavior(adapter, monkeypatch, plugin_enabled, default, allowlist):
    import hermes_cli.plugins as sdk
    from tests.gateway.test_discord_conversation_plugin import load_plugin
    manager = sdk.PluginManager()
    if plugin_enabled:
        ctx = sdk.PluginContext(SimpleNamespace(name="discord-conversations", key="discord-conversations"), manager)
        monkeypatch.setattr(ctx, "get_config", lambda key, fallback=None: {
            "recent_channel_sessions": False, "auto_thread_channels": allowlist,
        }.get(key, fallback))
        load_plugin().register(ctx)
    assert manager.has_hook("gateway_auto_thread") is (plugin_enabled and allowlist == [])
    monkeypatch.setattr(sdk, "ainvoke_hook", manager.ainvoke_hook)
    adapter.config.extra.update(auto_thread=default, history_backfill=False)
    channel = FakeTextChannel()
    channel.send = AsyncMock()
    adapter._auto_create_thread = AsyncMock(return_value=None)
    result = await adapter._handle_message(make_message(channel=channel, content="<@999> hello"))
    expected = default and not (plugin_enabled and allowlist == [])
    assert result is not expected  # Creation failure skips dispatch; disabled threads dispatch inline.
    assert adapter._auto_create_thread.await_count == int(expected)


@pytest.mark.asyncio
async def test_preference_uses_routed_profile_and_restores_ambient_scope(monkeypatch, tmp_path):
    import hermes_cli.plugins as sdk
    from hermes_constants import get_hermes_home
    from gateway.session_identity import RoutingIdentity
    ambient = get_hermes_home()
    routed = tmp_path / "routed"
    source = SessionSource(Platform.DISCORD, "1", profile="routed")
    source._identity = RoutingIdentity("default", "routed", ambient, routed)
    ambient_manager = sdk.PluginManager(scope_key=ambient.resolve())
    routed_manager = sdk.PluginManager(scope_key=routed.resolve())
    calls = []
    def register(manager, decision):
        ctx = sdk.PluginContext(SimpleNamespace(name="policy", key="policy"), manager)
        def hook(context):
            calls.append((get_hermes_home(), context.profile))
            return decision
        ctx.register_hook("gateway_auto_thread", hook)
    register(ambient_manager, False)
    register(routed_manager, True)
    monkeypatch.setattr(sdk, "_plugin_manager", ambient_manager)
    monkeypatch.setattr(sdk, "_plugin_managers_by_home", {
        ambient.resolve(): ambient_manager, routed.resolve(): routed_manager,
    })
    assert await auto_thread_preference(AutoThreadContext("discord", "1", "routed", False), source)
    assert calls == [(routed, "routed")]
    assert get_hermes_home() == ambient
    assert sdk.get_plugin_manager() is ambient_manager


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True], ids=["async", "sync"])
async def test_timeout_fails_closed(monkeypatch, synchronous):
    import asyncio
    import threading
    import hermes_cli.plugins as sdk
    manager = sdk.PluginManager()
    ctx = sdk.PluginContext(SimpleNamespace(name="slow", key="slow"), manager)
    async def slow(context):
        await asyncio.Event().wait()
    release = threading.Event()
    def slow_sync(context):
        release.wait(timeout=2)
        return True
    ctx.register_hook("gateway_auto_thread", slow_sync if synchronous else slow)
    monkeypatch.setattr(sdk, "_resolve_hook_callback_timeout", lambda: 0.01)
    monkeypatch.setattr(sdk, "ainvoke_hook", manager.ainvoke_hook)
    try:
        with pytest.raises(ValueError):
            if synchronous:
                # The synchronous dispatcher bounds sync callbacks; ainvoke_hook runs
                # sync callbacks inline by contract and only bounds awaitables.
                results = manager.invoke_hook("gateway_auto_thread", context=AutoThreadContext("discord", "1", None, True))
                monkeypatch.setattr(sdk, "ainvoke_hook", AsyncMock(return_value=results))
            await auto_thread_preference(AutoThreadContext("discord", "1", None, True),
                                         SessionSource(Platform.DISCORD, "1"))
    finally:
        release.set()
