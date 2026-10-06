# Implementation handoff (partial migration)

All edits are uncommitted in the isolated feature worktree. No live checkout/config/plugin modification, service restart, publication, or merge was performed.

## Fix cycle 1: independent-review blockers

Only the three blocking defects were addressed; no legacy importer, SQLite timeout redesign, or unrelated cleanup was added.

- **Lane-blind recovery:** strict RED `test_new_lane_after_unrelated_window_has_fresh_real_transcript` failed because distinct plugin lanes returned the same durable session ID. Exact-key-only recovery for explicit lanes then passed together with real transcript/compression-tip/restart coverage (**2 passed**).
- **Concurrent ingress:** strict RED `test_overlapping_routes_publish_in_ingress_order_per_scope` failed with different A/B lanes when A's history was paused and B saw A before publication. Per-base-scope FIFO serialization now spans history retrieval and association publication (**1 passed**); another profile namespace proceeds independently while A is blocked.
- **Late reset observer:** after correcting a test callback signature, both deterministic reset cases failed because the following invocation's successful reply association was absent. Tests pause after a real core reset at finalization or inside the asynchronous reset observer, then attempt another invocation. Core now shares a runtime-home/base-scope lock between lane selection and the complete lane reset handler, preserving lifecycle facade/first-party observers. Both cases pass; post-boundary reply IDs persist and late OLD reply IDs remain absent. No superficial observer reordering was used.

Focused contracts/policy gate after fixes: **2 files, 41 passed, 0 failed**, 19.4 seconds, exit 0 (22 contract tests + 19 policy tests; four added regression cases).

Current broader gate:

```sh
HERMES_PYTHON="$PWD/.venv-discord/bin/python" scripts/run_tests.sh -j 2 tests/gateway/test_plugin_conversation_contract.py tests/gateway/test_discord_conversation_plugin.py tests/gateway/test_discord_free_response.py tests/gateway/test_discord_edit_message_overflow.py tests/gateway/test_adapter_session_key_seam.py tests/gateway/test_multiplex_adapter_session_key_namespace.py tests/gateway/test_session_model_reset.py tests/gateway/test_reset_button_deadlock.py tests/gateway/test_session_db_recovery.py tests/gateway/test_session_recovery.py tests/gateway/test_reaped_session_recovery.py -v
```

Actual result: **11 files, 117 tests passed, 0 failed**, 80.3 seconds, exit 0. This is **not** a pristine first-attempt pass: `test_reset_completes_when_cleanup_raises` again timed out at Telegram `_telegram_topic_new_header` (`slash_commands_session.py:200`); first attempt **1 failed, 2 passed in 15.95s**, retry **3 passed in 13.18s**. This non-lane source bypasses the new reset lock; the same failure was recorded before this fix cycle. Its underlying cause remains unclassified and was not changed within this scope.

`git diff --check` and Ruff on the conversation contract module, custom policy, and policy tests passed. Existing environment reused; no live source/config/plugin edits, installs, service restarts, commits, pushes, or PRs. Independent re-review remains pending. Legacy `/resume` acceptance beyond the earlier documented cutover remains untested (not one of the three requested blockers).

## Previous implementation verification (historical)

Previous focused run:

```sh
HERMES_PYTHON="$PWD/.venv-discord/bin/python" scripts/run_tests.sh -j 2  tests/gateway/test_plugin_conversation_contract.py  tests/gateway/test_discord_conversation_plugin.py  tests/gateway/test_discord_free_response.py  tests/gateway/test_discord_edit_message_overflow.py  tests/gateway/test_adapter_session_key_seam.py  tests/gateway/test_multiplex_adapter_session_key_namespace.py  tests/gateway/test_session_model_reset.py  tests/gateway/test_reset_button_deadlock.py
```

Actual runner result: **8 files, 100 tests passed, 0 failed**, 63.1 seconds, exit 0. Includes **37 new tests** (22 contracts, 15 plugin tests). The runner flagged one retry-dependent file, so this is not a pristine first-attempt pass:

```
FAILED tests/gateway/test_reset_button_deadlock.py::test_reset_completes_when_cleanup_raises
E TimeoutError
 gateway/slash_commands_session.py:198
 header = await asyncio.to_thread(self._telegram_topic_new_header, source) or default_header
1 failed, 2 passed in 17.05s
Retry: 3 passed in 11.40s
```

The timeout occurred at Telegram topic-header work before the newly extended reset observer. Its root cause and baseline-vs-regression classification are **not established**. No unrelated timeout changes were made.

Earlier broader run: 181 passed / 1 failed across 12 files (including 87 plugin SDK tests). The failure was real: `test_short_tagged_bot_chunk_waits_for_followup_window` expected `sleep(0.08)`, received `sleep(0.01)` because continuation timing was lost across new admission work. Moved bot continuation bookkeeping after the admission veto; the entire 30-test free-response file then passed, and passes in the final focused run. Earlier plugin SDK tests were not rerun after the final base-adapter routing refactor.

RED→GREEN cycles were exercised for durable generic lanes, immutable early admission/veto, pre-batch unfiltered history routing, literal custom admission, latest-association routing/restart, persistent-channel exclusions, receipt recording/reset fences, per-chunk partial-send receipts, background delivery identity, streamed edit/partial overflow IDs, reset payload enrichment, resolved session IDs, routed profile receipts, thread-parent admission scoping, inline command scopes, SDK host degradation declarations, native media receipts, legacy reset payload compatibility, and native interaction routing/origin IDs. Example RED output: lane field rejected by SessionSource; early veto returned True instead of False; missing receipt produced zero IDs; stream receipts were empty; native interaction lane was None. Real SQLite acceptance coverage confirms retained transcript/compression tip/restart without a plugin touching private SessionStore methods.

## Environment vs implementation

- Initial isolated bootstrap probe failed: `hermes: no dependency environment is committed for this install; run hermes pm repair`. No repair or installed-environment mutation was attempted.
- Built disposable worktree `.venv` using PM dev/test groups, then `.venv-discord` using PM dev/test plus Discord extra. Initial environment lacked aiohttp; URL-media tests could therefore pass via text fallback instead of exercising native sends. The final contract suite imports the real Discord SDK when available and ran in `.venv-discord`, exercising native media paths with mocked HTTP/transport results (no Discord network credentials).
- Official docs extraction tool was unavailable for this provider; fetched the official plugin guide through curl and inspected repository SDK/source contracts.
- New-file Ruff checks and `git diff --check` passed. Imports were verified to resolve from this worktree, not the live source.

## Remaining gaps / review priorities

- **Partial deployment migration:** no automatic import of today's legacy `:recent:` route keys/SQLite schema. Existing transcripts remain accessible via supported `/resume`; README documents explicit cutover continuity and backups. A seamless bulk importer is not delivered.
- Receipt coverage is explicit adapter wiring, not interception of every Discord SDK UI/interaction/forum send method. Reviewed normal-channel text, split/stream/partial, and native-media paths are covered; unscoped proactive sends intentionally have no invocation association. Audit additional production-specific send paths before claiming universal receipt coverage.
- Gate/router hooks are in-process contracts; host-isolated execution does not preserve typed payload/service semantics.
- Final backward-compatibility review caught a reset-observer regression: direct SDK dispatch bypassed first-party lifecycle observers. Extended the reset contract test (RED: only on_session_finalize observed), then used the existing lifecycle.ainvoke_hook facade (GREEN: all 22 contract tests passed). The 8-file/100-test run above precedes this import-only correction; the full contract suite and Ruff/diff checks passed after it.
- No live Discord smoke test, full suite, docs-site build, independent review, or publishing was performed. Parent must review broader default-adapter routing effects and native interaction/reset paths before merge/deployment.

Main cost bottlenecks: the task crossed raw Discord ingress, batching/busy guards, canonical profile routing, session resets/compression, SDK hook registration/isolation, and several physical delivery/media paths; isolated PM environments and optional Discord dependencies had to be built; regression runs were relatively expensive and exposed a real bot-debounce timing issue. Scope expansion into complete native-media receipts and command-interaction routing added iterations. Stopped further expansion when parent relayed the cost constraint.
