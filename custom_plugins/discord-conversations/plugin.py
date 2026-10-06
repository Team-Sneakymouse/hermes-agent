"""Discord conversation policy, using only documented gateway contracts."""


def admission(context, **kwargs):
    if context.platform != "discord" or context.chat_type == "dm":
        return {"action": "allow"}
    literal = context.bot_id and any(
        token in context.original_content
        for token in (f"<@{context.bot_id}>", f"<@!{context.bot_id}>")
    )
    return {"action": "allow" if literal else "block"}


import asyncio
import sqlite3
import uuid
from contextlib import contextmanager


def register(ctx):
    from hermes_constants import get_hermes_home
    policy = ConversationPolicy(get_hermes_home() / "plugin-data/discord-conversations/associations.sqlite3")
    if ctx.get_config("require_inline_mention", True):
        ctx.register_hook("gateway_message_admission", admission)
    if ctx.get_config("recent_channel_sessions", True):
        ctx.register_hook("gateway_session_route", policy.route)
        ctx.register_hook("gateway_delivery_receipt", policy.delivered)
        ctx.register_hook("on_session_reset", policy.reset)


class ConversationPolicy:
    def __init__(self, path):
        self.path = path
        self._ingress_locks = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.database() as db:
            db.execute("CREATE TABLE IF NOT EXISTS associations (scope TEXT, message_id TEXT, lane TEXT, PRIMARY KEY(scope, message_id))")

    @contextmanager
    def database(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("BEGIN IMMEDIATE")
            with db:
                yield db
        finally:
            db.close()

    async def route(self, context, services, **kwargs):
        source = context.source
        if source.platform.value != "discord" or source.chat_type != "group" or source.thread_id or not context.shared_channel:
            return None
        # asyncio.Lock queues in ingress order. Hold it across history retrieval
        # and publication so the next invocation can see this one's association.
        lock = self._ingress_locks.setdefault(context.base_session_key, asyncio.Lock())
        async with lock:
            recent_ids = await services.preceding_message_ids()
            with self.database() as db:
                lane = None
                for mid in recent_ids:
                    row = db.execute("SELECT lane FROM associations WHERE scope=? AND message_id=?",
                                     (context.base_session_key, str(mid))).fetchone()
                    if row:
                        lane = row[0]
                        break
                lane = lane or uuid.uuid4().hex
                db.execute("INSERT OR REPLACE INTO associations VALUES (?, ?, ?)",
                           (context.base_session_key, context.source.message_id, lane))
            return {"lane": lane}

    def reset(self, source=None, base_session_key=None, new_session_id=None, **kwargs):
        if source is None or base_session_key is None:
            return
        if source.platform.value != "discord" or source.chat_type != "group" or not source.conversation_lane:
            return
        with self.database() as db:
            db.execute("DELETE FROM associations WHERE scope=?", (base_session_key,))
            if source.message_id:
                db.execute("INSERT INTO associations VALUES (?, ?, ?)",
                           (base_session_key, source.message_id, source.conversation_lane))

    def delivered(self, receipt, **kwargs):
        source = receipt.source
        if source.platform.value != "discord" or source.chat_type != "group" or not source.conversation_lane or str(source.chat_id) != receipt.channel_id:
            return
        with self.database() as db:
            origin = db.execute("SELECT lane FROM associations WHERE scope=? AND message_id=?",
                                (receipt.base_session_key, receipt.origin_message_id)).fetchone()
            # Missing origin means an intentional boundary erased this in-flight turn.
            if origin and origin[0] == source.conversation_lane:
                db.executemany("INSERT OR REPLACE INTO associations VALUES (?, ?, ?)",
                               [(receipt.base_session_key, mid, origin[0]) for mid in receipt.message_ids])
