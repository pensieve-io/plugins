# Automatic work conversations

This candidate requires the companion conversation-knowledge app release before
publication. Capture remains **off by default**. Pairing the plugin or updating
it never enables capture or company knowledge contribution.

The global **Save agent transcripts** control still applies to all of a person's
supported agents and devices. The Conversations page owns company contribution
and retention choices. Connection approval explicitly shares captured work
conversations with the selected company, including the saved transcript.
Extracted knowledge can contribute to the company's existing context layer.
Installing an upload key grants no transcript-reading or MCP authority.

Work belongs to the selected Pensieve context. No selected context means no
upload. A → B → A saves separate portions without copying the whole session
between companies. Resuming the same host conversation preserves its identity.
The plugin does not extract company knowledge itself; the hosted processing
service owns that policy and the connection to curated Pages.

## Connect in the browser

1. Install the Pensieve plugin in a supported agent and sign in to Pensieve.
2. Ask the agent: **“Use Pensieve’s connect-conversations skill.”**
3. Open the approval link it shows, review the company and settings, and approve
   using the same Pensieve account as the agent's MCP connection.
4. Continue working. The helper completes pairing privately. If the short wait
   ends before approval, the next ordinary agent hook finishes it automatically.
   The Conversations page shows the last actual upload separately from pairing.

There are no setup files, pasted credentials, exports or user terminal commands
in this connection flow. The agent runs the bundled helper. Its model-visible
output contains only a non-secret approval URL and safe status. A pairing lasts
for the server's short expiry period, can be claimed once, and is bound to the
invoking client. An expired, revoked or already-claimed exchange requires a new
pairing. If the exchange response is lost after the server claims it, start a
new pairing rather than exposing the old secret for recovery.

The helper stores credentials in `~/.config/pensieve/capture.json` with mode
`0600`, under a private `0700` directory. Pending pairing secrets live in a
separate private file and are removed after completion or expiry. It never reads
host OAuth credentials. Codex and Claude have separate credentials, and profiles
for different accounts survive reconnects. A one-off migration binds a legacy
pilot credential to the first invoking client; other clients must pair separately.

The version-3 config holds client-scoped account profiles and installation IDs.
`PENSIEVE_CAPTURE_CONFIG` and `PENSIEVE_CAPTURE_STATE` can override local paths
for isolated fixtures. Keys and preference flags never belong in hook manifests.
Turning the global capture setting off stops new and queued uploads. Re-enabling
starts a new consent period without importing the disabled interval. Revoking a
device stops its credential, without disconnecting MCP or deleting saved work.
Re-pairing with a replacement key drops the old local unsent backlog and starts
from a fresh prompt. Already accepted server history remains. Company
contribution is separate from capture and is enforced by the service.

## Pairing protocol

All pairing requests use the fixed
`https://api.pensieve.uk/users/me/conversation-capture` base, reject redirects,
and send the explicit `Pensieve-Plugin-Pairing/1.0` user agent. Only HTTP loopback
fixtures can override the base.

- `POST /pairings/start` sends client, runtime, label, plugin version and host
  version. It returns `id`, `poll_secret`, `verification_url`, `expires_at` and
  `poll_interval_seconds`. Only the approval link and expiry are displayed.
- The user approves at `/oauth/conversation-capture?pairing_id=<public UUID>`.
  Pairing creates an installation key but does not itself enable capture or
  contribution.
- `POST /pairings/{id}/exchange` sends the polling secret directly. A pending
  response keeps the private challenge. An approved response returns the
  account, company, installation ID and upload key once. The helper atomically
  installs the credential without sending it through agent output.
- Hooks can report `POST /installations/heartbeat` using the upload key. Only
  runtime metadata and a transcript-available flag are included. Existing hooks
  cannot distinguish every CLI/desktop runtime, so their heartbeat reports
  `unknown` rather than claiming desktop verification.

Retries use one bounded exchange per eligible hook, respect the server poll
interval and share a private lock with interactive setup. There is no daemon.

## Import previous work automatically

In Conversations, choose the connected installation, company, project folder
and how far back to import. This is permission to share the matching saved work
with that company. The plugin does not guess which company an old chat belongs
to. No chat exports, uploads or terminal commands are required.

The installed helper fetches authorised requests at ordinary agent hooks and
resumes bounded work after live capture. The setup skill can also run a bounded
sync directly. An idle or unsupported app cannot receive a browser request by
itself. The UI must distinguish waiting for the app, running and completed.
Large imports resume on later hooks; no permanent background process is installed.

Only standard local stores are scanned: Codex's `~/.codex/sessions` and
`~/.codex/archived_sessions`, and Claude Code's `~/.claude/projects`. The selected
folder is compared with the transcript's native recorded working directory,
including descendants. It is never opened as an arbitrary filesystem source.
Symlinked stores, directories and files are excluded. A folder change inside a
conversation is respected. Missing working-directory or timestamp evidence is
not guessed. Only history still retained locally can be imported; deleted files,
cloud-only chats and history on another machine are unavailable.

The server issues a short-lived import grant with the exact company, client,
capture generation and original-time window. `GET /installations/history-imports`
uses the installation's upload key and returns `{imports: [...]}`. Each grant
contains `id`, `context_id`, `client`, `capture_generation`, nullable
`publication_generation`, nullable `since`, `until` and `project_path`.
Upload batches use the normal upload endpoint with an added `history_import_id`
and the exact approved `history_project_path`.
`POST /installations/history-imports/{id}/progress` reports `state`,
`processed_conversations`, `processed_events` and a content-free `error_code`.
Progress counts processed records, including already-known events.

Original timestamps are preserved. Event identities match live capture's host
conversation and committed byte position, so the server can deduplicate retries
and overlapping imports. An import gets its own deterministic segment identity.
Events already assigned to another company cause an explicit conflict rather
than being silently moved. Exact pending batches and parser cursors are saved
atomically in private local SQLite state, then sent. Revoked grants clear pending
content on the next successful poll. Failed, deleted or expired uploads never
roll old history into a newly authorised segment.

Each pass has a time and byte budget. Discovery is capped at 10,000 files and
10,000 directories; oversized or unreadable records fail the request visibly.
These limits do not change the existing future-only live scanner.

## What is saved

- Visible user and assistant messages.
- Tool names and visible text results; arbitrary tool arguments are excluded.
- Artifact references and attachment omission notices, without opening files.

Reasoning, system/developer instructions, hook payloads and compaction internals
are excluded. Native Pensieve transcript read/list results and searches that
request `node_types: ["transcript"]` are excluded too, including mixed searches.
This uses the native call arguments and persists only a result-exclusion flag;
search queries are never saved in capture state. Ordinary Page/Data searches
remain eligible. Configured credentials and common secret patterns, including
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
Successful native Pensieve page mutations and `save_data` results can include
an opaque server receipt. Only the matching native result event carries it;
quoted receipts, failed calls and aggregate output cannot supply provenance.
The server validates account, company, client and conversation before linking
the actual write to its transcript evidence. Receipt markers are removed from
visible transcript text.

SessionStart establishes a baseline, prompt/Stop checkpoints work, and
SessionEnd attempts a short best-effort flush. Immutable events and exact-byte
receipts tolerate retries, resume and out-of-order delivery. There are no
message edit revisions or explicit presence updates. The hosted service tracks
newly accepted events for inactivity-based processing. The private SQLite spool is
bounded at 16 MiB per conversation, retaining valid unacknowledged work when
full. Pending work retries at later hooks; there is no background daemon.
Final work can be lost if the machine or local transcript disappears.
Each upload may use the remaining hook budget, so ordinary hosted receipt
latency does not pin the queue to an already accepted batch. The host deadlines
remain unchanged; SessionEnd still uses its shorter best-effort budget.

Retention is explicit. A successful upload receipt includes `expires_at`,
which is null for history kept until deletion. Older pilot portions retain their
original fixed 90-day deadline unless their owner chooses a different policy;
resume does not extend a fixed deadline. The helper accepts both forms.
An expired portion can roll forward only from a proven fresh post-expiry user
turn. A `deleted` response discards that portion's local backlog without rolling
its old content into a replacement. Capture-disabled responses do the same.
Server tombstones prevent retry resurrection. Deleting raw history, stopping
capture and withdrawing shared company knowledge remain separate service actions.

## Supported clients and verification

The tested adapters target local macOS Codex CLI and Claude Code CLI.
[Claude Code desktop](https://code.claude.com/docs/en/desktop) documents shared
hook settings, and [Codex/ChatGPT Work](https://developers.openai.com/plugins/guides/submit-claude-plugin)
documents command hooks. This package still needs live acceptance in each
desktop runtime. [Cowork supports hooks](https://support.claude.com/en/articles/13837440-use-plugins-in-claude),
but its local or cloud execution environment does not guarantee access to the
host stores above. Ordinary Claude Chat and ordinary ChatGPT Chat do not provide
this capture contract. MCP connectivity and installing skills do not prove
transcript access. Windows and ephemeral sessions remain unverified.

Package tests cover browser pairing, private storage, one-off config migration,
client/account isolation, retry, nullable retention, expiry, unchanged live baselines,
explicit historical grants and native mutation provenance. Synthetic installed-client probes are
in [client-probes.md](client-probes.md). Live authenticated browser pairing, fresh installation, updates and resumed
sessions remain release checks.
Deploy the companion app/API/MCP/scheduler before publishing the plugin feature.
