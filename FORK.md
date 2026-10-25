# Team-Sneakymouse fork

`origin` = `Team-Sneakymouse/hermes-agent`, `upstream` = `NousResearch/hermes-agent`. We track
upstream `main` and carry the smallest possible delta on top. Read this before a sync or before
adding a new integration.

## What the fork carries

| Delta | Where | Conflicts with upstream? | Retire when |
|---|---|---|---|
| Our plugins | `custom_plugins/*`, `plugins/pocketbase/` | Never (new paths) | n/a |
| Conversation hook seam | `gateway/conversation_plugins.py` (contract) + call sites below | Yes — the only real conflict surface | Upstream ships equivalent admission / routing / delivery-receipt hooks |
| Discord clarify fix | `plugins/platforms/discord/adapter.py` (PR #5) | Occasionally | Upstream fixes lossless clarify options |

Hook call sites in upstream files (the conflict checklist):
`gateway/platforms/base.py`, `gateway/run_turn_prepare.py`, `gateway/session.py`,
`gateway/session_recovery.py`, `gateway/slash_commands_session.py`, `hermes_cli/plugins.py`,
`hermes_cli/plugins_dispatch.py`, `hermes_cli/plugin_isolation.py`,
`plugins/platforms/discord/adapter.py`, `plugins/platforms/discord/adapter_media.py`.
Contract tests: `tests/gateway/test_plugin_conversation_contract.py`,
`tests/gateway/test_discord_conversation_plugin.py`. Docs:
`website/docs/developer-guide/gateway-conversation-plugins.md`.

`git diff --stat upstream/main -- . ':!custom_plugins' ':!plugins/pocketbase'` must show only the
above. Anything else that appears is an accidental fork drift; fix it.

## Weekly upstream sync

Merge, never rebase: rebasing rewrites our merged PRs and throws away earlier resolutions.

```sh
git fetch upstream origin
git switch -c chore/sync-upstream-$(date +%F) origin/main
git merge-tree --write-tree --name-only HEAD upstream/main | grep CONFLICT   # preview
P='conversation_plugins|conversation_lane|gateway_message_admission|gateway_session_route|gateway_delivery_receipt|prepare_conversation_route|preceding_message_ids|report_delivery|delivery_turn|bind_delivery_session|conversation_reset_boundary|plugin_profile_scope'
INV="$(mktemp -d)"
git grep -c -E "$P" -- gateway hermes_cli plugins/platforms | sort > "$INV/before.txt"
git merge --no-ff upstream/main
# resolve (rules below), then:
git grep -c -E "$P" -- gateway hermes_cli plugins/platforms | sort > "$INV/after.txt"
diff "$INV/before.txt" "$INV/after.txt"   # every difference must be explained
```

Verify, then open a PR into `origin/main` (title `chore(sync): merge NousResearch main at <sha>`):

```sh
HERMES_PYTHON=.venv/bin/python scripts/run_tests.sh \
  tests/gateway/test_plugin_conversation_contract.py tests/gateway/test_discord_conversation_plugin.py \
  tests/gateway/test_discord_clarify_lossless.py tests/gateway/test_discord_clarify_buttons.py \
  tests/plugins/test_pocketbase.py tests/plugins/test_pocketbase_api_scope.py
for d in custom_plugins/*/; do .venv/bin/python -m unittest discover -s "$d" -p 'test_*.py'; done
python scripts/check
```

(Windows: `.venv/Scripts/python.exe`; run `scripts/check` with `PYTHONUTF8=1`.) Expect `health`
ratchet findings in upstream files: the baseline is our pre-sync `main`, so only findings in the
fork-delta files above are ours. Windows hosts also fail a few path/file-lock tests that pass on
Linux (`send_multiple_images` receipts, todo-assessment `test_gateway_handoff`); compare against
`main` in a scratch worktree before blaming the sync. Then read each `custom_plugins/*/README.md` "compatibility
fence" section and grep the merged tree for the upstream behaviour it names.

Build `.venv` once with `python -m pm.build_env --source . --out .venv --group dev --group test --extra discord`
(Python 3.14; rebuild when `uv.lock` changes). Merge the sync PR with a merge commit, not a squash.
Don't let syncs lapse: three weeks of drift was ~9.7k commits and 5 conflicts.

### Conflict rules (learned the hard way)

- **Upstream refactors win.** Upstream constantly splits god files into `<stem>_<topic>.py`
  siblings. Take their side (`git checkout --theirs <file>`) and re-apply our one-liner where the
  code now lives (`git grep` for the surrounding function). Example: `_hmwa_resolve_session` moved
  from `run_turn.py` to `run_turn_prepare.py`, so `bind_delivery_session` moved with it.
- **Put receipts inside upstream's new helpers, not their callers**, so callers upstream adds later
  report too (e.g. `report_delivery` lives inside `_post_voice_message`).
- **Clean auto-merges can still drop a call site.** Never skip the before/after inventory diff.
- **Same name ≠ same feature.** Upstream's `gateway.session_ingress.admit_message` is queue
  durability, not our plugin admission veto. Read the code before retiring a hook.
- **Check whether each hook is still needed:** diff `VALID_HOOKS` in `hermes_cli/plugins.py` and
  skim upstream commits touching the checklist files. If upstream now covers a hook, port the
  plugin to it and delete our seam in the same sync PR. Update the table above.
- **A clean merge can still break a plugin.** Upstream tightens behaviour our plugins rely on
  without touching our files. Example: since `5018d437cc`, `ctx.inject_message(origin=...)` is
  refused for a chat with no admitted human message, which broke the todo-assessment one-click
  start. These are deliberate security boundaries: adapt the plugin, never patch core around
  them, and don't deploy the sync until the plugin is adapted.
- Keep upstream's style changes (`Set[str]` → `set[str]`, string annotations dropped) in our lines.

## Writing a new integration

Climb the footprint ladder in the root `AGENTS.md`, and stop at the first rung that works:

1. **Plugin on existing surface:** a directory in `custom_plugins/<name>/` (`plugin.yaml`,
   `__init__.py`, `README.md`, tests next to it) using existing hooks / `ctx` methods. Zero
   conflict cost. Strongly preferred. Use only documented `ctx`/hook behaviour, write an
   integration test through the real gateway path, including the runtime production builds
   (`initialize_gateway_runtime` / session authority; a bare `GatewayRunner` skips the policy
   that refused todo-assessment), since that test is the sync admission check, and list any
   behaviour you depend on that upstream might tighten under a "compatibility fence" heading in
   the README.
2. **Need a seam that doesn't exist?** Add a *generic* hook, never plugin-specific logic in core:
   - contract types and dispatch go in our own module (`gateway/conversation_plugins.py` or a new
     `gateway/<topic>_plugins.py`), never inline in upstream files;
   - each upstream file gets a 1–3 line, late-imported call into that module and nothing more;
   - register the hook in `VALID_HOOKS`, `HOOK_TIMEOUT_FAIL_CLOSED_HOOKS` if it is a policy gate,
     and `HOST_DEGRADED_HOOKS` if its payload can't cross the plugin host;
   - add a contract test in `tests/gateway/` that drives the real code path, and add the new call
     sites to the checklist and the inventory regex above.
3. **Upstream it if it's generic.** A seam with a working consumer (our plugin) meets upstream's
   bar ("no hooks without a concrete consumer"). Every accepted hook is one less file to resolve
   each week.

Don't patch upstream behaviour directly in a core file for a local preference. Make it a plugin
setting, or carry it as a clearly scoped fix PR that you'd also offer upstream (like PR #5).
