# Hook and server contract

The client package calls the hosted Pensieve MCP service. The server owns
authentication, context selection and company content; the client owns hook
registration and recognition of context the host actually accepted.

## Briefing request

`context_briefing.py` calls `POST https://mcp.pensieve.uk/hooks/briefing`
using the device credential registered through MCP sign-in in
`Authorization: Bearer`. There is no briefing MCP tool and no agent fallback
that calls one. The member's signed-in MCP call authorises briefing reads when
it registers the device (see enrolment below); transcript sharing remains
independently optional.

Hook adapters send `client` (`claude` or `codex`), the host `session_id` UUID,
and the actual `event` (`SessionStart` or `UserPromptSubmit`). Optional `source`
records startup/clear/compact; Codex's `turn_id` preserves native capture
attribution. A private `delivery_id` UUID is stable during the host run and
renews at SessionStart. `force_refresh` restores grounding after compaction,
missing transcripts or a bounded tail that cannot establish delivery. It does
not change the event, so a recovered prompt still receives its capture marker.

The HTTP response is hook JSON containing `hookSpecificOutput.hookEventName`
and `hookSpecificOutput.additionalContext`. A root-rendering fallback also sets
`briefing_available: false`; the script consumes that field before forwarding
the hook JSON. Full and unchanged responses omit it. Before network work the
script records an unfinished refresh. Failure, interruption or unavailable root
keeps that flag, forcing the next successful briefing to replace the unavailable
notice even when an older briefing was acknowledged. Success clears the flag.
It acknowledges accepted receipts synchronously before requesting a refresh;
other capture handlers may run concurrently. The server assembles company
content, enforces current membership and permissions, and owns durable
conversation selection. No company content is cached locally.

Credentials are client/context scoped. A 409 `pairing_required` response names
the authenticated account and currently selected Context. The helper uses an
already registered matching credential, or keeps a pending claim for that exact
destination for the next signed-in Pensieve tool call to register. It never
resets the conversation to fit an available credential. Initial account
selection uses a prior accepted native marker or private conversation identity;
with multiple accounts, the conversation's signed-in MCP call registers the
account rather than the helper guessing which one the host means. Definitive
401/403 key rejections may try another registered credential for that same account,
within the hook deadline. A successful lifecycle fetch remembers the good
credential's fingerprint so subsequent calls do not keep choosing a revoked key.
Other failures do not justify switching keys. If every key rejects,
reconnection pins the account but allows a currently available Context, rather than
forcing a deleted membership. The server still owns conversation selection.

Automatic pairing records its initiating conversation in private local
state. Any capture or briefing hook may finish the shared exchange; before the
pending claim is removed, it saves the chosen account and Context in that
conversation's private receipt. Only the initiating conversation adopts that
registration, once per claim. A capture/Stop hook or another open conversation
winning the exchange cannot lose or redirect it. These receipts contain
identifiers only, never credentials or company content.

Claude ordinary calls provide a tool-use ID but no conversation ID. A synchronous
`PreToolUse` command registers `{client: "claude", session_id, tool_use_id}` at
`POST /hooks/tool-binding`. The following OAuth-authenticated MCP request
resolves that exact ID from `_meta["claudecode/toolUseId"]`. Missing bindings
never reuse another conversation's transport selection. The hook matches only
`mcp__plugin_pensieve_pensieve__*` and checks native plugin provenance when the
host supplies it. With a credential, expected failures deny the call; server
enforcement is also necessary because host command timeouts fail open. Codex
supplies `_meta.threadId` directly and needs no per-tool binding request.

Without a briefing credential, the helper keeps one private pending claim and
binds it to what its host is about to send to Pensieve's MCP server:
`POST /hooks/enrolment` with `{pairing_id, poll_secret, client, session_id}`,
plus the exact `tool_use_id` for Claude. Enrolment sends no `Authorization`
header; the claim's poll secret is its only proof. Claude binds in `PreToolUse`
and always allows that call; Codex binds its thread from the lifecycle hook. The
following OAuth-authenticated MCP request registers the claim for the signed-in
account and Context, and the next lifecycle or capture hook exchanges it
(status `registered`) for a scoped device key. A 410 discards the claim so the
next call starts afresh; other failures keep it for a retry.

Old local profiles without explicit `briefing_enabled` remain upload-only and
connect again through MCP sign-in. A profile gains briefing permission only when
the server's `registered` exchange confirms it; the retired browser `approved`
status is never accepted. Pairing works before any briefing exists, eliminating
a bootstrap cycle.
Failure notices describe reconnect/retry without asking the agent to call a
briefing tool or revealing a credential.

## Delivery receipt

Briefings carry an opaque marker:

```text
<!-- pensieve-delivery token=<64 lowercase hex characters>.<32 lowercase hex characters> -->
```

The helper recognises only accepted hook-context records belonging to the
current host conversation. It posts `{ "token": "...", "operation": "ack" }`
or `"operation": "reset"` to `https://mcp.pensieve.uk/hooks/delivery`.
The server accepts at most 512 bytes, returns 204 for accepted or unknown/expired
receipts, and fences stale nonces. The token authorises only delivery bookkeeping;
it does not authorise access to company data.

Receipt requests contain no transcript text, local file contents or OAuth
credentials. They identify themselves with the fixed `User-Agent`
`Pensieve-Plugin-Receipt/1.0`: the service sits behind Cloudflare, whose
Browser Integrity Check rejects Python's default `Python-urllib/…` signature
with error 1010 before the request reaches the server. A reset or missing
receipt can cause a repeated briefing, and a receipt the service does not accept
is reported on the helper's stderr (failure class only) so the host's hook
record shows it. Hook execution order and transcript writes are not assumed to
be synchronous.

Both repositories test their side of this contract. Client receipt/probe tests
live here; the server repository tests forced recovery, receipt fencing,
authentication and conversation selection without importing client code.

## Optional conversation capture

Capture requests identify themselves as `Pensieve-Plugin-Capture/1.0`, following
the receipt helper's explicit identification so the production edge admits them.

Capture has separate authorization; delivery receipt tokens remain receipt-only.
The first Pensieve tool call after MCP sign-in registers the device, and a
private one-time exchange installs a client/context-scoped device key. Ordinary
hooks complete pending pairing. No OAuth credential is read or passed through model output.
Every upload checks membership, the enabled consent generation and the key's
scope/revocation. Legacy account profiles retain their previous policy until
replaced; see the capture guide for migration and reconnect boundaries.

At every `UserPromptSubmit`, including an unchanged briefing, the service emits
one terminal, non-secret marker in accepted hook context:

```text
<!-- pensieve-capture-context {"v":2,"capture_generation":"UUID-or-null","kind":"prompt","user_id":"UUID","client":"codex","conversation_id":"UUID","context_id":497,"turn_id":"UUID"} -->
```

`context_id` is null when unselected; `capture_generation` is null when disabled
or consent lookup is unavailable. False → true starts a new generation. `turn_id` is nullable; Codex supplies its
native turn ID, while the Claude adapter currently uses null. Successful
`set_context` results carry the same marker with `kind="selection"`. Markers
reflect the authenticated selection snapshot of that exact call. They never
contain credentials. The helper does not treat a quoted marker as authorization.

Command capture hooks establish a baseline at `SessionStart`, checkpoint at
`UserPromptSubmit` and `Stop`, and attempt a bounded flush at `SessionEnd`.
The last hook has a one-second configured timeout. Same-event handlers may
run concurrently, so a checkpoint can be completed by the following hook.

Uploads use `POST https://mcp.pensieve.uk/hooks/conversations` with the key in
`Authorization: Bearer`. A batch carries `batch_id`, `client`,
`host_conversation_id`, `segment_id`, `capture_generation`, `events`, `title`,
and the attributed `context_id`. Every immutable event has `event_id`,
`sequence`, `kind`, `content`, `occurred_at` and `truncated`. There are no
presence updates or message revisions.

Limits are 100 events, 32,000 characters per event and 262,144 bytes for the exact
UTF-8 JSON request. The helper sends only nonempty batches. A successful HTTP
200 receipt must match `batch_id`, `batch_sha256` of the exact raw request,
`segment_id`, and `accepted_events`, and include a valid `conversation_id`.
`expires_at` must be present: `null` means indefinite storage; a valid legacy
aware timestamp is also accepted but never drives local expiry or rollover.
Retries preserve the original batch bytes. A failed or mismatched receipt does
not remove pending events. HTTP 401/403 retains the denied scope's backlog and
allows other configured scopes to proceed. HTTP 410 with a matching `segment_id`
and `reason: deleted` or `reason: capture_disabled` erases that segment's queued
content and keeps a local tombstone, including old-generation retries after
re-enable. Nothing from that segment is replayed under a new identity, including
later queued turns or a provisional prompt. Only a newly observed, attributed
user turn can start a new segment. Unknown or mismatched 410 responses cannot
erase the queue. See
[capture setup and boundaries](conversation-capture.md).


## Hook registration and sharing

The first Pensieve tool call after MCP sign-in registers the helper before any
company content is read; registration does not change sharing. The helper
privately exchanges its one-time poll secret for the linked profile; a
`registered` response is valid even while sharing is off. Only unregistered
claims expire, and an unclaimed registration is preserved while offline.
Once a key exists and the native consent marker reports `unknown`, the helper
opens `https://app.pensieve.uk/oauth/conversation-capture?client=<client>&context_id=<id>`
on macOS, where Approve or Deny records the sharing choice. It asks at most once
per conversation and not again within ten minutes; a decline is remembered and
never re-asked. Native consent markers and server upload admission independently
require an enabled, current generation. The settings toggle changes the same
preference without another approval screen. The obsolete
connection-intent API and `pensieve-capture-setup` marker are no longer produced or
used; older marker text is still stripped from visible captured messages.
