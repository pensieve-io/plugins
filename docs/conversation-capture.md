# Optional work conversation capture

Capture saves new visible work in Postgres for future team handoffs. It is
**off by default**, controlled for each person in Pensieve **Settings →
Agent transcripts**. One **Save agent transcripts** toggle controls all of your
supported agents and devices.
Installing or updating the plugin never opts you in.

Saved work belongs to the selected context and is intended for its members.
Only enable this for work you want to share. The pilot has no transcript
browser, search/read tools, summaries, embeddings or automatic knowledge
extraction. No selected context means no upload. A → B → A saves separate
portions without copying the whole session to both companies. Resuming the
same host conversation appends to its existing logical record.

## Set up a device

1. Sign in to Pensieve with the same account used for this client's MCP login.
2. [Open personal transcript settings](https://app.pensieve.uk/dashboard/contexts?modal=settings&settings_section=agent-transcripts), enable **Save agent transcripts**, and choose
   **Set up device**. This downloads a uniquely named JSON file with an upload-only key.
3. In a terminal, from your installed Pensieve plugin folder, run the command
   shown in settings, using the actual downloaded file path:

   ```sh
   python3 scripts/capture_setup.py ~/Downloads/pensieve-capture-DEVICE_ID.json
   ```

4. Delete the downloaded setup file, then start or resume your work session.

An agent can help locate the plugin folder, but do not paste the setup file or
key into chat. The importer makes no network calls, opens no sign-in flow and
never reads host credentials. It stores the key in
`~/.config/pensieve/capture.json` with mode `0600`, under a private `0700`
directory. It preserves profiles for other accounts; one setup works across supported
clients on this device.

Settings is the only opt-in control. Turning capture off stops new and queued
uploads for every client on every device. Re-enabling starts a new consent period;
it never backfills the disabled period or retries an older period's backlog.
Saved content remains until explicitly deleted. **Remove device** revokes only that
upload key, including queued retries; it does not disconnect MCP or delete
saved history. Device IDs in settings match their downloaded setup filenames.

The version-2 private config holds `user_id` and `upload_key` profiles.
Keys cannot read transcripts or call MCP tools. The server checks current
consent, key status and membership for every batch. `PENSIEVE_CAPTURE_CONFIG`
and `PENSIEVE_CAPTURE_STATE` may override local paths; no preference flag or
secret belongs in the hook/marketplace definition.

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
existing 16 MiB spool bound and is pruned on later hooks after explicit retirement.
The new prompt must confirm the same account, context and consent generation;
the source segment must not have been explicitly retired. The adapter neither
reads the source transcript nor substitutes its latest head. This supports
forks at captured messages inside a turn as well as completed turns.

Missing source spools, uncaptured/legacy boundaries and boundaries ending on
unindexed internal records leave ancestry unknown. Claude's tested fork records
retain message UUIDs but do not identify their source conversation, so its
fork starts a new capture chain without re-uploading copied history. Rewinds
reported as an in-place Codex rollback also break lineage. Graph storage and
resolution of these references are a later application PR.

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

Saved transcripts remain indefinitely until explicitly deleted. Successful
receipts carry `expires_at: null`; this helper accepts that value, clears cached
legacy deadlines and preserves resume/fork anchors regardless of age. Disconnecting
stops uploads without deleting saved history. A scoped `410 deleted` erases the
named local backlog and anchors without moving deleted content to a new segment.
The helper still understands finite receipts and explicit `410 expired` responses
from older servers during the coordinated rollout; local time alone never retires
saved history or its ancestry.

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
