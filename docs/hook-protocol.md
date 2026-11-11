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

The briefing carries no git state. The conversation's own transcript link
arrives inside its ordinary `additionalContext`; other conversations on the
agent's branch arrive after a shell command instead, through
[branch recall](#branch-recall).

Every briefing also sends `hook_revision`, an integer (`HOOK_REVISION` in
`context_briefing.py`, currently 1). It is bumped whenever hook behaviour the
service relies on changes, such as a new request field or hook; a service whose
minimum is higher adds one line to the briefing asking the member to update the
plugin. A service older than `hook_revision` refuses unknown fields with 422, so
on a 422 the script asks once more in the same hook run without it, keeping
`client`, `session_id`, `event`,
`delivery_id`, `force_refresh`, `source` and `turn_id`. The retry uses the
remainder of a 9-second budget inside the 10-second hook timeout, and is skipped
when less than half a second remains.

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

Claude desktop hands its claude.ai connectors to Claude Code under opaque server
ids (`mcp__<uuid>__<tool>`) and drops `_meta["claudecode/toolUseId"]` on the way
out, so a binding cannot resolve those calls; the claude.ai relay itself keeps
the id, as Claude Code CLI shows. The same desktop session also runs the
plugin's own server, whose calls do carry it. The opaque connector id proves
neither provider identity nor account equivalence, so hooks neither steer the
model between the two nor deny or redirect connector calls. A company call through
the desktop connector therefore stays unattributed: for a member of several
contexts, saving does not start from it, and the transcript check still stops
saving at a second context. This is a Claude desktop limitation, reported
upstream, and only arises when both the connector and the plugin are installed.

Every existing PreToolUse denial prints the same reason on stderr and in the
structured decision, covering older hosts that only surface stderr on exit 2.

The server records a conversation's company exposure only for calls it can tie
to that conversation: Codex's `threadId` or a bound Claude tool-use ID. A host
command timeout fails open, and other server names are never bound, so those
calls reach the server unattributed. The capture helper's transcript check is
the backstop: it reads company calls' explicit `context_id`, or the canonical
Pensieve URLs in `read.identifier`, and stops saving locally at a second context.
Exposure detection is deliberately conservative; it is never used to establish
the identity of a connection or redirect its execution.

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
The last hook has a one-second configured timeout on Claude and three seconds on Codex. Same-event handlers may
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


## Conversation anchors

`git_anchor.py` is a `PostToolUse` command hook on the shell tool, which both
hosts name `Bash` (matcher `^Bash$`; Codex 0.166's `post-tool-use.command.input`
carries `cwd`, `tool_input.command` and `tool_response`). It links a saved
conversation to the commits, pushes and pull requests its agent makes:

| Command (also with `git -C <dir>`, `-c k=v`, env prefixes, `cd <dir> &&`, chained with `&&`, `;`, `\|` or newlines) | `kind` | Recorded |
| --- | --- | --- |
| `git commit` | `commit` | `HEAD` after the command, current branch |
| `git push` | `push` | `HEAD`, current branch |
| `gh pr create` | `pull_request` | the last `https://<host>/<owner>/<repo>/pull/<n>` in the tool output, `HEAD`, current branch |

`gh pr view`, `gh pr merge` and every other command are ignored, as is a quoted
or echoed `git commit`. At most three anchors are sent per command.

It posts `POST https://mcp.pensieve.uk/hooks/conversations/anchors` (the upload
endpoint plus `/anchors`, with the same loopback-only fixture override) with
`Authorization: Bearer <the conversation's upload key>`,
`X-Pensieve-Capture-Protocol: 2` and `User-Agent: Pensieve-Plugin-Capture/1.0`:

```json
{"client": "claude", "host_conversation_id": "<hook session_id>", "context_id": 497,
 "kind": "commit", "repo_url": "https://github.com/o/r.git", "branch": "main",
 "commit_sha": "<40 hex>", "pr_number": null, "pr_url": null, "tool_call_id": "toolu_…",
 "turn_id": null}
```

Every field is always present; `turn_id` is the hook's `turn_id` (Codex; null
for Claude Code, which gives none), the id its saved events carry, because
Codex's `tool_use_id` (`exec-…`) is not the call id its transcript saves.
`tool_call_id` is the hook's `tool_use_id` (null
when absent or not a capture id), the same id the captured tool call carries, so
branch recall can open the conversation at the turn that ran the command.
`repo_url` and `branch` follow the
[repository identity](#repository-identity) rules, and a request over 2,048
bytes is not sent. 200
(`{anchor_id, conversation_id, created}`) is idempotent. Each request has a
3-second timeout within a 4-second budget. 401, 403 (`service_hold` or an
unavailable key), 410 `capture_disabled` and 422 are reported on stderr only.

An agent often commits in a conversation's first turn, before its first upload
at Stop, when the service could only answer 404 `{"reason": "not_saved"}`. While
no upload in the conversation's context has been accepted, the hook therefore
queues the anchor instead of posting it; it also queues one answered 404, 426,
429, 5xx or not at all. The queue (`anchor_queue.py`) is
`<client>-<session>.anchors.json` beside the conversation's spool: a private
JSON list of at most 20 entries (the oldest go first), deduplicated by kind,
commit and pull request URL, written atomically under its own lock, holding
each anchor body and its owner only. Entries older than 24 hours are dropped.

After the capture hook's flush (the Stop that uploads the first turn, or any
later hook), pending anchors whose context now holds an accepted upload are
posted with that scope's upload key, within the same 3-second requests and a
4-second cap inside the hook's own deadline. 200 and permanent refusals (401,
403, 404 now that the transcript is known to be saved, 410, 422) remove an
entry; no answer, 426, 429 or 5xx keep it for the next flush. A stopped
conversation's queue is discarded unsent.

The conversation is the hook's own `session_id`, never one inferred from the
working directory, so a commit by a person or by another conversation in the
same repository is never attributed to it. Nothing is sent unless:

- the host reports the command finished successfully: failed commands are
  filtered by exit status (`exit_code`, or Codex's `Exit code: N` output),
  interruption, background execution and, for commits, the HEAD guard below;
- for `commit`, the work tree's newest `HEAD` reflog entry is a commit made
  within the last ten minutes, so `git commit …; echo done` with nothing to
  commit does not anchor an older commit, a pull or a checkout;
- the command was not a `--dry-run`;
- the directory the command ran in can be followed: a `cd` inside a subshell
  ends where the subshell closes, and after a `cd` to a path holding a shell
  expansion (`cd "$REPO"`) nothing is anchored until an absolute `cd`;
- the command ran inside a work tree with an `origin` remote;
- the conversation's transcript is being shared: it has a local spool that is
  not stopped; the newest native capture marker in its own transcript (or, with
  none, the spool's attributed scope) names a context with an enabled consent
  generation; that context's latest upload was not held (`service_hold`) or
  refused (`forbidden`); the generation's segment is not retired or disabled;
  and a key is configured for that scope. Anything else is dropped, not queued.

While saving is paused on the machine (`paused.json`, see
[the capture guide](conversation-capture.md#pausing-on-this-machine)), the hook
posts nothing and queues nothing. The spool is read read-only without its lock;
any error, timeout or unreachable service fails open. Anchors never add model
context: the hook's only output is [branch recall](#branch-recall), and `{}`
otherwise.

## Repository identity

`git_workspace.py` describes the work tree a command ran in, with a 300 ms
timeout per git call. `repo_url` is `git remote get-url origin` with any
userinfo, query and fragment removed
(`https://x-access-token:…@github.com/o/r.git` → `https://github.com/o/r.git`);
scp-style `git@host:o/r.git` is sent unchanged for the server to normalise, and
a local-path or `file://` remote, a directory outside a work tree, a missing
`origin`, or any git failure or timeout means no repository at all. `branch` is
`git symbolic-ref --short -q HEAD`, null when detached; an anchor's `commit_sha`
is the 40-hex `HEAD`. Credentials never leave the device.

## Branch recall

The same `PostToolUse` hook tells the agent which other saved conversations
worked on the branch it has just arrived on, straight after the command that
got it there: at its first shell command in a repository, or right after a
`git checkout`, `git switch`, `gh pr checkout` or a `cd` into another work tree,
mid-turn rather than at the next prompt. The briefing never carries this list.

After every shell command (`branch_recall.py`), the hook takes the directory
the command worked in: the target of its last `git` (including `git -C <dir>`)
or `gh` command, otherwise the directory after its last `cd`, otherwise the
payload's `cwd`. It reads that directory's [repository identity](#repository-identity)
and, for a named branch, looks it up among those already recalled for this
conversation. Only a `{repo_url, branch}` not yet answered is asked about, so
returning to a branch never repeats its list, and never
the repository's default branch (this clone's `origin/HEAD`, else `main` or
`master`; no network call): most conversations start there, and what was
committed straight to it is rarely the work at hand.

`POST https://mcp.pensieve.uk/hooks/recall` (loopback fixtures as for the
briefing), with the briefing's own `Authorization: Bearer` installation
credential, never an upload key, and at most 2,048 bytes:

```json
{"client": "claude", "session_id": "<hook session_id>",
 "workspace": {"repo_url": "https://github.com/o/r.git", "branch": "feature/x"}}
```

| 200 `status` | Meaning | Hook output | Asked again |
| --- | --- | --- | --- |
| `listed` | Other saved conversations anchored to that repository and branch | `{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "<the service's text>"}}` | Never in this window |
| `empty` | Grounded, nothing on that branch | `{}` | Never in this window |
| `unavailable` | No company chosen yet, a service hold, or recall failed | `{}` | After 60 seconds |

Any other status, an invalid body, no answer or a timeout counts as
`unavailable`. The hook rebuilds the output from `additionalContext` alone,
since Codex refuses unknown output fields; both hosts add PostToolUse
`additionalContext` to the model's context after the tool result (Codex as
developer context, under its `additionalContextLimit`).

The recall state is `<client>-<session>.recall.json` beside the briefing state:
a private file holding up to 20 `{repo_url, branch}` pairs, each with its status
and when it was asked, written under its own lock. `unavailable` is recorded
before the request is sent, so a hook the host kills mid-request backs off for
the same minute. A command finding the lock held asks nothing,
so parallel commands never inject the list twice. SessionStart (startup,
resume, clear or compact) deletes it, so a new or compacted window is told
again. Nothing is asked for a detached HEAD, outside a work tree with `origin`,
before the briefing hook has connected the installation, or with less than half
a second of the hook's 4-second budget left after anchors; an unasked branch is
asked by the next command. Recall reads only: it needs no transcript sharing
and, like the briefing, still runs while saving is paused. Any error fails open
and never changes or delays the command.

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
