"""Delivery receipts for the gateway conversation-plugin seam (Team-Sneakymouse fork, FORK.md).

One aiohttp trace on the bot's HTTP session reports every message Discord confirms creating or
editing, so every send path (text chunks, overflow continuations, attachments, raw voice POSTs,
prompts) gets a receipt without per-path wiring. A multi-chunk send that fails part-way still
reports each chunk that landed: every chunk is its own request."""
import json
import re

import aiohttp

from gateway.conversation_plugins import report_delivery

_MESSAGE_WRITE = re.compile(r"/api/v\d+/channels/(\d+)/messages(?:/(\d+))?")
_OPERATIONS = {"POST": "send", "PATCH": "edit"}


async def _report_message_write(session, context, params):
    match = _MESSAGE_WRITE.fullmatch(params.url.path)
    operation = _OPERATIONS.get(params.method)
    # POST creates on the collection, PATCH edits one message; anything else is not a write.
    if match is None or operation is None or (operation == "edit") != bool(match[2]):
        return
    if params.response.status // 100 != 2:
        return
    # aiohttp caches the body, so discord.py's own read gets the same bytes.
    message_id = match[2] or json.loads(await params.response.read())["id"]
    await report_delivery(match[1], (str(message_id),), operation)


def delivery_receipt_trace() -> aiohttp.TraceConfig:
    """``http_trace`` for the Discord bot client."""
    trace = aiohttp.TraceConfig()
    trace.on_request_end.append(_report_message_write)
    return trace
