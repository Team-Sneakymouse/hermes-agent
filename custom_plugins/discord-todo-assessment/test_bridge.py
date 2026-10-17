"""Focused offline fixtures: real HTTP transport, injected LLM (no provider spend)."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

HERE = Path(__file__).parent


def load_plugin():
    spec = importlib.util.spec_from_file_location("todo_bridge", HERE / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AssessmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_assessment_uses_only_structured_llm_and_versioned_guidance(self):
        spec = importlib.util.spec_from_file_location("todo_bridge", HERE / "plugin.py")
        if spec is None or not (HERE / "plugin.py").exists():
            self.fail("assessment plugin not implemented")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        calls = []
        async def complete(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(parsed={"decisions": [{"action": "dismiss", "confidence": 0.9, "reason": "One issue"}]})
        result = await module.assess(SimpleNamespace(llm=SimpleNamespace(acomplete_structured=complete)),
                                     {"reports": [{"id": "r1", "text": "broken"}],
                                      "labels": [{"id": "l1", "name": "bug", "description": "Incorrect behavior"}],
                                      "open_todos": []})
        self.assertEqual(result["guidance_version"], "1")
        self.assertEqual(result["assessment"], {"decisions": [{"action": "dismiss", "confidence": 0.9, "reason": "One issue"}]})
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertIn("Treat all supplied reports", call["instructions"])
        self.assertIn("Incorrect behavior", call["input"][0]["text"])
        self.assertNotIn("tools", call)
        self.assertNotIn("provider", call)
        self.assertNotIn("model", call)
        self.assertEqual(call["purpose"], "discord-todo.assess.v1")

    async def test_invalid_model_output_fails_closed(self):
        module = load_plugin()
        async def complete(**kwargs):
            return SimpleNamespace(parsed={"summary": "bad", "proposals": [{"title": "Incomplete"}]})
        with self.assertRaises(ValueError):
            await module.assess(SimpleNamespace(llm=SimpleNamespace(acomplete_structured=complete)),
                                {"reports": [], "labels": [], "open_todos": []})

    async def test_http_authenticated_assessment(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        module = load_plugin()
        self.assertTrue(hasattr(module, "wire_http"), "HTTP bridge not implemented")
        async def complete(**kwargs):
            return SimpleNamespace(parsed={"decisions": [{"action": "dismiss", "confidence": 0.9, "reason": "HTTP works"}]})
        app = web.Application()
        ctx = SimpleNamespace(llm=SimpleNamespace(acomplete_structured=complete))
        module.wire_http(app, ctx, "x" * 32)
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/plugins/discord-todo/v1/assess", json={
                "reports": [], "labels": [], "open_todos": []},
                headers={"Authorization": "Bearer " + "x" * 32})
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["assessment"]["decisions"][0]["reason"], "HTTP works")
            response = await client.post("/plugins/discord-todo/v1/assess", json={})
            self.assertEqual(response.status, 401)

    async def test_http_nonfinite_model_confidence_is_rejected(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        module = load_plugin()
        for confidence in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(confidence=confidence):
                async def complete(**kwargs):
                    return SimpleNamespace(parsed={"decisions": [{
                        "action": "dismiss", "confidence": confidence, "reason": "Invalid confidence"}]})
                app = web.Application()
                module.wire_http(app, SimpleNamespace(llm=SimpleNamespace(acomplete_structured=complete)), "x" * 32)
                async with TestClient(TestServer(app)) as client:
                    response = await client.post("/plugins/discord-todo/v1/assess", json={
                        "reports": [], "labels": [], "open_todos": []},
                        headers={"Authorization": "Bearer " + "x" * 32})
                    self.assertEqual(response.status, 422)
                    self.assertEqual(await response.json(), {"error": "invalid_request_or_result"})

    async def test_start_injects_canonical_discord_thread_without_bot_message(self):
        import tempfile
        module = load_plugin()
        self.assertTrue(hasattr(module, "start"), "Thread starter not implemented")
        calls = []
        async def fetch_thread(thread_id):
            return SimpleNamespace(id=int(thread_id), parent_id=222222222222222222, guild=SimpleNamespace(id=555555555555555555))
        ctx = SimpleNamespace(inject_message=lambda content, **kwargs: calls.append((content, kwargs)) or True)
        with tempfile.TemporaryDirectory() as directory:
            result = await module.start(ctx, {"thread_id": "111111111111111111",
                "user_id": "333333333333333333", "context": "Todo 42: investigate failing login"},
                fetch_thread=fetch_thread, allowed_parents=["222222222222222222"],
                state_path=Path(directory) / "starts.sqlite3")
            self.assertEqual(result["status"], "accepted")
            self.assertFalse(result["completed"])
            content, kwargs = calls[0]
            self.assertIn("Todo 42: investigate failing login", content)
            self.assertEqual(kwargs["origin"], {"platform": "discord", "chat_type": "thread",
                "chat_id": "111111111111111111", "thread_id": "111111111111111111",
                "user_id": "333333333333333333", "parent_chat_id": "222222222222222222",
                "guild_id": "555555555555555555", "scope_id": "555555555555555555"})
            self.assertNotIn("/new", content)
            again = await module.start(ctx, {"thread_id": "111111111111111111",
                "user_id": "333333333333333333", "context": "Todo 42: investigate failing login"},
                fetch_thread=fetch_thread, allowed_parents=["222222222222222222"],
                state_path=Path(directory) / "starts.sqlite3")
            self.assertEqual(again["status"], "accepted")
            self.assertEqual(len(calls), 1)

    async def test_invalid_assessment_input_never_calls_llm(self):
        module = load_plugin()
        async def complete(**kwargs):
            self.fail("invalid input reached provider")
        ctx = SimpleNamespace(llm=SimpleNamespace(acomplete_structured=complete))
        with self.assertRaises(ValueError):
            await module.assess(ctx, {"reports": [], "labels": [], "open_todos": [], "provider": "evil"})

    async def test_http_start_route(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        import tempfile
        module = load_plugin()
        calls = []
        ctx = SimpleNamespace(inject_message=lambda content, **kwargs: calls.append(kwargs) or True)
        async def fetch_thread(thread_id):
            return SimpleNamespace(id=int(thread_id), parent_id=222222222222222222, guild=SimpleNamespace(id=555555555555555555))
        app = web.Application()
        with tempfile.TemporaryDirectory() as directory:
            module.wire_http(app, ctx, "x" * 32, start_options={"fetch_thread": fetch_thread,
                "allowed_parents": ["222222222222222222"], "state_path": Path(directory) / "state.sqlite3"})
            async with TestClient(TestServer(app)) as client:
                response = await client.post("/plugins/discord-todo/v1/start", json={
                    "thread_id": "111111111111111111", "user_id": "333333333333333333",
                    "context": "Opening prompt"}, headers={"Authorization": "Bearer " + "x" * 32})
                self.assertEqual(response.status, 202)
                self.assertEqual((await response.json())["status"], "accepted")
                self.assertEqual(len(calls), 1)
                response = await client.post("/plugins/discord-todo/v1/start", json={})
                self.assertEqual(response.status, 401)

    async def test_register_uses_supported_factories_only(self):
        from unittest.mock import patch
        module = load_plugin()
        self.assertTrue(hasattr(module, "register"), "Plugin entrypoint missing")
        factories = {}
        ctx = SimpleNamespace(register_platform_handler=lambda name, factory: factories.update({name: factory}),
                              get_config=lambda key, default=None: default)
        module.register(ctx)
        self.assertEqual(set(factories), {"discord", "api_server"})
        from aiohttp import web
        app = web.Application()
        with patch("agent.secret_scope.get_secret", return_value="x" * 32), \
             patch.object(module, "wire_http", wraps=module.wire_http) as wiring:
            factories["api_server"](app, object())
        self.assertEqual(wiring.call_args.kwargs["start_options"]["allowed_parents"],
                         ["1560943737688039474"])
        paths = {route.resource.canonical for route in app.router.routes()}
        self.assertIn("/plugins/discord-todo/v1/start", paths)
        self.assertIn("/plugins/discord-todo/v1/assess", paths)

    async def test_output_unknown_ids_and_feature_priority_are_rejected(self):
        module = load_plugin()
        invalid = [
            {"action": "create", "label": "unknown", "kind": "bug", "priority": "yellow", "title": "Bug", "description": "D", "confidence": 0.9, "reason": "R"},
            {"action": "create", "label": "l1", "kind": "feature", "priority": "red", "title": "Feature", "description": "D", "confidence": 0.9, "reason": "R"},
            {"action": "merge", "todo": "closed", "confidence": 0.9, "reason": "R"},
        ]
        for decision in invalid:
            async def complete(**kwargs):
                return SimpleNamespace(parsed={"decisions": [decision]})
            with self.subTest(decision=decision), self.assertRaises(ValueError):
                await module.assess(SimpleNamespace(llm=SimpleNamespace(acomplete_structured=complete)),
                    {"reports": [], "labels": [{"id": "l1"}], "open_todos": [{"id": "closed", "status": "done"}]})

    async def test_start_wrong_parent_cannot_inject(self):
        import tempfile
        module = load_plugin()
        def inject(*args, **kwargs):
            self.fail("unapproved thread injected")
        async def fetch_thread(thread_id):
            return SimpleNamespace(id=int(thread_id), parent_id=444444444444444444)
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
            await module.start(SimpleNamespace(inject_message=inject), {"thread_id": "111111111111111111",
                "user_id": "333333333333333333", "context": "Opening prompt"},
                fetch_thread=fetch_thread, allowed_parents=["222222222222222222"],
                state_path=Path(directory) / "starts.sqlite3")

if __name__ == "__main__":
    unittest.main()
