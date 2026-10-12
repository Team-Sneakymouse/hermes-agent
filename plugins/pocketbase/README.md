# PocketBase backend plugin

A standalone, opt-in plugin maintained in the **Team-Sneakymouse Hermes fork**;
it is not a proposed upstream third-party integration. No core modifications,
new dependencies, migrations, deployment, or gateway restart are required by this PR.

## Operator setup (after review)

Set `POCKETBASE_HOST`, `POCKETBASE_USERNAME`, and `POCKETBASE_PASSWORD` in the
active profile's secret environment / `.env`. Authentication uses the PocketBase
`_superusers/auth-with-password` endpoint (PocketBase 0.23+). Use HTTPS; unencrypted
HTTP is accepted only on loopback for local development. Redirects are refused.

For the fork's bundled plugin, explicitly run `hermes plugins enable pocketbase`
and select the `pocketbase` toolset for **new** sessions. Alternatively, copy this
whole directory into the intended profile's `plugins/pocketbase/` directory and
enable it there. Do not install the entire Hermes repository as a standalone
plugin. This PR does not enable the plugin or change running sessions.

Credentials are resolved per call with Hermes's profile-scoped `get_secret` API;
authentication tokens never appear in tool results. Missing credentials hide the
tools. Response credential keys and matching password/token values are redacted;
backend error bodies are never returned. Successful record contents are otherwise
returned intentionally, so limit the information you ask for with `fields`.

## Tools

- `pocketbase_records`: `list` (filters, sort, fields, page/per_page), `get`,
  `create`, `update`, `preview_delete`, `delete`.
- `pocketbase_collections`: `list`, `get`, `create`, `preview_update`, `update`.
  Creation supports base/auth collections. Updates accept `fields`, `indexes`,
  and `listRule`, `viewRule`, `createRule`, `updateRule`, `deleteRule`.
  Fields/indexes are complete replacement arrays, not patches by position.
  Omitted rules remain unchanged; `null` locks a rule; `""` makes it public.
  System collection records, collection deletion, renaming, type conversions,
  file uploads, realtime subscriptions and auth-provider configuration are outside scope.

Ordinary requested record creates/updates and new-collection creation are direct.
**All existing-schema updates** take the reviewed-change-set path (conservative:
additive and destructive changes share the same one-approval-per-collection UX).

## Deletion and schema approval

```text
preview_delete(collection, record_id)
  OR preview_delete(collection, date_field, cutoff, timezone, optional filter)
    read schema and ALL collection-schema pages → reject incoming cascades → freeze IDs (no writes)

delete(action, same collection, snapshot_id)  # no other fields
  consume snapshot atomically
  ask host human gate ONCE for this exact collection + operation
  verify actual human-response event, expiry and unchanged collection schema/graph
  delete only frozen IDs → report actual deleted IDs and failures (no retries)

preview_update(collection, data)
  read current schema → freeze reviewed change set
update(action, same collection, snapshot_id)  # no other fields
  ask host human gate ONCE → revalidate → PATCH only frozen change set
```

Bulk previews use an exclusive `<` cutoff. Supply an ISO date-time **including a
UTC offset** and an IANA timezone matching that offset, e.g.
`2026-10-01T00:00:00+02:00` / `Europe/Berlin`. The preview and human prompt include
collection, count, date field, original cutoff and timezone. The preview also
returns the UTC cutoff and exact frozen IDs. A single-record preview has no date
cutoff and explicitly lists its one ID.

Snapshots expire after five minutes, are process-local and single-use, and bind
profile, session, host, credentials, collection ID, operation and schema digest.
Deletion also binds the full collection graph digest and re-reads every schema
page after approval. Incoming `cascadeDelete` relations, including self-relations,
are rejected rather than expanding approval to other collections or records.
Expiry is checked after verification IO before PATCH and before each DELETE.
If TTL expires mid-loop, results list deleted, failed and unattempted IDs.
A reload, restart, changed scope, expired snapshot, failed/denied approval or
attempted replay requires a new preview. Bulk previews are capped at 10,000 IDs /
20 pages, with 100 pending snapshots per plugin instance; expired entries are
removed on new previews. Concurrent changes during pagination are rejected when
counts/IDs disagree. Enumeration is **not** a database transaction: approval
covers the frozen IDs, not a future re-evaluation of the filter.
Totals are frozen on the first page; equal-count concurrent replacements cannot
be detected. Schema revalidation is also an unavoidable check-then-write race:
schemas can change after verification and before or during writes. This is not
a database transaction. Enforcing unchanged schemas at write time requires
backend conditional writes or a transactional server-side operation.

This calls the real `tools.approval.request_tool_approval` gate inside the handler,
not a model-supplied `confirmed` flag or a fail-open `pre_tool_call` hook. The
plugin also requires the host's `post_approval_response` event for that exact
random, per-attempt rule key. Consequently `yolo`, approvals-off, unattended
approve, missing approval observers and missing human bridges **do not authorize
destructive writes**. Host session/always buttons may be displayed, but each key
is unique to a single attempt, so they cannot approve another operation. Choose
"once". A supported in-process CLI/gateway/API-server human bridge is required;
isolated plugin hosts without that bridge fail closed.

PocketBase is authenticated as a superuser and therefore bypasses collection API
access rules; the human gate is not a substitute for restricting who may invoke
this toolset. Operators should keep it out of untrusted sessions.

A transport failure or HTTP 5xx write response may leave its outcome uncertain.
Errors classify uncertainty by request method; deletion failures include an
`uncertain` flag and an explicit warning to inspect the target and never
automatically retry. Never retry it blindly;
inspect the exact target read-only before deciding on a new operation. Deletion
results separate successful IDs from failed IDs, including uncertainty messages.
No code path automatically retries writes or silently broadens approval scope.

## Verification

```sh
HERMES_PYTHON=/path/to/test-venv/bin/python scripts/run_tests.sh \
  tests/plugins/test_pocketbase.py tests/tools/test_request_tool_approval.py
# Optional disposable real PocketBase server, never the configured production host:
PATH=/path/to/pocketbase-bin:$PATH HERMES_PYTHON=/path/to/test-venv/bin/python \
  scripts/run_tests.sh tests/plugins/test_pocketbase_live.py
```

The unit suite uses a loopback scripted HTTP backend and real Hermes discovery
and gateway approval bridges. The optional integration suite starts an isolated
PocketBase process under pytest's temporary directory, uses test-only credentials,
and terminates it in fixture cleanup. It never reads production PocketBase
configuration. Tested against PocketBase **0.40.5**, downloaded from the official
release with the published SHA-256 verified.
