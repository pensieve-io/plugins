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
