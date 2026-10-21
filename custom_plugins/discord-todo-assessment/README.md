# Discord todo bridge (not deployed)

## Manual retrospective extension

New bearer-authenticated routes under `/plugins/discord-todo/v1/retrospective/`:

- `start`: `{run_id,thread_id,user_id,guild_id,corrections,context}`; 202 returns accepted/run/thread/baseline version/hash. Requires 1–100 distinct correction IDs, total canonical input ≤200,000 bytes and actual fetched Discord thread guild/allowed parent matching. The bot MUST create a fresh forum thread; the SDK does not reset existing conversations. Identical run replays do not dispatch again; changed identity/evidence is rejected.
- `status`: `{run_id,user_id}`; investigating has no proposal. Proposed returns complete proposal, full SHA-256 hash and untruncated unified diff. Applied returns top-level receipt fields plus the exact persisted `receipt` object.
- `prepare-approval`: `{run_id,user_id,proposal_hash}`; same Bearer authentication. One SQLite immediate transaction checks full hash, owner and current baseline then freezes the exact proposal in `approval_pending`. Later draft calls reject. No policy activation occurs. Exact freeze replay is safe; applied replay returns the durable receipt fields. Node must prepare before PB approve-begin, and display the same complete proposal/diff plus genuine-click Approve retry while pending. Refresh cannot activate it.
- `approve`: `{run_id,user_id,proposal_hash}`; only the trusted bot's explicit human approval interaction may call this. Returns `{run_id,state:'applied',proposal_hash,policy_version,policy_hash}`. Wrong owner/hash or stale baseline fails closed (422). Exact receipt replay remains valid after later policy revisions.

A 256-bit random per-run capability is included ONLY in the trusted injected opening instructions. `discord_todo_retrospective_propose({capability,proposal})` uses it to find the persisted run; it accepts no model-selected run/user ownership. It can only record drafts, never activate policy. The capability is not present in start/status/approval responses. It is stored in the private run database and canonical transcript: keep these profile files private. The trusted full-agent runtime itself is not a sandbox against arbitrary local code/file access. Draft tool security does not replace the host's human authorization, profile isolation, or bridge secret boundary.

Proposal shape: `{rules:[{text,evidence_ids:[id]}],memory:[{text,evidence_ids:[id]}],summary:string}`. Arrays are complete replacement learned-policy state. Existing unchanged entries may retain historical evidence; new entries must cite frozen correction IDs. All removals are visible in the complete diff. Explicit no-change proposals need approval too, and preserve policy version/hash only when the arrays actually match the baseline.

`plugin-data/discord-todo-assessment/policy.sqlite3` owns active learned policy, immutable-by-API history and durable run/proposal/approval receipts. Policy activation and run receipt use one `BEGIN IMMEDIATE` SQLite transaction with baseline CAS. Crash between reservation and dispatch leaves `pending`; reconcile rather than blindly redispatch. An asynchronous accepted dispatch is not proof that the host authorized/executed a turn; inspect normal gateway output and the thread.

HTTP assessment reads the same database and actually supplies approved rules/memory to the existing tool-free structured call, under immutable guidance v2. Initial version is `2`; changed policy versions are `2.learned.<full proposal hash>`. Core guidance and severity/confidence validation are not editable by proposals. There is no retrospective tool or agent loop in ordinary assessment.

**Profile admission requirement:** run the bridge and draft tool in the same owning profile and install/enable the plugin there. A forum routed to another profile must have matching plugin/policy ownership; do not assume independently installed profiles share a policy database. The test configures the actual served coding profile and verifies same-profile tool persistence. No installation/configuration/restart is performed here.

### Bounded learned policy and indexed capabilities

The first open of an old database transactionally creates/backfills `draft_capabilities(capability_hash PRIMARY KEY, run_id UNIQUE REFERENCES retrospective_runs(id))`. SHA-256 capability hashes, not raw bearer capabilities, are indexed. The table's committed existence is the migration marker; no version counter is reset. A failed migration rolls back DDL/backfill and preserves history. Subsequent opens and draft submissions never decode unrelated historical runs; new run/index rows commit atomically. Raw capabilities remain in private run JSON as before.

Complete canonical JSON for `{rules,memory}` must fit **16,384 UTF-8 bytes**, including JSON structure and evidence lists. Each array has at most 100 lessons; text and summary must be nonblank and each raw stored value fits 4,000 UTF-8 bytes (whitespace is counted, not silently stripped). Each evidence list has 1–100 unique IDs, each nonblank and at most 128 UTF-8 bytes; frozen correction IDs have the same bound. New/changed entries still require frozen evidence; unchanged entries may retain historical evidence. Strict canonical JSON rejects nonfinite numbers. Draft writes, prepare-approval, approval, and assessment validate the budget. Oversized legacy drafts cannot activate; oversized active state fails closed before model invocation. Nothing is truncated or deleted: operators must explicitly reconcile legacy state while preserving history.

Assessment has a separate **49,152 UTF-8 byte combined ceiling** on actual instructions (including learned policy), canonical input, and canonical output schema. HTTP retains its 256 KiB transport ceiling, but that is not a model budget. Configure a model with **at least 65,536 context tokens**: this conservative byte-as-token admission budget reserves 6,000 output tokens and 10,384 tokens for host/schema framing. The budget leaves room for the complete learned-policy ceiling, a representative 37-label PocketBase catalog, and report evidence together; this combination has a regression test. The installed public PluginLlm API does not expose resolved model context size; this is an operator minimum, not automatic discovery or a guarantee for arbitrary non-byte-based tokenizers. Inputs that exceed the combined ceiling return 422 and must be reduced explicitly. Approval at the full learned-policy ceiling is tested followed by a small assessment; large report/todo inputs still share the remaining ceiling.

### Retrospective verification

```sh
hermes --run-module unittest discover -s /home/mothermouse/projects/hermes-todo-retrospective/custom_plugins/discord-todo-assessment -p 'test_*.py' -v
```

Installed-runtime suite: 16 tests including all original 12, actual aiohttp HTTP, real SQLite reopen, approval/hash/owner/CAS/no-change regressions, approved memory in actual structured-call arguments, and real GatewayRunner canonical question → human follow-up → registered draft-handler persistence. Model generation and external Discord remain explicit recording fixtures; no live model, Discord post or repository investigation was executed. Full output: `/home/mothermouse/.hermes/cache/scratch/todo-retrospective-hermes-tests.log`.


A directory plugin using the installed Hermes plugin APIs. No core patches, runtime rebinding, provider credentials in Node, new paid credential, or Discord bot-message admission is required.

## Compatibility fence — important

The installed checkout at `0ef671bbd4c` exposes origin injection for a new chat, and the real-host integration test below verifies that behavior. **The live official docs now describe a newer session-authority mode that refuses origin injection when no human message has yet been admitted in that chat.** It returns False with `has no human message admitted yet`. That newer restriction does not appear in this installed `gateway/run_plugin_injection.py`, and the installed local docs still describe new-session origin injection without it.

Therefore this artifact is tested against the installed runtime, **not a promise of fresh-thread bootstrap after an update to that newer authority implementation**. Preserve this integration test as an update admission check. If the authority refusal is present, the single-click/no-human-follow-up requirement needs a different supported ingress carrying actual platform provenance; a separate webhook reply is not equivalent, and fabricating provenance or patching private methods is not an acceptable workaround. This compatibility difference is a deployment blocker on such newer hosts, not a reason to change the live gateway here.


## One-click start contract

1. Node relies on Discord-authorized command delivery: `/todo` and `Create report` default to Administrator, with Discord application-command overrides granting intended developer access, following the same model as `/plugins update`. Start/edit components additionally require the original list owner, intended guild and a bot-authored message. Node atomically claims the todo for the **actual human developer** who clicked Start.
2. Node **creates a new forum post/thread for that attempt in forum `1560943737688039474`**, pings the actual developer who clicked Start in its starter message, and stores its ID. Do not reuse a previously conversed thread when promising a fresh conversation.
3. Node POSTs `/plugins/discord-todo/v1/start` on the Hermes API-server listener:

```json
{"thread_id":"111111111111111111","user_id":"333333333333333333","context":"Complete opening prompt: todo, plugin/repository identity, reports, developer, constraints and requested workflow."}
```

Send `Authorization: Bearer <HERMES_TODO_BRIDGE_TOKEN>` and `Content-Type: application/json`. This is a **separate, narrow bridge token**, not the LLM provider token or the general terminal-capable API-server token. Use loopback or a protected private network/TLS. Never send these requests from a browser or expose the token in Discord.

Response: HTTP 202 `{"status":"accepted","completed":false,"thread_id":"..."}`. The plugin calls:

```python
ctx.inject_message(context, origin={
    "platform": "discord", "chat_type": "thread",
    "chat_id": thread_id, "thread_id": thread_id,
    "user_id": developer_discord_id,
    "parent_chat_id": str(thread.parent_id),
    "guild_id": str(thread.guild.id), "scope_id": str(thread.guild.id),
})
```

The parent and guild/scope IDs come from the fetched Discord thread, never caller-supplied routing fields. They preserve forum-parent and guild profile routes. The gateway must serve this plugin's profile and route that forum to it; injection cannot select a different profile. This creates the **canonical Discord thread session in this plugin's own profile**, with the supplied context as its opening user prompt. Human follow-ups address that same session. Unlike webhooks, this is not a separate webhook conversation with its answer merely delivered to Discord. No `/new` command or bot-authored mention message is sent. The profile's human admission/mention policy still applies to later real Discord replies. The forum starter ping is performed by Node and is not used as a bot-authored gateway trigger.

Host injection authorizes the supplied human ID against current Hermes allowlists/pairing/explicit allow-all. An adapter-only role authorization is insufficient. Configure the actual human developers in the host's current authorization policy before enabling this integration; do not substitute a privileged fixed user ID. Node does not independently query a Developer role: its boundary is Discord command defaults/overrides plus owner-bound component checks. Host authorization of the actual human is independent and still required.

**Acceptance is asynchronous, not evidence of a completed turn or successful Discord delivery.** The caller must not describe 202 as completed work. Actual replies remain visible in the designated thread; no extra human confirmation or follow-up command is required to initiate the run.

Opening context must be nonempty and at most 60,000 Unicode code points (Python string length); the encoded JSON request must also fit the 256 KiB HTTP limit. Node checks both limits before creating a forum post and never silently truncates full context.

The plugin verifies the target using the connected Discord SDK, requires `discord.Thread`, and checks its actual parent ID against `allowed_parent_ids` (default: `["1560943737688039474"]`). The existing label development thread is NOT a Start destination. Node owns forum creation, developer ping, and persisting one unique thread per claimed todo attempt; the plugin only admits the supplied context there. SQLite stores a fingerprint and status per thread: identical repeated requests do not enqueue another turn; changed context/user for a claimed thread fails. A crash between reserving and enqueueing leaves `pending` (HTTP 409 on replay), deliberately requiring operator reconciliation rather than silently starting duplicate coding work. `rejected` also returns HTTP 409. Reusing a thread that already has a Hermes conversation **continues** that conversation; the Node-created fresh thread is the freshness fence. No supported reset primitive is used or claimed here.

## Assessment contract

POST `/plugins/discord-todo/v1/assess`, same Authorization header:

```json
{
  "reports": [{"id":"report-1","context":{"text":"The supplied report and context"}}],
  "labels": [{"id":"plugin-label-id","name":"Plugin name","description":"Plugin scope"}],
  "open_todos": [{"id":"todo-id","title":"Existing issue","description":"Details","label":"plugin-label-id","kind":"bug","priority":"yellow","status":"open"}]
}
```

Only these top-level keys are accepted. Identifiers must be nonempty strings; each array is bounded to 1000 records and the HTTP body to 256 KiB. The plugin uses `ctx.llm.acomplete_structured` with the host's configured provider/auth, a 90-second model timeout, 6000 output tokens, and no provider/model override. No AIAgent, tool loop, codebase access, URL retrieval, memory injection, or session creation occurs in assessment.

HTTP 200:

```json
{"guidance_version":"2","assessment":{"decisions":[{"action":"clarify","confidence":0.6,"reason":"Insufficient reproduction information"}]}}
```

The `assessment` object matches the bot's `src/todo/assessment.ts` decision schema: create, merge, dismiss, clarify; features must be blue; exact supplied label/todo IDs only; merge targets must be open/in_progress/blocked. The decision schema has no report ID, so Node must assess one report at a time and bind its response to that report; plural inputs do not provide report-to-decision correlation. Node still runs its own validation/confidence threshold before any PocketBase mutation. Confidence must be a finite number in the inclusive range 0..1; NaN and either infinity are rejected with HTTP 422. Successful HTTP output uses strict JSON serialization (no nonstandard NaN/Infinity literals). Low confidence is not automatically accepted by this bridge.

For the existing `AssessmentTransport.assess(messages)` interface, Node can parse `messages[1].content` and map:

```js
const supplied = JSON.parse(messages[1].content);
const payload = {
  reports: [{id: "report-context", context: supplied.untrustedReport}],
  labels: supplied.labels,
  open_todos: supplied.openTodos
};
// POST payload, then return response.assessment to validateAssessment().
```

Host-owned assessment guidance is the committed `guidance.v2.json` (version `2`), loaded by the actual structured LLM call with purpose `discord-todo.assess.v2`. Bump its version and purpose audit string together for policy changes. The caller's system prompt is not accepted as host instructions. Keep Node-reviewed guidance consistent with this version; editing Node's local guidance alone does not change bridge policy.

Policy v2: bugs are **red** for an economy exploit or an affected player unable to play; **blue** only when mildly disruptive **and** not expected gameplay; **yellow** between those definitions. Features default to blue and current validation requires blue. Split independent issues; merge only the same underlying actionable problem, not merely the same plugin. Use the supplied Untriaged label if the plugin cannot be identified. Confidence below `0.7` requires human intervention with no low-confidence association/label committed by Node. Incomplete evidence requires explicit uncertainty and clarification where it affects the decision. Reasons are concise, auditable evidence summaries, not chain-of-thought. The JSON decision schema is unchanged.

Failures: 401 missing/wrong/weak bridge token; 413 oversized body; 422 invalid input or invalid model output; 503 unavailable provider/transport/missing starter. No raw provider errors or secret-bearing exception text are returned. No todo collections are created or mutated.

## Operator activation (requires separate approval)

Nothing in this worktree is installed or enabled. After review and deployment authorization:

- Install this directory plugin in the intended profile using the supported plugin installation workflow.
- Put a random 32+ character `HERMES_TODO_BRIDGE_TOKEN` in the profile secret source and configure that **same bridge-only token** in Node's secret source.
- Enable the intended profile's API-server platform with its own required strong `API_SERVER_KEY`; bind loopback unless separately secured. This plugin's route authenticates itself rather than assuming custom routes inherit API-server auth.
- Use `hermes config set` for settings, not a manual config-file edit. Required settings: `plugins.entries.discord-todo-assessment.allow_gateway_injection = true`; `plugins.entries.discord-todo-assessment.settings.allowed_parent_ids = ["1560943737688039474"]`.
- Ensure this profile has its connected Discord adapter, correct profile routing, authorized developer user IDs, and bot permissions to view/post in the new thread. This does not require `DISCORD_ALLOW_BOTS`.
- Coordinate gateway activation/restart explicitly. Do not apply configuration to other profiles implicitly.
- Verify a real authorized click creates a new thread, supplies the opening context, produces a Hermes reply there, and a human reply continues the same durable session. That live smoke test was **not** run here because deployment/restart and a real coding run were not authorized.

## Capability/readiness admission gate

Run the full verification command below against the **exact runtime intended for activation**, before enabling or upgrading this integration. Its fresh-thread test intentionally injects without a previously admitted human message: a newer host enforcing that provenance restriction must fail this gate, not receive fabricated provenance, a hidden human-confirmation requirement, or a separate webhook fallback. The Node forum starter ping alone is not evidence that Hermes admitted a human message. There is no non-mutating public plugin injection-readiness API here; static version checks and plugin doctor do not prove that a real new thread can bootstrap. This offline gate proves the installed host's capability only, not live Discord connectivity/permissions, bot configuration, or deployment readiness. The installed host tested is `0ef671bbd4c` (Hermes `v0.21.5+7720.g0ef671b`). No live forum post or coding run was made.

## Verification

From any directory:

```sh
/home/mothermouse/.hermes/hermes-agent/.hermes/bin/hermes --run-module unittest discover -s /home/mothermouse/projects/hermes-discord-todo-bridge/custom_plugins/discord-todo-assessment -p 'test_*.py' -v
/home/mothermouse/.hermes/hermes-agent/.hermes/bin/hermes plugins validate /home/mothermouse/projects/hermes-discord-todo-bridge/custom_plugins/discord-todo-assessment
/home/mothermouse/.hermes/hermes-agent/.hermes/bin/hermes plugins doctor /home/mothermouse/projects/hermes-discord-todo-bridge/custom_plugins/discord-todo-assessment --ci
```

The complete suite passes **12 tests** on the installed runtime, including policy regressions that inspect instructions actually delivered to `ctx.llm`, returned guidance version `2` and purpose `discord-todo.assess.v2`. These policy tests were observed failing against v1 before the policy change. Tests exercise actual aiohttp loopback HTTP and injected provider fixtures. The host integration test uses the **installed** PluginContext, GatewayRunner, BasePlatformAdapter, authorization, durable sessions and transcript path, replacing only the model boundary and external Discord transport. It configures a real guild + forum-parent route to a served coding profile and proves that injection runs in that profile, empty initial history, opening prompt context, reply to the target thread, and a human follow-up reusing the same durable session ID with both turns read back from the real durable transcript. The model fixture returns `agent_persisted=False` so the gateway actually writes the user/assistant rows; omitting this flag can yield a misleading empty transcript. It does not pretend to test Discord's remote API or a live model.

The installed PM runtime does not include pytest, so this focused artifact uses stdlib unittest without modifying any active PM environment.

## Authoritative references checked

- https://hermes-agent.nousresearch.com/docs/developer-guide/plugin-llm-access
- https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins#injecting-messages
- https://hermes-agent.nousresearch.com/docs/developer-guide/plugins (platform handler factories)
- Installed `hermes_cli/plugins.py`, `hermes_cli/plugins_injection.py`, `gateway/run_plugin_injection.py` and existing host origin-injection tests.
