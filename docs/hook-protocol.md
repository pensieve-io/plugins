# Hook and server contract

The client package calls the hosted Pensieve MCP service. The server owns
authentication, context selection and company content; the client owns hook
registration and recognition of context the host actually accepted.

## Briefing request

The MCP tool is `context_briefing`. Hook adapters send:

- `client`: `claude` or `codex`.
- `session_id`: the host's conversation UUID.
- `event`: `SessionStart` or `UserPromptSubmit`.
- `source`: the host's startup source where available, including `clear` or
  `compact`.

Claude's hook server name is `plugin:pensieve:pensieve`; Codex's is `pensieve`.
Ordinary MCP calls remain available alongside these hooks.

The response's text is JSON containing `hookSpecificOutput.hookEventName` and
`hookSpecificOutput.additionalContext`. An unchanged, acknowledged briefing may
have empty additional context. **`event="SessionStart"` forces a fresh briefing**
even if the server still holds an acknowledgement of pre-compaction content.
The helper requests that event when transcript loss, a bounded tail or a failed
reset means the old acknowledgement can no longer establish current grounding.

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
credentials. A reset or missing receipt can cause a repeated briefing. Hook
execution order and transcript writes are not assumed to be synchronous.

Both repositories test their side of this contract. Client receipt/probe tests
live here; the server repository tests forced recovery, receipt fencing,
authentication and conversation selection without importing client code.

## Optional conversation capture

Capture has separate authorization; delivery receipt tokens remain receipt-only.
Personal Settings → Connected clients owns per-user/client consent and device
keys. The setup command only imports a downloaded upload-only credential into
private storage. It never reads OAuth credentials. Every upload checks current
membership, matching client and current enabled consent generation.

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
The last hook has a one-second configured timeout and no native MCP companion:
Codex does not support native MCP hooks at SessionEnd. Same-event handlers may
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
`segment_id`, and `accepted_events`, and include a valid `conversation_id` and fixed `expires_at`.
Retries preserve the original batch bytes. A failed or mismatched receipt does
not remove pending events. HTTP 401/403 retains the denied scope's backlog and
allows other configured scopes to proceed; HTTP 410 securely removes the
expired segment's old content and retains a local tombstone. A scoped expiry
response carries `reason: expired`, `segment_id` and `expires_at`; only a proven
fresh post-expiry user-turn suffix can roll into a new segment. A scoped
`reason: capture_disabled` response discards the rejected segment's queued work
without rollover, including old-generation retries after re-enable. See
[capture setup and boundaries](conversation-capture.md).
