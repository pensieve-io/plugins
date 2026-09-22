# Optional work conversation capture

Capture saves new visible work as ordered `TranscriptTurn` nodes in the selected
context's Neo4j database. Postgres owns consent, upload receipts and temporary
staging. Saved conversations are visible to context members through the existing
search/read tools. Installing the plugin alone never enables sharing.

## Connect an installation

Ask the installed plugin to **use Pensieve's connect-conversations skill**.
The agent starts the bundled helper and shows a Pensieve browser approval link.
Choose a context, review the sharing checkbox and click **Connect**. The checkbox
covers saving conversations and extracting useful company knowledge. A previous
learning opt-out leaves it unchecked until you make a fresh choice. Extraction
also requires the separately gated application worker.

The helper installs an upload-only credential privately; the user does not run
commands, download a credential file or paste tokens. Approval applies to this
client installation and context. The remote MCP connection is unchanged. The
helper never reads the harness's OAuth credentials, and neither the upload key
nor the temporary polling secret reaches the agent or browser approval page.

Pairing expires after ten minutes. Ordinary hooks finish pending approval
without a background service. If the one-time exchange succeeds at the server
but its response is lost, start fresh approval; a consumed key is not returned
again. `paired` proves local setup, not successful capture.

The version-3 config stores client-scoped profiles under
`~/.config/pensieve/capture.json` (mode `0600`, private directory `0700`). Existing
version-2 account profiles keep their previous consent until replaced by new
pairing. Pairing migrates obsolete local credential entries without touching
transcript spools. The application temporarily retains legacy Personal Settings
and key revocation while its Conversations management replacement is built.
Remove a key there to stop that installation's uploads and extraction; MCP tools
remain connected. The old downloaded-file setup applies only to older plugins.

Keys cannot read transcripts or call MCP tools. The server checks client,
context, membership, current consent generation and key status on every batch.
`PENSIEVE_CAPTURE_CONFIG` and `PENSIEVE_CAPTURE_STATE` can override local paths;
secrets never belong in hook manifests.

### Offline and reconnect behaviour

MCP OAuth expiry alone does not revoke the separate upload key. Already
attributed durable batches retry on later hooks after a network interruption.
New turns need their own authenticated context marker; work without one is not
silently assigned to the last context or backfilled on MCP reconnect. Disconnect
revokes uploads, including queued retries. Re-pairing establishes a fresh
baseline and cannot authorize an older key's backlog.

This stage does not import historical conversations. An explicit history-import
flow is separate work. Transcripts expire after 90 days; selected extracted
company knowledge may remain unless explicitly withdrawn.

## What is saved

- Visible user and assistant messages.
- Tool names and visible text results; arbitrary tool arguments are excluded.
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
remains distinct. Tool arguments remain excluded.

Predecessors describe retained visible events, not every internal host record.
They survive upload acknowledgement and resume. Account, context, consent,
retention and unknown attribution boundaries break the chain. A plugin upgrade
preserves existing spool rows and exact queued batch bytes; it never enriches
already captured events or reconstructs older turns. Metadata starts with the
next observed prompt. These references are client reports, not authorization
or server-authenticated evidence of a successful tool write.

Codex forks can link to the exact `forked_from_ordinal_exclusive` boundary when
that native record has a captured endpoint in this device's source spool.
A content-free ordinal index survives acknowledgement, stays within the
existing 16 MiB spool bound and is pruned on later hooks after expiry/retirement.
The new prompt must confirm the same account, context and consent generation;
the source must have a known, unexpired retention deadline. The adapter neither
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
remain incomplete. Native terminal records can be written **after** Stop;
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

Each portion expires **90 days after its first accepted upload**; resume does
not extend it. The server erases bodies, titles and receipts, retaining only
content-free tombstones against retry resurrection. A fresh post-expiry user
turn can begin a new portion of the same conversation. If expiry is discovered
while work is queued, only a host-timestamped post-expiry turn can roll over.
Account/context deletion removes its stored content. The pilot has no individual
transcript delete action.

## Supported clients and verification

The pilot targets local macOS Codex CLI and Claude Code. Desktop, ChatGPT Work,
Cowork, ordinary chat tabs, Windows and ephemeral sessions without a local
transcript require separate verification. MCP connectivity does not prove capture.

Package tests cover credential import, private storage, client/consent boundaries,
retry, expiry and no-backfill behavior. Synthetic installed-client probes are
in [client-probes.md](client-probes.md). Live authenticated personal-settings
setup, fresh installation, updates and resumed sessions remain release checks.
Deploy the companion app/API/MCP/scheduler before publishing the plugin feature.
This turn-metadata candidate specifically requires #973's additive database
column and compatible upload service to be deployed before plugin publication.
Transcript content still goes to Postgres in this stage; Neo4j storage,
retrieval, successful-write links and extraction remain separate PRs.

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
