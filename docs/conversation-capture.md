# Optional work conversation capture

Capture saves new visible work to the selected Pensieve context so its members
and agents can search/read earlier conversations. It is **off by default**.
Installing or updating the plugin alone never uploads conversation text.

Once enabled for your account, capture follows the selected context. **Every
current member of that context can read its captured portions.** There is no
private mode, sharing toggle or conversation delete action. No selected context
means no upload. A session that switches A → B → A saves the first and third
portions to A and the second to B; it does not copy the entire session to both.
Resuming the same host conversation updates the same logical archive record.

## Set up in the plugin

Run the bundled helper from the installed plugin directory in an interactive
terminal. You can ask your agent to locate that directory; no key needs to be
copied into the conversation. The commands, relative to the plugin root, are:

```sh
python3 scripts/capture_setup.py enable
python3 scripts/capture_setup.py status
python3 scripts/capture_setup.py disable
```

`enable` explains sharing/retention and asks you to opt in. It opens the existing
Pensieve sign-in/consent flow in your browser using its own OAuth PKCE grant,
then stores an upload-only key in `~/.config/pensieve/capture.json` with mode
`0600`. Its containing directory must be private (`0700`). Start or resume your
work session afterward. Neither the app's settings page nor manual credential
copying is required. This setup is separate from your host's MCP login; use the
same account. The helper never reads the host's OAuth credentials and never
stores its own temporary OAuth access/refresh tokens.

Capture begins with newly observed work. It does not import older sessions or
backfill a resumed conversation. `disable` removes local capture configuration;
it neither deletes already-shared history nor revokes keys on other computers.
Every enable issues a new upload key. Changed key fingerprints reset the live
capture baseline, even if no hook ran while capture was disabled; already-durable
previously enabled work can still retry.

To manage upload credentials, including a lost computer:

```sh
python3 scripts/capture_setup.py keys
python3 scripts/capture_setup.py revoke KEY_UUID
```

These commands use browser sign-in. Listing shows key IDs/labels, never secret
keys. Revocation stops that key's uploads, including queued retries. Use `keys`
to identify and revoke an unused key before re-enabling if the account key cap
is reached. `PENSIEVE_CAPTURE_CONFIG` can select another private config file;
`PENSIEVE_CAPTURE_STATE` selects the private spool directory. Both contain paths,
not credentials. Keep these outside the plugin installation so updates preserve
configuration and pending uploads.

The private config supports multiple accounts, each with `user_id` and
`upload_key`. The server authenticates that account and checks its current
membership in every upload's `context_id`. A configured account needs no separate
profile for each context. An unconfigured account never uses another account's
key. Upload keys cannot read transcripts or call MCP tools.

## What is saved

- Visible user and assistant messages.
- Tool names and visible text results; arbitrary tool arguments are excluded.
- Artifact references and attachment omission notices, without opening files.

Reasoning, system/developer instructions, hook payloads and compaction internals
are excluded. Known credentials and common secret patterns are redacted, although
pattern matching cannot identify every secret in arbitrary prose. Each event is
limited to 32,000 characters with explicit truncation. Batches hold at most 100
events / 256 KiB. Unsupported oversized records pause parsing without guessing.

## Attribution and reliability

The service emits authenticated, non-secret prompt/selection markers. The helper
accepts only recognised native hook records and actual Pensieve MCP-call results,
matching account, client, conversation and (for Codex) turn. Unknown or ambiguous
attribution stays local. A later marker never assigns earlier work wholesale to
its context. Codex code mode uses native completed MCP-call records; combined
`exec`/`wait` output is omitted because it may span several contexts.

SessionStart establishes the baseline, prompt/Stop checkpoints durable work and
SessionEnd attempts a short best-effort flush. Stable event identities and exact
batch receipts tolerate retries, resume and out-of-order delivery. The private
SQLite spool is bounded at 16 MiB per conversation and pauses scanning when full,
without evicting unacknowledged events. There is no background daemon: pending
work retries at later supported hooks. Losing the machine or host transcript can
leave final work unsaved.

Each saved context portion expires **90 days after its first accepted upload**;
resume does not extend it. Expired bodies and titles are erased, leaving a
content-free tombstone that rejects old retries. Receipts include the deadline.
A fresh post-expiry user turn starts a new portion under the same logical
conversation. If expiry is discovered after an offline backlog accumulated,
only a suffix beginning with a host-timestamped post-expiry user turn can move.
Old or untimestamped work is never resurrected. User/context deletion also removes
its stored content through the existing account/context lifecycle.

## Supported clients and verification

The tested capture contracts are local macOS Codex CLI and Claude Code. Desktop,
ChatGPT Work, Cowork, ordinary chat tabs, Windows and ephemeral sessions without
a local transcript require their own verification. A working MCP connection does
not establish capture support.

Run package tests and synthetic installed-client probes as described in
[client-probes.md](client-probes.md). Setup tests exercise a real loopback callback,
including idle browser connections, wrong OAuth state, PKCE, private file storage
and disabled capture. They use synthetic identity/upload services, not real
accounts. Live authenticated fresh installation, update, resume, membership
changes and expiry remain release acceptance requirements. The companion server
must be deployed before this plugin feature is published.
