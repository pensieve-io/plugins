# Optional work conversation capture

Capture saves new visible work as immutable original events in Postgres.
Ordered Neo4j `TranscriptTurn` nodes and search passages are rebuildable projections.
Postgres also owns consent, upload receipts and deletion. Saved conversations are visible to context members through Sources and the
Context viewer. Existing agent search/read tools support them; agent searches
include transcripts only when explicitly requested. Installing the plugin alone never enables sharing.

## Connect an installation

Native MCP authentication authorizes the installed hooks during connection
initialization/discovery. The supported header helper sends a private installation
proof alongside the harness's OAuth bearer; it creates the proof before connecting,
so login does not depend on a prior hook. The server binds its hash to the verified
user and harness. Scripts can then load briefings without a model tool or a second
device-login page. The helper never reads the harness's OAuth credentials.

On the first undecided user/context/harness use, the plugin opens a separate
Approve/Deny page. Both answers persist across conversations and devices. Closing
without choosing allows a later prompt in another conversation after ten minutes.
The page checks that the browser account matches the authenticated hook account.
Declining keeps briefing enabled. Uploads require a fresh enabled-consent marker;
the server also checks explicit consent and its generation on every batch.
Extraction requires the separately gated application worker.

The header proof lives in `~/.config/pensieve/mcp-headers-{client}.json`, with the
authenticated profile in `~/.config/pensieve/capture.json` (files `0600`, directory
`0700`). It is client-bound and follows the member's selected context; context
switches do not create keys. No secret enters model text, tool arguments or the
consent page. The script resolves its owner through `/hooks/connection`, then
uses the same proof for briefing, exact Claude call binding and opted-in upload.

Native login never widens existing upload-only keys. After verified registration,
obsolete local profiles are replaced and credential cutovers prevent old work
being imported. A revoked proof cannot be reactivated by reconnecting. Use the
[explicit reset command](../README.md#reset-a-connection) to rotate it, remove that
harness's profile and fence dormant queues before reauthentication. Native account
mismatch revokes the previous installation. Clearing OAuth alone does not revoke
this persistent installation credential; revoke it in Pensieve to stop hooks.

**Data → Connectors** shows a shared card per harness. It is Connected when any
current member has linked a hook, even if everyone has sharing off. The people count
expands current contributors. Your + becomes a cog when sharing is on. Both open the
same modal, changing only your user/context/harness preference. Without a registered
hook, the toggle and Save are disabled with a plugin-setup tooltip and docs link.
After either choice on the sharing page, you can change sharing directly with the toggle
and Save. No separate connection button or repeat approval is needed. Turning off
stops your uploads and extraction across installations; your hook stays linked,
MCP access and other members' sharing stay unchanged, and enabling starts fresh.

Keys cannot read transcripts or call MCP tools. Briefing-enabled keys may
fetch the Context briefing and register Claude tool-call correlation. The server checks client,
context, membership, current consent generation and key status on every batch.
`PENSIEVE_CAPTURE_CONFIG` and `PENSIEVE_CAPTURE_STATE` override script paths for
fixtures; a custom MCP configuration must point its helper at the same config
directory. Production manifests use the fixed private home directory and contain
no credential values.

### Offline and reconnect behaviour

MCP OAuth expiry alone does not revoke the separate upload key. Already
attributed durable batches retry on later hooks after a network interruption.
New turns need their own authenticated context marker; work without one is not
silently assigned to the last context or backfilled on MCP reconnect. Revoking the installation in Pensieve
revokes uploads, including queued retries. A new credential cannot authorise unread
work from before its cutover. Already durable batches retain their exact bytes and
original consent generation; the server accepts retries only while that scope and
generation remain authorised.

This stage does not import historical conversations. An explicit history-import
flow is separate work. Transcripts remain until explicitly deleted. Turning
sharing off can retain or delete your existing raw transcripts in that context;
extracted company knowledge remains.

## What is saved

- Visible user and assistant messages.
- Tool names, inputs and visible text results. Codex function arguments and
  custom-tool input, and Claude `tool_use.input`, pass through the same redaction
  and truncation as message bodies. Missing input is not inferred from results.
- Artifact references and attachment omission notices, without opening files.

Reasoning, system/developer instructions, hook payloads and compaction internals
are excluded. Configured credentials and common secret patterns, including
quoted JSON credential fields, are redacted; arbitrary prose can still contain
secrets. Events are limited to 32,000 characters with explicit truncation;
batches hold at most 100 events / 256 KiB. Raw transcript records over 1 MiB
are skipped without decoding their contents. The helper checkpoints that skip
within its scan budget, even across hook runs, then waits for a fresh user prompt
with its matching context marker before saving again. Earlier queued uploads
keep their original bytes and attribution.

## Attribution and reliability

Authenticated version-2 prompt/selection markers carry account, client,
conversation, context and current consent generation (or null when disabled).
Only recognised native hook records and actual Pensieve MCP results supply
attribution. Codex also matches its turn ID. Only an observed user prompt may
wait provisionally for its own marker; ambiguous/unassignable work is discarded.
Codex code mode uses native completed MCP-call records and omits combined
`exec`/`wait` output that could span contexts.

### Turn identity and lineage

Newly observed turns carry the additive `capture` envelope from
[Pensieve #973](https://github.com/pensieve-io/pensieve/pull/973): a turn ID,
an exact preceding captured event reference, native tool-call IDs on tool
events, and separate empty `turn_end` events when completion is known.
Codex supplies its native turn ID; Claude uses the initiating user message UUID.
Older records without a usable native ID use the stable prompt event ID.
Existing event IDs remain file-position based, so repeated identical text
remains distinct. Input-bearing calls add `capture.tool_name` and put their input
in the event's bounded `content`; older name-only calls keep their original shape.
Empty input is distinct from missing input. Inputs never enter the identity
metadata, and queued retry bytes and already acknowledged events remain unchanged.
The protocol guard requires a service accepting this field before any upload.

Predecessors describe retained visible events, not every internal host record.
They survive upload acknowledgement and resume. Account, context, consent,
deletion and unknown attribution boundaries break the chain. A consent generation
change within the same context waits for a fresh prompt. Switching contexts may
change generation too; the selection result and subsequent work belong to the
new context when its own current generation permits capture. A plugin upgrade
preserves existing spool rows and exact queued batch bytes; it never enriches
already captured events or reconstructs older turns. Metadata starts with the
next observed prompt. These references are client reports, not authorization
or server-authenticated evidence of a successful tool write.

Codex forks can link to the exact `forked_from_ordinal_exclusive` boundary when
that native record has a captured endpoint in this device's source spool.
A content-free ordinal index survives acknowledgement, stays within the
existing 16 MiB spool bound and is removed when a segment is retired.
The new prompt must confirm the same account, context and consent generation.
Stored legacy expiry dates do not rotate segments or invalidate fork anchors. The adapter neither
reads the source transcript nor substitutes its latest head. This supports
forks at captured messages inside a turn as well as completed turns.

Missing source spools, uncaptured/legacy boundaries and boundaries ending on
unindexed internal records leave ancestry unknown. Claude's tested fork records
retain message UUIDs but do not identify their source conversation, so its
fork starts a new capture chain without re-uploading copied history. Claude
may skip SessionStart for forks: an absent transcript at UserPromptSubmit also
establishes an empty baseline so the first fork turn is captured. Rewinds
reported as an in-place Codex rollback also break lineage. The application resolves retained references within the same author, client,
context and consent generation.

Codex's native `task_complete` and `turn_aborted` records certify completed and
interrupted turns respectively. Claude completion requires an `end_turn`
assistant message followed by a successful native Stop summary with no
continuation reason. A Stop invocation, tool result or process exit alone never
certifies completion. Claude interruption and unrecognised failure signals
remain incomplete. Claude's `[Request interrupted by user…]` notice, like its
local `/compact` and `/clear` output, is recognised by its prefix on a record
without prompt provenance and never opens a new turn, so an interruption keeps
the conversation in one segment. An invoked skill (`<command-message>`) also
lacks provenance but still opens its turn. Native terminal records can be written **after** Stop;
their marker uploads at a later checkpoint (including bounded recovery from another chat), while visible
content still uploads at Stop. There is no polling process to eliminate that
delay.

SessionStart establishes a baseline, prompt/Stop checkpoints work, and
SessionEnd attempts a short best-effort flush. Immutable events and exact-byte
receipts tolerate retries, resume and out-of-order delivery. There are no
message edit revisions or active/inactive updates. The private SQLite spool is
bounded at 16 MiB per conversation, retaining valid unacknowledged work when
full. Ordinary hooks reserve 0.6 seconds of their existing 2.5-second budget to retry up to eight other same-client spools, using a private round-robin cursor. Recovery uses only stored paths and native session identities; it never discovers host history. Missing or replaced transcript files cannot prevent already durable batches from uploading, while unseen content still requires the original file. Busy or damaged spools are skipped. SessionEnd keeps its 0.9-second budget and performs no cross-session recovery. There is no background daemon; recovery needs another hook on the same device.
Each upload may use the remaining hook budget, so ordinary hosted receipt
latency does not pin the queue to an already accepted batch. The host deadlines
remain unchanged; SessionEnd still uses its shorter best-effort budget.

Saved transcripts remain **until explicitly deleted**; they do not expire after
90 days. Deletion erases raw content and keeps content-free tombstones against retry
resurrection. Members can remove their own transcripts through Sources or the
connector's stop-sharing-and-delete choice. Extracted company knowledge remains.
Account/context deletion also removes its stored content.

## Supported clients and verification

The pilot targets local macOS Codex CLI and Claude Code. Desktop, ChatGPT Work,
Cowork, ordinary chat tabs, Windows and ephemeral sessions without a local
transcript require separate verification. MCP connectivity does not prove capture.

Package tests cover credential import, private storage, client/consent boundaries,
retry, deletion, indefinite retention and no-backfill behavior. Synthetic installed-client probes are
in [client-probes.md](client-probes.md). Live MCP sign-in registration, the sharing page, Connector settings, fresh installation, updates and
resumed sessions remain release checks.
The guarded helper may be merged before the companion application release:
it checks protocol support and preserves pending work until compatible services
are deployed. Functional acceptance still requires the complete application stack
through [Pensieve #996](https://github.com/pensieve-io/Pensieve/pull/996), its verified
body migration and current API/MCP/worker builds. See
[release order](../CONTRIBUTING.md#release-order). Storage, retrieval, verified
write links, extraction and naming remain application responsibilities.

### Capture state and saving status

`capture_adapters.py` converts native records into one typed event stream;
`capture_state.py` owns the four phases `ready`, `awaiting_attribution`, `capturing`
and `blocked`. Native tool/turn bookkeeping is separate from file positions,
credential cutovers and durable SQLite events/batches. Local legacy flags are
migrated without rewriting queued bytes or identities.

`capture_config.py` persists client-wide credential removals by scope. Each spool
applies a byte-position cutover when it next opens its known source, so restoring
the same key cannot import a disabled interval from a dormant conversation.
Unchanged scopes continue normally; frozen attributed batches retain their receipts.
The first observation also fences older spools across partial removal/restoration.
Removing an owner-wide fallback leaves unchanged explicit context keys authorised;
an explicit context revocation still wins. A new empty Claude file snapshots both
credentials and removal epochs so past revocations cannot discard its first turn.
No filesystem scan of host conversation history is introduced.

Before upload requests, `capture_protocol.py` checks the public
service `/capabilities` manifest: protocol 1, service type, native clients and
request bounds. No credentials are sent on this check. Absent/malformed/incompatible
manifests or service/network failure preserve exact batches.
Redirects and arbitrary service origins remain disallowed. Each process caches
checks for at most 30 seconds; the next lifecycle hook starts fresh. Servers also
reject unsupported `X-Pensieve-Capture-Protocol` versions with 426. A deleted or disabled segment returns a scoped 410; deployment route 404 remains retryable.

Connectors distinguishes Sharing enabled from Last saved, which records a
retained accepted server upload, not proof of an empty device queue. For a known
native session, inspect private local delivery status without network or body reads:

```sh
python3 pensieve/scripts/conversation_capture.py --client codex --status SESSION_UUID
```

The result contains per-segment context, queued count, last attempted delivery
status and last validated acknowledgement time (up to 100 segments). It never
prints transcript text or keys, creates a spool or infers saving from MCP login.


### Nested Codex write calls

The native `item_completed/McpToolCall` record identifies nested Pensieve
Page writes and `save_data` calls within an active `exec`/`wait` wrapper.
Capture retains the tool name and native call ID once, without arguments or
result bodies. It does not interpret a completed call as a successful write:
Pensieve #977 matches server changeset/job receipts before creating provenance.
Native records must match this thread and turn and identify the Pensieve server.

Codex emits these records at completion. If concurrent work crosses a context
switch, the backend's full identity match may leave a call unlinked; no combined
output or timing heuristic assigns it to another context. Sequential writes on
either side of a selection have native matching identities. Old captured events
remain immutable; this adds no backfill and changes no existing retry bytes.

### Remembered harness preferences

Consent belongs to a user, context and harness type. The authenticated native marker
carries approved/declined/unknown status. Declining sharing suppresses further
sharing offers; upgrading an old upload-only key separately requires a fresh
registration through MCP sign-in. Once a private profile exists, changing the
sharing preference never re-pairs it. Same-machine apps of one harness reuse
`~/.config/pensieve/capture.json`. A new computer connects through its own MCP
sign-in and applies the remembered choice; merely visiting the sharing page cannot
authorise a helper. Cloud runtimes are not supported by this macOS-only sharing prompt.
