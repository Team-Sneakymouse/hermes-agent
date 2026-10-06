import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from gateway.conversation_plugins import AdmissionContext, RouteContext, RoutingServices
from gateway.session import SessionSource, build_session_key
from gateway.config import Platform


def load_plugin():
    path = Path(__file__).resolve().parents[2] / "custom_plugins/discord-conversations/plugin.py"
    assert path.exists(), "versioned conversation plugin is missing"
    spec = importlib.util.spec_from_file_location("discord_conversation_plugin_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("content,kind,bot,expected", [
    ("reply metadata only", "group", False, "block"),
    ("<@999> hello", "group", False, "allow"),
    ("<@!999> hello", "group", True, "allow"),
    ("<@9990> hello", "group", False, "block"),
    ("reply metadata only", "thread", True, "block"),
    ("hello", "dm", False, "allow"),
])
def test_literal_admission(content, kind, bot, expected):
    plugin = load_plugin()
    context = AdmissionContext("discord", content, "999", "1", kind, "10", "42", bot, None)
    assert plugin.admission(context)["action"] == expected


@pytest.mark.asyncio
async def test_latest_associated_invocation_wins_and_empty_window_starts_new_lane(tmp_path):
    plugin = load_plugin()
    assert hasattr(plugin, "ConversationPolicy"), "durable routing policy is missing"
    policy = plugin.ConversationPolicy(tmp_path / "index.sqlite3")
    source = SessionSource(Platform.DISCORD, "1", chat_type="group", message_id="10")
    async def route(mid, ids):
        async def history():
            return tuple(ids)
        context = RouteContext(replace_source(source, message_id=mid), "<@999> hello", build_session_key(source, False), True)
        return (await policy.route(context, RoutingServices(history)))["lane"]
    first = await route("10", [])
    second = await route("20", ["19", "18"])
    assert first != second
    # An unrelated message counts in the window but does not hide an older association.
    assert await route("30", ["29", "20", "10"]) == second
    # Restart: associations survive reopening the plugin's own DB.
    policy = plugin.ConversationPolicy(tmp_path / "index.sqlite3")
    assert await route("40", ["39", "10"]) == first


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,shared,platform", [("dm", True, Platform.DISCORD), ("thread", True, Platform.DISCORD), ("group", False, Platform.DISCORD), ("group", True, Platform.SLACK)])
async def test_non_shared_normal_discord_channels_keep_builtin_routing(tmp_path, kind, shared, platform):
    plugin = load_plugin()
    policy = plugin.ConversationPolicy(tmp_path / "index.sqlite3")
    source = SessionSource(platform, "1", chat_type=kind, message_id="10")
    async def history():
        pytest.fail("persistent channel must not query recent window")
    assert await policy.route(RouteContext(source, "", "key", shared), RoutingServices(history)) is None


@pytest.mark.asyncio
async def test_delivered_replies_rejoin_lane_and_reset_fences_late_old_replies(tmp_path):
    plugin = load_plugin()
    policy = plugin.ConversationPolicy(tmp_path / "index.sqlite3")
    source = SessionSource(Platform.DISCORD, "1", chat_type="group", message_id="10")
    base = build_session_key(source, False)
    async def route(mid, ids):
        async def history():
            return tuple(ids)
        return (await policy.route(RouteContext(replace_source(source, message_id=mid), "", base, True), RoutingServices(history)))["lane"]
    lane = await route("10", [])
    assert hasattr(policy, "delivered"), "receipt persistence is missing"
    receipt = SimpleNamespace(source=replace_source(source, conversation_lane=lane), base_session_key=base,
                              channel_id="1", origin_message_id="10", message_ids=("50", "51"), session_id="old")
    policy.delivered(receipt)
    assert await route("60", ["59", "51"]) == lane
    reset_source = replace_source(source, message_id="70", conversation_lane=lane)
    await route("70", ["60"])
    assert hasattr(policy, "reset"), "reset boundary is missing"
    policy.reset(source=reset_source, base_session_key=base, new_session_id="new")
    # An old in-flight send completes after /new: it cannot resurrect erased history.
    policy.delivered(SimpleNamespace(**{**receipt.__dict__, "message_ids": ("80",)}))
    assert await route("90", ["80", "51", "10"]) != lane
    assert await route("100", ["70"]) == lane


def test_plugin_passes_actual_doctor_validation(tmp_path):
    import os
    import subprocess

    root = Path(__file__).resolve().parents[2]
    home = tmp_path / "doctor-home"
    home.mkdir()
    bundled = tmp_path / "bundled-plugins"
    bundled.mkdir()
    result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "plugins", "doctor",
         str(root / "custom_plugins/discord-conversations"), "--ci"],
        cwd=root,
        env={**os.environ, "HERMES_HOME": str(home),
             "HERMES_BUNDLED_PLUGINS": str(bundled),
             "HERMES_ENABLE_PROJECT_PLUGINS": "0"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK: runtime discovery" in result.stdout, result.stdout
    assert "registrations: 0 tool(s), 4 hook(s)" in result.stdout, result.stdout


@pytest.mark.asyncio
async def test_plugin_install_loads_and_registers_validated_hooks(tmp_path, monkeypatch):
    import shutil
    from hermes_cli.plugins import PluginManager
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    src = Path(__file__).resolve().parents[2] / "custom_plugins/discord-conversations"
    assert (src / "plugin.yaml").exists(), "installable manifest is missing"
    home = tmp_path / "home"
    shutil.copytree(src, home / "plugins/discord-conversations")
    (home / "config.yaml").write_text("plugins:\n  enabled: [discord-conversations]\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    token = set_hermes_home_override(home)
    try:
        manager = PluginManager()
        manager.discover_and_load()
        for name in ("gateway_message_admission", "gateway_session_route", "gateway_delivery_receipt", "on_session_reset"):
            assert manager.has_hook(name), f"plugin did not register {name}"
        context = AdmissionContext("discord", "implicit reply", "999", "1", "group", "10", "42", False, None)
        assert await manager.ainvoke_hook("gateway_message_admission", context=context) == [{"action": "block"}]
    finally:
        reset_hermes_home_override(token)


@pytest.mark.asyncio
async def test_real_session_store_retains_transcript_and_compression_tip_across_restart(tmp_path):
    from gateway.session import SessionStore
    from gateway.config import GatewayConfig
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB
    home = get_hermes_home()
    policy = load_plugin().ConversationPolicy(tmp_path / "index.sqlite3")
    source = SessionSource(Platform.DISCORD, "1", chat_type="group", message_id="10")
    base = build_session_key(source, False)
    async def history():
        return ()
    lane = (await policy.route(RouteContext(source, "", base, True), RoutingServices(history)))["lane"]
    routed = replace_source(source, conversation_lane=lane)
    store = SessionStore(home / "sessions", GatewayConfig(group_sessions_per_user=False))
    try:
        entry = store.get_or_create_session(routed)
        parent = entry.session_id
        store.append_to_transcript(parent, {"role": "user", "content": "retained history"})
        assert store.load_transcript(parent)[0]["content"] == "retained history"
        db = SessionDB(db_path=home / "state.db")
        try:
            db.end_session(parent, "compression")
            db.create_session("compression-tip", source="discord", parent_session_id=parent)
            db.replace_messages("compression-tip", [{"role": "user", "content": "retained compressed history"}])
        finally:
            db.close()
        async def prior():
            return ("irrelevant", "10")
        resumed_lane = (await policy.route(RouteContext(replace_source(source, message_id="20"), "", base, True), RoutingServices(prior)))["lane"]
        resumed = store.get_or_create_session(replace_source(routed, message_id="20", conversation_lane=resumed_lane))
        assert resumed.session_id == "compression-tip"
        assert store.load_transcript(resumed.session_id)[0]["content"] == "retained compressed history"
    finally:
        store.close_all_db_handles()
    restarted = SessionStore(home / "sessions", GatewayConfig(group_sessions_per_user=False))
    try:
        assert restarted.get_or_create_session(routed).session_id == "compression-tip"
    finally:
        restarted.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("pause_at", ["finalize", "observer"])
async def test_reset_boundary_preserves_following_origin_and_fences_old_reply(tmp_path, monkeypatch, pause_at):
    import asyncio
    from unittest.mock import AsyncMock
    from gateway.conversation_plugins import select_conversation_lane, DeliveryReceipt
    from gateway.session import SessionStore
    from gateway.config import GatewayConfig
    from gateway.platforms.event import MessageEvent
    from hermes_constants import get_hermes_home
    from tests.gateway.test_session_model_reset import _make_runner
    import hermes_cli.plugins as sdk
    import hermes_cli.lifecycle as lifecycle
    policy = load_plugin().ConversationPolicy(tmp_path / "index.sqlite3")
    source = SessionSource(Platform.DISCORD, "1", user_id="42", chat_type="group", message_id="10")
    base = build_session_key(source, False)
    paused, release = asyncio.Event(), asyncio.Event()
    observed = []
    monkeypatch.setattr(lifecycle, "_observe", lambda name, **kw: observed.append(name))
    async def delayed(*args, **kwargs):
        paused.set()
        await release.wait()
    async def hook(name, **kwargs):
        if name == "gateway_session_route":
            return [await policy.route(kwargs["context"], kwargs["services"])]
        if name == "on_session_reset":
            if pause_at == "observer":
                await delayed()
            policy.reset(**kwargs)
        return []
    monkeypatch.setattr(sdk, "ainvoke_hook", hook)
    async def route(mid, ids):
        async def history():
            return tuple(ids)
        invocation = replace_source(source, message_id=mid)
        await select_conversation_lane(RouteContext(invocation, "", base, True), RoutingServices(history), invocation)
        return invocation
    runner = _make_runner()
    runner.config = GatewayConfig(group_sessions_per_user=False)
    store = SessionStore(get_hermes_home() / "sessions", runner.config)
    runner.session_store = store
    runner._cleanup_old_agent_for_reset = AsyncMock()
    runner._fire_session_reset_hooks = AsyncMock(side_effect=delayed if pause_at == "finalize" else None)
    runner._reset_notice_session_info = lambda source: ""
    runner._telegram_topic_new_header = lambda source: None
    runner._is_telegram_topic_lane = lambda source: False
    reset_task = follow_task = None
    try:
        old_source = await route("10", [])
        old = store.get_or_create_session(old_source)
        old_sid = old.session_id
        store.append_to_transcript(old_sid, {"role": "user", "content": "old"})
        reset_source = await route("20", ["10"])
        key = build_session_key(reset_source, False)
        reset_task = asyncio.create_task(runner._handle_reset_command(MessageEvent(text="/new", source=reset_source, message_id="20")))
        await asyncio.wait_for(paused.wait(), timeout=3)
        # Core reset is real and already committed; its delayed observer has not run.
        assert store.get_or_create_session(reset_source).session_id != old_sid
        follow_task = asyncio.create_task(route("30", ["20"]))
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await checkpoint.wait()
        routed_before_boundary = follow_task.done()
        release.set()
        await reset_task
        following = await follow_task
        assert following.conversation_lane == reset_source.conversation_lane
        current = store.get_or_create_session(following)
        policy.delivered(DeliveryReceipt(following, key, current.session_id, "30", "1", None, ("40",), "send"))
        policy.delivered(DeliveryReceipt(old_source, key, old_sid, "10", "1", None, ("50",), "send"))
        with policy.database() as db:
            assert db.execute("SELECT lane FROM associations WHERE scope=? AND message_id='40'", (base,)).fetchone() == (following.conversation_lane,)
            assert db.execute("SELECT lane FROM associations WHERE scope=? AND message_id='50'", (base,)).fetchone() is None
        assert not routed_before_boundary
        assert "on_session_reset" in observed
    finally:
        release.set()
        for task in (reset_task, follow_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_overlapping_routes_publish_in_ingress_order_per_scope(tmp_path):
    import asyncio
    policy = load_plugin().ConversationPolicy(tmp_path / "index.sqlite3")
    source = SessionSource(Platform.DISCORD, "1", chat_type="group", message_id="10")
    base = build_session_key(source, False)
    entered = asyncio.Event()
    release = asyncio.Event()
    second_history_entered = asyncio.Event()
    async def first_history():
        entered.set()
        await release.wait()
        return ()
    async def second_history():
        second_history_entered.set()
        return ("10",)
    first = asyncio.create_task(policy.route(RouteContext(source, "", base, True), RoutingServices(first_history)))
    await entered.wait()
    second = asyncio.create_task(policy.route(RouteContext(replace_source(source, message_id="20"), "", base, True), RoutingServices(second_history)))
    # FIFO ready-queue barrier: second has attempted ingress before A's release.
    checkpoint = asyncio.Event()
    asyncio.get_running_loop().call_soon(checkpoint.set)
    await checkpoint.wait()
    overlapped = second_history_entered.is_set()
    # A blocked scope must not block another profile namespace.
    async def independent_history():
        return ()
    other = await asyncio.wait_for(policy.route(
        RouteContext(replace_source(source, message_id="30", profile="other"), "", "agent:other:discord:group:1", True),
        RoutingServices(independent_history)), timeout=1)
    release.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert second_result["lane"] == first_result["lane"]
    assert not overlapped
    assert other["lane"] != first_result["lane"]


@pytest.mark.asyncio
async def test_new_lane_after_unrelated_window_has_fresh_real_transcript(tmp_path):
    from gateway.session import SessionStore
    from gateway.config import GatewayConfig
    from hermes_constants import get_hermes_home
    policy = load_plugin().ConversationPolicy(tmp_path / "index.sqlite3")
    source = SessionSource(Platform.DISCORD, "1", user_id="42", chat_type="group", message_id="10")
    base = build_session_key(source, False)
    async def route(mid, ids):
        async def history():
            return tuple(ids)
        invocation = replace_source(source, message_id=mid)
        lane = (await policy.route(RouteContext(invocation, "", base, True), RoutingServices(history)))["lane"]
        return replace_source(invocation, conversation_lane=lane)
    store = SessionStore(get_hermes_home() / "sessions", GatewayConfig(group_sessions_per_user=False))
    try:
        first_source = await route("10", [])
        first = store.get_or_create_session(first_source)
        store.append_to_transcript(first.session_id, {"role": "user", "content": "must not leak"})
        second_source = await route("40", [str(mid) for mid in range(39, 19, -1)])
        assert second_source.conversation_lane != first_source.conversation_lane
        second = store.get_or_create_session(second_source)
        assert second.session_id != first.session_id
        assert store.load_transcript(second.session_id) == []
    finally:
        store.close_all_db_handles()


def test_legacy_non_gateway_reset_payload_is_ignored(tmp_path):
    policy = load_plugin().ConversationPolicy(tmp_path / "index.sqlite3")
    assert policy.reset(new_session_id="cli-successor") is None


def replace_source(source, **kwargs):
    from dataclasses import replace
    return replace(source, **kwargs)
