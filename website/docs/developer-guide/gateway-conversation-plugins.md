---
title: Gateway conversation plugin contracts
---

Gateway policies can register these public hooks with `ctx.register_hook`. No plugin means ordinary gateway routing and Discord admission remain in effect. These typed contracts require `plugins.isolation: in_process`; host-mode degradation is declared in the SDK. They do not replace platform authorization.

## Admission

`gateway_message_admission(context: AdmissionContext)` runs on Discord message ingress after built-in authorization, before content normalization, bot continuation bookkeeping, auto-thread creation, attachments, or batching. `context` is frozen and contains `platform`, exact `original_content` (not stripped or snapshot-derived), `bot_id`, `channel_id`, `chat_type`, `message_id`, `user_id`, `is_bot`, and `profile`.

Return `None` or `{"action": "allow"}` to retain built-in behavior; `{"action": "block"}` vetoes the message. Any veto wins. Invalid results and callback exceptions fail closed. Async callbacks run on the gateway loop and are awaited. Admission cannot bypass built-in channel/identity policy. Native slash interactions are commands, not incoming text messages, and do not require a textual mention.

## Routing

`gateway_session_route(context: RouteContext, services: RoutingServices)` runs before Discord text batching and before base-adapter busy/session guards (including native command interactions). `context.source` is a detached, read-only-by-contract `SessionSource`; other fields are `original_content`, `base_session_key` (canonical key before a lane), and `shared_channel`.

`await services.preceding_message_ids()` returns the full configured preceding history window, newest first, without filtering authors/content. Irrelevant messages count against its size. Discord uses `discord.history_backfill_limit`, independently of model-context backfill. Unsupported history and transport errors raise; they must not masquerade as empty history.

Return `None` to preserve the route, or `{"lane": "opaque-stable-id"}`. Lanes must be nonempty strings of at most 128 characters. Conflicting lane results, malformed results, and callback errors fail closed. Core changes only `source.conversation_lane`, never delivery channel/thread IDs. `build_session_key` appends an escaped lane discriminator to the ordinary profile-scoped key. The field round-trips through `SessionSource.to_dict/from_dict`. Routing is once per event; flushes, already-laned sources, and internal wakes retain their route.

Adapters can implement public `preceding_message_ids(event)` and call public `prepare_conversation_route(event, original_content=...)` before their own batching. Base `handle_message` supplies the guard boundary. Early admission is currently wired into Discord; other adapters may use `gateway.conversation_plugins.admit_message` before normalization.

## Delivery and reset

`gateway_delivery_receipt(receipt: DeliveryReceipt)` is an observer (return ignored, callback errors isolated). It reports `source`, `session_key`, `base_session_key`, resolved `session_id` (or `None` before resolution), `origin_message_id`, actual `channel_id`, `profile`, `message_ids`, and `operation` (`send`/`edit`). Source/origin are snapshotted per background turn and inline command bypass; child streaming tasks inherit the turn. Dispatch uses the routed profile's plugin manager, not the transport profile's manager.

Discord reports successful physical text chunks, streaming edits/overflow continuations, and normal-channel native media deliveries immediately. Already-delivered chunks remain observable when a subsequent send fails. Repeated edits/retries can repeat IDs: consumers must be idempotent. Unscoped proactive sends do not invent an invocation association. Additional adapter send paths must explicitly call public `report_delivery`; this is not a universal interception of arbitrary SDK sends.

Gateway `on_session_reset` retains existing ID/platform fields and adds detached `source`, `channel_id`, `profile`, `session_key`, and `base_session_key`. It is awaited after the replacement session exists through the lifecycle facade, preserving first-party observers. For explicit lane routes, core serializes routing and the complete reset handler (including awaited observers) by runtime home and base session key: ingress during reset waits until the boundary finishes, so a delayed reset observer cannot erase a following invocation's association. Other scopes/profiles remain independent; built-in non-lane resets retain their existing behavior. Legacy non-gateway reset payloads need not contain these added fields. Session-store compression and recovery remain core responsibilities; explicit lanes use exact-key recovery, never the lane-blind platform/chat/user fallback. Plugins can index stable lanes rather than pinning obsolete session IDs.

Route callbacks and reset observers must not await recursive routing for the same scope: the shared scope lock is intentionally non-reentrant. Keep same-scope routing outside these callbacks to avoid deadlocks.

See the versioned `custom_plugins/discord-conversations/README.md` for policy and deployment steps.
