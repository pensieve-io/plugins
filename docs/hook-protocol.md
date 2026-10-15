# Hook and server contract

The client package calls the hosted Pensieve MCP service. The server owns
authentication, context routing and company content; the client owns hook
registration and recognition of context the host actually accepted.

## Briefing request

`context_briefing.py` calls `POST https://mcp.pensieve.uk/hooks/briefing`
using the device credential registered through MCP sign-in in
`Authorization: Bearer`. There is no briefing MCP tool and no agent fallback
that calls one. Native MCP discovery registers the private installation proof
alongside verified OAuth; transcript sharing remains independently optional.

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
content, enforces current membership and permissions, and owns each
conversation's briefing context and capture destination. No company content is
cached locally.

Installation credentials are user/client scoped and follow the conversation's
briefing context, subject to current membership and permissions. Tools route by
an explicit `context_id` (reads may omit it with one context; writes require it); the briefing
follows the context those tools last used. The helper
stores no context content or token from the harness. Older contextual credentials
never widen their scope; upgrading registers a fresh installation.

Claude ordinary calls provide a tool-use ID but no conversation ID. A synchronous
`PreToolUse` command registers `{client: "claude", session_id, tool_use_id}` at
`POST /hooks/tool-binding`. The following OAuth-authenticated MCP request
resolves that exact ID from `_meta["claudecode/toolUseId"]`. Missing bindings
never reuse another conversation's transport. The hook matches the plugin's own
`mcp__plugin_pensieve_pensieve__*` tools, checking native plugin provenance when
the host supplies it, the claude.ai connector's `mcp__claude_ai_Pensieve__*`
tools and a server added by hand as `pensieve` (`mcp__pensieve__*`). A binding is
correlation only: the server still authenticates each call's OAuth account. With
a credential, expected failures deny the call. Codex supplies `_meta.threadId`
directly and needs no per-tool binding request.

The server records a conversation's company exposure only for calls it can tie
to that conversation: Codex's `threadId` or a bound Claude tool-use ID. A host
command timeout fails open, and other server names are never bound, so those
calls reach the server unattributed. The capture helper's transcript check is
the backstop: it reads every company call's `context_id` itself and stops saving
locally at a second context.

The plugin's dynamic-header helper creates its private proof before MCP connects
and emits only `X-Pensieve-Plugin: <client> <proof>` as header JSON. Claude uses
`headersHelper`; Codex uses the same source embedded in `http_headers_helper`
because its HTTP helper has no plugin-root working directory. Native OAuth still
supplies Authorization. Authenticated initialize/list requests bind the proof to
the OAuth user, independently of tools and conversation IDs. `/hooks/connection`
returns the hook's authenticated identity using that proof as bearer.

Before registration, hooks ask for native MCP login; they do not start a separate
browser login or block initial tool discovery. Rejected proofs never rotate
automatically. Explicit reset clears that harness's cached profile, fences queued
work and creates a fresh proof. Account mismatch revokes the old installation;
reconnect cannot reactivate it. Native OAuth logout alone does not revoke hooks.
No new agent-visible tool participates in connection or capture.

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
authentication and per-conversation capture locking without importing client code.

## Optional conversation capture

Capture requests identify themselves as `Pensieve-Plugin-Capture/1.0`, following
the receipt helper's explicit identification so the production edge admits them.

Capture uses the restricted installation proof and explicit transcript consent;
delivery receipts remain receipt-only. Every upload checks live membership,
client, enabled consent generation and key revocation. Registration grants no
sharing choice. See the capture guide for migration and reconnect boundaries.

At every `UserPromptSubmit`, including an unchanged briefing, the service emits
one terminal, non-secret marker in accepted hook context:

```text
<!-- pensieve-capture-context {"v":2,"capture_generation":"UUID-or-null","kind":"prompt","user_id":"UUID","client":"codex","conversation_id":"UUID","context_id":497,"turn_id":"UUID"} -->
```

`context_id` is the conversation's capture destination, never its briefing
context: null before capture locks to the first company briefing or tool
exposure, and null for the rest of the conversation once they use a second.
`capture_generation` is null when disabled
or consent lookup is unavailable. False → true starts a new generation. `turn_id` is nullable; Codex supplies its
native turn ID, while the Claude adapter currently uses null. `prompt` is the
only marker kind; tool results never carry one. Markers reflect the
authenticated snapshot of that exact call. They never contain credentials. The
helper does not treat a quoted marker as authorization.

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

Native MCP login authorizes the installation during discovery. No pairing claim
or one-time exchange is required. The script resolves its owner privately through
`/hooks/connection` before loading a briefing.
Once a key exists and the native consent marker reports `unknown`, the helper
opens `https://app.pensieve.uk/oauth/conversation-capture?client=<client>&context_id=<id>&user_id=<id>`
on macOS, where Approve or Deny records the sharing choice. It asks at most once
per conversation and not again within ten minutes; a decline is remembered and
never re-asked. Native consent markers and server upload admission independently
require an enabled, current generation. The settings toggle changes the same
preference without another approval screen. The obsolete
connection-intent API and `pensieve-capture-setup` marker are no longer produced or
used; older marker text is still stripped from visible captured messages.
