# Discord conversations

Opt-in custom policy, not bundled/automatically enabled. Requires the gateway APIs in this feature branch. No monkeypatches, adapter subclass overrides, or private adapter/session-store access.

- Guild humans and bots must type literal `<@BOT_ID>` or `<@!BOT_ID>` in the original message. Reply-ping metadata, forwarded snapshots, or other IDs alone do not trigger. DM text admission is unchanged.
- Ordinary shared guild channels (`group_sessions_per_user: false`) query the entire configured preceding history window on each invocation. The newest associated invocation or delivered reply selects its durable lane; unrelated messages still consume window positions. An empty qualifying window creates a new lane, never overwriting another lane's accumulated transcript.
- Threads/DMs retain built-in persistent routing. Admission still requires a literal mention in guild threads.
- `/new` and `/reset` erase channel associations and retain the reset invocation as the new boundary. Receipts whose origin was erased cannot resurrect old conversations. The plugin's own SQLite index closes handles after each transaction and survives restarts; core routing follows compression tips.

State: `$HERMES_HOME/plugin-data/discord-conversations/associations.sqlite3`. Settings: `plugins.entries.discord-conversations.settings.require_inline_mention` and `.recent_channel_sessions`, both default true when enabled. No external Python dependencies beyond the normal Discord gateway runtime.

## Deployment / migration (operator-run; not executed by implementation)

1. Review/merge the feature branch into the fork's `main`. Preserve today's local patch, untracked helper/tests, config, `state.db` (use a consistent SQLite backup), session routing state, and `sessions/discord_recent_sessions.sqlite3` before deployment. Do not delete backups or selectively undo running code before cutover.
2. Verify `hermes update --check` targets the fork's `main`. The operator performs `hermes update` and any required maintenance/restart through their usual CLI workflow. Resolve the existing local core patch deliberately; do not blindly discard it.
3. Install the reviewed directory into the target profile, replacing an existing copy only after backing it up:
   ```sh
   cp -R "$CHECKOUT/custom_plugins/discord-conversations" "$PROFILE_HOME/plugins/"
   HERMES_HOME="$PROFILE_HOME" hermes config set plugins.isolation in_process
   HERMES_HOME="$PROFILE_HOME" hermes plugins enable discord-conversations
   HERMES_HOME="$PROFILE_HOME" hermes config set plugins.entries.discord-conversations.settings.require_inline_mention true
   HERMES_HOME="$PROFILE_HOME" hermes config set plugins.entries.discord-conversations.settings.recent_channel_sessions true
   ```
   Choose the target profile explicitly; repeat only for profiles meant to run this policy. In-process plugins are trusted Python code.
4. Retain built-in `group_sessions_per_user: false`, the existing history limit (currently 20), and the existing thread/channel configuration. The plugin does not silently disable auto-thread creation. Normal-channel recent routing applies where the transport actually delivers inline.
5. **Legacy data limitation:** old local `:recent:` keys and `discord_channel_lane` metadata are not automatically imported into new generic `:lane:` keys. Old transcripts are not deleted. Cutover starts a new routing lane; when continuity is required, select the previous durable session explicitly using the supported `/resume <session_id>` command in the channel after cutover. A bulk/seamless legacy-index importer is not included. Do not point the new plugin at the old schema.
6. After parity checks, retire the old `discord.require_inline_mention` / `discord.recent_channel_sessions` local flags with `hermes config unset` and remove only the superseded local core changes. They are replaced by plugin settings, not new core Discord settings. No implementation step mutated production configuration.
7. Verify literal human and bot mentions trigger, implicit reply-pings do not, an associated message at the oldest window position resumes, a fully unrelated window starts fresh, long/streamed/partial output extends the association window, `/new` fences late replies, and restart/compression preserve a lane's history. Check both DM and thread routing. Delivery observers are explicitly wired send paths, not interception of arbitrary SDK UI/interaction response methods; review any additional production reply paths before claiming universal coverage.

Rollback: operator disables `discord-conversations`, restores the reviewed previous source/config and consistent backed-up state, and uses their usual CLI restart workflow. Do not mix a restored association index with unrelated newer session state.

## Tests

PM-managed test environment with Discord extra:
```sh
<managed-python> -m pm.build_env --source . --out .venv-discord --group dev --group test --extra discord
HERMES_PYTHON="$PWD/.venv-discord/bin/python" scripts/run_tests.sh -j 2   tests/gateway/test_plugin_conversation_contract.py   tests/gateway/test_discord_conversation_plugin.py
```
The output environment must not exist when building it. Tests isolate profile homes and close DB handles. The two test files cover real plugin loading, literal admission, unfiltered routing services, native command routing, split/partial/stream/native-media receipts, reset fencing, profile scoping, and real SQLite transcript/compression/restart behavior.
