"""Public adapter admission contracts. Policy belongs to registered plugins."""
from dataclasses import dataclass
from contextvars import ContextVar
import asyncio
from weakref import WeakValueDictionary
from typing import Awaitable, Callable
from gateway.session import SessionSource
from functools import wraps
from contextlib import contextmanager
from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from gateway.session_identity import identity_of, replace_source


@dataclass(frozen=True)
class AdmissionContext:
    platform: str
    original_content: str
    bot_id: str | None
    channel_id: str
    chat_type: str
    message_id: str
    user_id: str
    is_bot: bool
    profile: str | None


@contextmanager
def plugin_profile_scope(source):
    """Dispatch through the routed profile's plugin manager, not the transport's."""
    identity = identity_of(source)
    token = set_hermes_home_override(identity.runtime_home) if identity else None
    try:
        yield
    finally:
        if token is not None:
            reset_hermes_home_override(token)


async def admit_message(context, source):
    """Async veto before normalization/side effects. No listeners means allow."""
    from hermes_cli.plugins import ainvoke_hook
    with plugin_profile_scope(source):
        results = await ainvoke_hook("gateway_message_admission", context=context)
    for result in results:
        if not isinstance(result, dict) or result.get("action") not in {"allow", "block"}:
            return False
        if result["action"] == "block":
            return False
    return True


@dataclass(frozen=True)
class RouteContext:
    source: SessionSource  # detached snapshot; read-only by contract
    original_content: str
    base_session_key: str
    shared_channel: bool


@dataclass(frozen=True)
class RoutingServices:
    preceding_message_ids: Callable[[], Awaitable[tuple[str, ...]]]


_conversation_locks = WeakValueDictionary()


def _conversation_lock(source, base_session_key):
    """One FIFO boundary per loop, runtime home and base route, never per lane."""
    from hermes_constants import get_hermes_home
    identity = identity_of(source)
    home = identity.runtime_home if identity else get_hermes_home()
    key = (asyncio.get_running_loop(), str(home), base_session_key)
    lock = _conversation_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _conversation_locks[key] = lock
    return lock


def conversation_reset_boundary(callback):
    """Fence plugin ingress across core reset AND every awaited reset observer.

    Built-in routes retain their existing behavior. Lane routes share their base
    scope with selection, so post-reset origins cannot be erased by a late observer.
    """
    @wraps(callback)
    async def wrapped(runner, event, *args, **kwargs):
        source = event.source
        if not source.conversation_lane:
            return await callback(runner, event, *args, **kwargs)
        base = runner._session_key_for_source(source).rsplit(":lane:", 1)[0]
        async with _conversation_lock(source, base):
            return await callback(runner, event, *args, **kwargs)
    return wrapped


async def select_conversation_lane(context, services, source):
    """None preserves routing; agreeing lane results apply, conflicts fail closed."""
    from hermes_cli.plugins import ainvoke_hook
    async with _conversation_lock(source, context.base_session_key):
        with plugin_profile_scope(source):
            results = await ainvoke_hook("gateway_session_route", context=context, services=services)
        lanes = []
        for result in results:
            if not isinstance(result, dict):
                raise ValueError("gateway_session_route requires a lane mapping")
            lane = result.get("lane")
            if not isinstance(lane, str) or not lane or len(lane) > 128:
                raise ValueError("conversation lane must be a nonempty string of at most 128 characters")
            lanes.append(lane)
        if len(set(lanes)) > 1:
            raise ValueError("conflicting plugin conversation lanes")
        if lanes:
            source.conversation_lane = lanes[0]


@dataclass(frozen=True)
class DeliveryReceipt:
    source: SessionSource
    session_key: str
    session_id: str | None
    origin_message_id: str | None
    channel_id: str
    profile: str | None
    message_ids: tuple[str, ...]
    operation: str

    @property
    def base_session_key(self):
        return self.session_key.rsplit(":lane:", 1)[0] if self.source.conversation_lane else self.session_key


_delivery = ContextVar("gateway_plugin_delivery", default=None)


@contextmanager
def delivery_scope(source, session_key, session_id=None):
    """Snapshot origin before awaits; child tasks inherit this turn's delivery identity."""
    token = _delivery.set((replace_source(source), session_key, session_id))
    try:
        yield
    finally:
        _delivery.reset(token)


def bind_delivery_session(session_id):
    """Bind the resolved session before streaming tasks are spawned; origin remains frozen."""
    current = _delivery.get()
    if current is not None:
        source, key, _ = current
        _delivery.set((source, key, session_id))


def delivery_turn(callback):
    """Core adapter boundary: a fresh scope for every queued/drained background turn."""
    @wraps(callback)
    async def wrapped(adapter, event, *args, **kwargs):
        session_key = args[0] if args else adapter._event_session_key(event)
        with delivery_scope(event.source, session_key):
            return await callback(adapter, event, *args, **kwargs)
    return wrapped


async def report_delivery(channel_id, message_ids, operation="send"):
    """Adapters call immediately after each successful physical send/edit, including partial sends."""
    current = _delivery.get()
    if current is None or not message_ids:
        return
    source, key, sid = current
    from hermes_cli.plugins import ainvoke_hook
    receipt = DeliveryReceipt(source, key, sid, source.message_id, str(channel_id),
                              source.profile, tuple(str(mid) for mid in message_ids), operation)
    with plugin_profile_scope(source):
        await ainvoke_hook("gateway_delivery_receipt", receipt=receipt)
