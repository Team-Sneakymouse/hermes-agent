"""Installed host integration, recording Discord transport and model boundary only.

No real Discord message or inference request is made. The real PluginContext,
GatewayRunner, BasePlatformAdapter, session store and transcript path run.
"""
import asyncio
import importlib.util
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import hermes_bootstrap
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from gateway.run import GatewayRunner
from gateway.profile_routing import parse_profile_routes
from hermes_constants import get_hermes_home
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

HERE = Path(__file__).parent
USER = "333333333333333333"
THREAD = "111111111111111111"
PARENT = "1560943737688039474"
GUILD = "555555555555555555"

class RecordingDiscord(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="fixture-not-a-credential"), Platform.DISCORD)
        self.sent = []
        self._running = True
    async def connect(self, *, is_reconnect=False):
        return True
    async def disconnect(self):
        self._mark_disconnected()
    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content))
        return SendResult(success=True, message_id=str(len(self.sent)))
    async def send_typing(self, chat_id, metadata=None):
        return None
    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "thread"}

class HandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_forum_route_to_coding_profile_then_human_reply_uses_same_durable_session(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            coding_home = home / "profiles" / "coding"
            coding_home.mkdir(parents=True)
            (coding_home / "config.yaml").write_text("plugins:\n  entries:\n    discord-todo-assessment:\n      allow_gateway_injection: true\n")
            (home / ".env").write_text(f"DISCORD_ALLOWED_USERS={USER}\n")
            with patch.dict(os.environ, {"HERMES_HOME": str(home), "DISCORD_ALLOWED_USERS": USER}), \
                 patch("gateway.run._hermes_home", home), \
                 patch("hermes_cli.profiles.profiles_to_serve", return_value=[("default", home), ("coding", coding_home)]), \
                 patch("hermes_cli.profiles.get_profile_dir", side_effect=lambda name: coding_home if name == "coding" else home), \
                 patch("hermes_cli.profiles.profile_exists", return_value=True):
                config = GatewayConfig(multiplex_profiles=True, sessions_dir=home / "sessions")
                config.profile_routes = parse_profile_routes([{
                    "name": "todo-forum", "platform": "discord", "profile": "coding",
                    "chat_id": PARENT, "guild_id": GUILD}])
                runner = GatewayRunner(config)
                adapter = RecordingDiscord()
                runner.adapters = {Platform.DISCORD: adapter}
                runner._profile_adapters = {"coding": {Platform.DISCORD: adapter}}
                adapter.gateway_runner = runner
                adapter.set_message_handler(runner._handle_message)
                turns = []
                async def model_boundary(message, context_prompt, history, source, session_id, **kwargs):
                    turns.append(SimpleNamespace(message=message, history=list(history), session_id=session_id, home=get_hermes_home(), source=source))
                    reply = f"fixture-reply-{len(turns)}"
                    messages = list(history) + [{"role": "user", "content": message},
                                                {"role": "assistant", "content": reply}]
                    return {"final_response": reply, "messages": messages, "tools": [],
                            "agent_persisted": False,
                            "history_offset": len(history), "last_prompt_tokens": 0}
                # Fixture model does not flush SQLite itself: agent_persisted=False above
                # delegates durable user/assistant writes to the REAL gateway persistence path.
                runner._run_agent_inner = model_boundary
                runner._gateway_loop = asyncio.get_running_loop()
                runner._running = True
                manager = PluginManager(scope_key=str(coding_home))
                ctx = PluginContext(PluginManifest(name="discord-todo-assessment", key="discord-todo-assessment", source="user"), manager)
                spec = importlib.util.spec_from_file_location("todo_bridge", HERE / "plugin.py")
                assert spec is not None and spec.loader is not None
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                async def fetch_thread(thread_id):
                    return SimpleNamespace(id=int(thread_id), parent_id=int(PARENT), guild=SimpleNamespace(id=int(GUILD)))
                async def settle():
                    async with asyncio.timeout(10):
                        while runner._background_tasks or adapter._session_tasks or adapter._active_sessions:
                            await asyncio.sleep(0.01)
                with patch("hermes_cli.plugins._known_plugin_managers", return_value=[manager]):
                    runner._install_plugin_message_injector()
                    try:
                        result = await module.start(ctx, {"thread_id": THREAD, "user_id": USER,
                            "context": "Todo 42: investigate failing login; create a PR, do not deploy."},
                            fetch_thread=fetch_thread, allowed_parents=[PARENT], state_path=home / "starts.sqlite3")
                        self.assertEqual(result["status"], "accepted")
                        await settle()
                        self.assertEqual(len(turns), 1)
                        self.assertEqual(turns[0].history, [])
                        self.assertEqual(turns[0].home, coding_home)
                        self.assertEqual(turns[0].source.profile, "coding")
                        self.assertEqual(turns[0].source.guild_id, GUILD)
                        self.assertEqual(turns[0].source.scope_id, GUILD)
                        self.assertEqual(turns[0].source.parent_chat_id, PARENT)
                        self.assertIn("Todo 42: investigate failing login", turns[0].message)
                        self.assertEqual(adapter.sent, [(THREAD, "fixture-reply-1")])
                        source = adapter.build_source(chat_id=THREAD, chat_type="thread", thread_id=THREAD, user_id=USER,
                            parent_chat_id=PARENT, guild_id=GUILD, scope_id=GUILD)
                        await adapter.handle_message(MessageEvent(text="Human reply: add a regression test.", source=source))
                        await settle()
                        self.assertEqual(len(turns), 2)
                        self.assertEqual(turns[0].session_id, turns[1].session_id)
                        self.assertEqual(turns[1].home, coding_home)
                        self.assertIn("Todo 42: investigate failing login", str(turns[1].history))
                        persisted = runner.session_store.load_transcript(turns[1].session_id)
                        self.assertIn("Human reply: add a regression test.", str(persisted))
                        self.assertIn("Todo 42: investigate failing login", str(persisted))
                        self.assertIn("Human reply: add a regression test.", turns[1].message)
                        self.assertEqual(adapter.sent[-1], (THREAD, "fixture-reply-2"))
                        again = await module.start(ctx, {"thread_id": THREAD, "user_id": USER,
                            "context": "Todo 42: investigate failing login; create a PR, do not deploy."},
                            fetch_thread=fetch_thread, allowed_parents=[PARENT], state_path=home / "starts.sqlite3")
                        self.assertEqual(again["status"], "accepted")
                        await settle()
                        self.assertEqual(len(turns), 2, "a duplicate request must not run another turn")
                        next_thread = "444444444444444444"
                        second = await module.start(ctx, {"thread_id": next_thread, "user_id": USER,
                            "context": "Todo 43: implement a different feature."},
                            fetch_thread=fetch_thread, allowed_parents=[PARENT], state_path=home / "starts.sqlite3")
                        self.assertEqual(second["status"], "accepted")
                        await settle()
                        self.assertEqual(len(turns), 3)
                        self.assertNotEqual(turns[2].session_id, turns[0].session_id)
                        self.assertEqual(turns[2].history, [])
                        self.assertEqual(adapter.sent[-1], (next_thread, "fixture-reply-3"))
                    finally:
                        runner._clear_plugin_message_injector()
                        runner._running = False
                        if runner._session_db:
                            await runner._session_db.close()

if __name__ == "__main__":
    unittest.main()
