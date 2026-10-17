"""Tool-free todo assessment via the supported host-owned plugin LLM lane."""
import json
import math
import hmac
import hashlib
import sqlite3
import re
from contextlib import closing
from pathlib import Path


def register(ctx):
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    clients = {}
    def wire_discord(bot, adapter):
        clients["discord"] = bot
    async def fetch_thread(thread_id):
        import discord
        bot = clients.get("discord")
        if bot is None:
            raise RuntimeError("Discord transport unavailable")
        thread = await bot.fetch_channel(int(thread_id))
        if not isinstance(thread, discord.Thread):
            raise ValueError("Target must be a Discord thread")
        return thread
    def wire_api(app, adapter):
        from agent.secret_scope import get_secret
        wire_http(app, ctx, get_secret("HERMES_TODO_BRIDGE_TOKEN", "") or "", start_options={
            "fetch_thread": fetch_thread,
            "allowed_parents": ctx.get_config("allowed_parent_ids", ["1560943737688039474"]),
            "state_path": home / "plugin-data/discord-todo-assessment/starts.sqlite3",
        })
    ctx.register_platform_handler("discord", wire_discord)
    ctx.register_platform_handler("api_server", wire_api)


async def start(ctx, payload, *, fetch_thread, allowed_parents, state_path):
    if set(payload) != {"thread_id", "user_id", "context"}:
        raise ValueError("Expected thread_id, user_id, context")
    for key in ("thread_id", "user_id"):
        if not isinstance(payload[key], str) or not re.fullmatch(r"[0-9]{17,20}", payload[key]):
            raise ValueError("Invalid Discord ID")
    content = payload["context"]
    if not isinstance(content, str) or not content.strip() or len(content) > 60000:
        raise ValueError("Invalid opening context")
    thread = await fetch_thread(payload["thread_id"])
    if str(thread.id) != payload["thread_id"] or str(thread.parent_id) not in allowed_parents:
        raise ValueError("Thread not in configured parent allowlist")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    with closing(sqlite3.connect(state_path)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS starts (thread_id TEXT PRIMARY KEY, fingerprint TEXT, status TEXT)")
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT fingerprint, status FROM starts WHERE thread_id=?", (payload["thread_id"],)).fetchone()
        if row:
            if row[0] != fingerprint:
                raise ValueError("Thread already claimed with different context")
            return {"status": row[1], "completed": False, "thread_id": payload["thread_id"]}
        db.execute("INSERT INTO starts VALUES (?, ?, 'pending')", (payload["thread_id"], fingerprint))
    origin = {"platform": "discord", "chat_type": "thread", "chat_id": payload["thread_id"],
              "thread_id": payload["thread_id"], "user_id": payload["user_id"],
              "parent_chat_id": str(thread.parent_id),
              "guild_id": str(thread.guild.id), "scope_id": str(thread.guild.id)}
    accepted = ctx.inject_message(content, origin=origin)
    status = "accepted" if accepted else "rejected"
    with closing(sqlite3.connect(state_path)) as db, db:
        db.execute("UPDATE starts SET status=? WHERE thread_id=?", (status, payload["thread_id"]))
    return {"status": status, "completed": False, "thread_id": payload["thread_id"]}


def wire_http(app, ctx, token, *, start_options=None):
    from aiohttp import web
    async def handle(request, operation):
        supplied = request.headers.get("Authorization", "")
        if len(token) < 32 or not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
            return web.json_response({"error": "unauthorized"}, status=401)
        if request.content_length and request.content_length > 262144:
            return web.json_response({"error": "payload_too_large"}, status=413)
        try:
            raw = bytearray()
            async for block in request.content.iter_chunked(16384):
                raw.extend(block)
                if len(raw) > 262144:
                    return web.json_response({"error": "payload_too_large"}, status=413)
            payload = json.loads(raw)
            if operation == "start":
                if start_options is None:
                    return web.json_response({"error": "starter_unavailable"}, status=503)
                result = await start(ctx, payload, **start_options)
                status = 202 if result["status"] == "accepted" else 409
            else:
                result = await assess(ctx, payload)
                status = 200
            return web.json_response(result, status=status,
                                     dumps=lambda value: json.dumps(value, allow_nan=False))
        except (ValueError, KeyError, TypeError, AttributeError):
            return web.json_response({"error": "invalid_request_or_result"}, status=422)
        except Exception:
            return web.json_response({"error": "bridge_unavailable"}, status=503)
    async def handle_assess(request):
        return await handle(request, "assess")
    async def handle_start(request):
        return await handle(request, "start")
    app.router.add_post("/plugins/discord-todo/v1/assess", handle_assess)
    app.router.add_post("/plugins/discord-todo/v1/start", handle_start)

_COMMON = {"confidence": {"type": "number", "minimum": 0, "maximum": 1},
           "reason": {"type": "string", "minLength": 1, "maxLength": 2000}}
_DECISION_FIELDS = {
    "create": {"label": {"type": "string", "minLength": 1},
               "kind": {"enum": ["bug", "feature"]}, "priority": {"enum": ["red", "yellow", "blue"]},
               "title": {"type": "string", "minLength": 1, "maxLength": 200},
               "description": {"type": "string", "minLength": 1, "maxLength": 20000}},
    "merge": {"todo": {"type": "string", "minLength": 1}}, "dismiss": {}, "clarify": {},
}
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["decisions"],
          "properties": {"decisions": {"type": "array", "minItems": 1, "maxItems": 30,
              "items": {"oneOf": [
                  {"type": "object", "additionalProperties": False,
                   "required": ["action", *_COMMON, *fields],
                   "properties": {"action": {"const": action}, **_COMMON, **fields}}
                  for action, fields in _DECISION_FIELDS.items()
              ]}}}}


async def assess(ctx, payload):
    if not isinstance(payload, dict) or set(payload) != {"reports", "labels", "open_todos"}:
        raise ValueError("Expected reports, labels, open_todos")
    for key, identifier in (("reports", "id"), ("labels", "id"), ("open_todos", "id")):
        if not isinstance(payload[key], list) or len(payload[key]) > 1000:
            raise ValueError("Expected bounded arrays")
        for item in payload[key]:
            if not isinstance(item, dict) or not isinstance(item.get(identifier), str) or not item[identifier]:
                raise ValueError("Missing string identifier")
    if len(json.dumps(payload).encode()) > 262144:
        raise ValueError("Input too large")
    guidance = json.loads(Path(__file__).with_name("guidance.v2.json").read_text())
    result = await ctx.llm.acomplete_structured(
        instructions=guidance["instructions"],
        input=[{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        json_schema=SCHEMA, schema_name="discord_todo_assessment",
        purpose="discord-todo.assess.v2", temperature=0.0, max_tokens=6000, timeout=90,
    )
    from jsonschema import validate, ValidationError
    try:
        validate(result.parsed, SCHEMA)
        labels = {label["id"] for label in payload["labels"]}
        todos = {todo["id"] for todo in payload["open_todos"] if todo.get("status") in {"open", "in_progress", "blocked"}}
        for decision in result.parsed["decisions"]:
            confidence = decision["confidence"]
            if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise ValueError("Confidence must be finite and between 0 and 1")
            if decision["action"] == "create":
                if decision["label"] not in labels:
                    raise ValueError("Model returned unknown label")
                if decision["kind"] == "feature" and decision["priority"] != "blue":
                    raise ValueError("Features must be blue")
            if decision["action"] == "merge" and decision["todo"] not in todos:
                raise ValueError("Model returned unknown open todo")
    except ValidationError as exc:
        raise ValueError("Invalid assessment output") from exc
    return {"guidance_version": guidance["version"], "assessment": result.parsed}
