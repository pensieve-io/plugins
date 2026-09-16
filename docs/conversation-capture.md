# Optional work conversation capture

Capture saves new visible work in Postgres for future team handoffs. It is
**off by default**, controlled for each person in Pensieve **Settings →
Connected clients**, separately for Codex and Claude Code across devices.
Installing or updating the plugin never opts you in.

Saved work belongs to the selected context and is intended for its members.
Only enable this for work you want to share. The pilot has no transcript
browser, search/read tools, summaries, embeddings or automatic knowledge
extraction. No selected context means no upload. A → B → A saves separate
portions without copying the whole session to both companies. Resuming the
same host conversation appends to its existing logical record.

## Set up a device

1. Sign in to Pensieve with the same account used for this client's MCP login.
2. Open personal **Settings → Connected clients**, enable the client, and choose
   **Set up device**. This downloads a uniquely named JSON file with an upload-only key.
3. In a terminal, from your installed Pensieve plugin folder, run the command
   shown in settings, using the actual downloaded file path:

   ```sh
   python3 scripts/capture_setup.py ~/Downloads/pensieve-codex-DEVICE_ID.json
   ```

4. Delete the downloaded setup file, then start or resume your work session.

An agent can help locate the plugin folder, but do not paste the setup file or
key into chat. The importer makes no network calls, opens no sign-in flow and
never reads host credentials. It stores the key in
`~/.config/pensieve/capture.json` with mode `0600`, under a private `0700`
directory. It preserves profiles for other accounts and clients.

Settings is the only opt-in control. Turning capture off stops new and queued
uploads for that client on every device. Re-enabling starts a new consent period;
it never backfills the disabled period or retries an older period's backlog.
Saved content keeps its original expiry. **Remove device** revokes only that
upload key, including queued retries; it does not disconnect MCP or delete
saved history. Device IDs in settings match their downloaded setup filenames.

The version-2 private config holds `user_id`, `client` and `upload_key` profiles.
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
batches hold at most 100 events / 256 KiB. Unsupported oversized records pause
parsing without guessing.

## Attribution and reliability

Authenticated version-2 prompt/selection markers carry account, client,
conversation, context and current consent generation (or null when disabled).
Only recognised native hook records and actual Pensieve MCP results supply
attribution. Codex also matches its turn ID. Only an observed user prompt may
wait provisionally for its own marker; ambiguous/unassignable work is discarded.
Codex code mode uses native completed MCP-call records and omits combined
`exec`/`wait` output that could span contexts.

SessionStart establishes a baseline, prompt/Stop checkpoints work, and
SessionEnd attempts a short best-effort flush. Immutable events and exact-byte
receipts tolerate retries, resume and out-of-order delivery. There are no
message edit revisions or active/inactive updates. The private SQLite spool is
bounded at 16 MiB per conversation, retaining valid unacknowledged work when
full. Pending work retries at later hooks; there is no background daemon.
Final work can be lost if the machine or local transcript disappears.

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
