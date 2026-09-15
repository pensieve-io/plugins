# Optional work conversation capture

Capture saves visible work conversations in Pensieve so their owner can find
earlier work and link it to tasks. It is **off by default**. Installing or
updating the plugin alone does not upload a conversation.

The first implementation supports the tested local macOS Claude Code and Codex
CLI contracts. Desktop, ChatGPT Work, Cowork, ordinary chat tabs, Windows and
ephemeral sessions without a local transcript are not verified capture surfaces.
An installed marketplace entry or working MCP connection does not establish
capture support.

## Enable for a specific account and company

1. In Pensieve's conversation capture settings, create an upload-only key for
   the intended company. Review the sharing and retention settings there.
2. Download its config. Place it at `~/.config/pensieve/capture.json` with owner
   read/write permissions only (`chmod 600 ~/.config/pensieve/capture.json`).
   Keep downloaded copies private, and delete surplus copies when configured.
3. Enable the plugin hooks and start or resume a conversation. Select the
   company through Pensieve. Capture begins with newly observed turns; it does
   not import earlier conversations or backfill the resumed conversation.

The config supports several independently authorized scopes:

```json
{
  "version": 1,
  "profiles": [
    {
      "user_id": "your-Pensieve-member-UUID",
      "context_id": 497,
      "upload_key": "your-private-upload-only-key"
    }
  ]
}
```

Use the downloaded UUID and key. Never paste a key into a conversation, plugin
manifest, project settings or source control. Keys authorize uploads for one
member/company, not transcript reads or ordinary MCP tools. The helper does not
read the host's OAuth tokens, run another MCP server or sign in again.

`PENSIEVE_CAPTURE_CONFIG` can select another private config file.
`PENSIEVE_CAPTURE_STATE` can select another private state directory. These
variables contain paths, never credentials. The default state directory is
`~/.local/state/pensieve/capture` (mode `0700`); its SQLite files are `0600`.
Keep state outside the installed plugin directory so updates preserve retries.

## What is saved

- Visible user and assistant text.
- Tool names and visible text results. Arbitrary tool input arguments are
  excluded because they often carry environment values or credentials.
- An omission notice for attachments. The helper does not follow file paths,
  upload attachment bytes or open artifact files.

Reasoning, encrypted reasoning, system/developer messages, hook payloads and
compaction internals are excluded. Configured upload keys and common bearer,
API-key, password and private-key patterns are redacted. Pattern redaction
cannot recognize every secret in arbitrary prose; keep capture disabled for
work that should not be retained.

Each event is limited to 32,000 characters, with an explicit truncation notice.
Batches contain at most 100 events and 256 KiB of UTF-8 JSON. Unsupported raw
records over 1 MiB pause parsing without discarding their cursor; they are not
guessed into a visible event.

## Company and account boundaries

The authenticated Pensieve service emits a non-secret context marker for every
new user prompt and each successful context selection. The helper accepts
markers only in recognized host hook records or results of the actual Pensieve
`set_context` call. It matches client, conversation, member and company to the
configured key. Codex prompt markers also match the native turn ID.

Every new user message needs a fresh prompt marker. A missing marker, an
unmatched turn or ambiguous ordering keeps that text pending locally; a later
unrelated marker never assigns it to the final selected company. Selecting a
company without a matching profile is capture-off for that scope. Switching
accounts does not let the new account's unmarked turns use an earlier key.

## Checkpoints, retries and limitations

`SessionStart` establishes the initial cursor. `UserPromptSubmit` and `Stop`
checkpoint complete records. `SessionEnd` attempts a short flush within the
host's deadline; it marks inactivity, not immutable completion. Resuming uses
the same logical conversation. The application's continuation operation can
link a new/forked conversation to earlier work without copying runtime state.

Each committed source position gets a stable event identity, so identical
messages at different positions remain distinct. The helper retains exact
batch bytes and their ID until the server returns the matching SHA-256 receipt
and accepted event count. An interrupted upload retries those same bytes.
Activity sequence numbers ensure an older queued end cannot supersede newer
resume activity. Unknown boundaries are retained separately from uploadable
events. A changed/truncated transcript file pauses capture instead of guessing
how the old cursor maps to the new file.

The local spool is limited to 16 MiB per conversation. A full spool pauses
reading at the first record that cannot fit and still retries durable uploads;
unacknowledged events are never evicted to make room. Space is reserved for the
upload batch so an offline backlog can drain after connectivity returns.
Transcripts that the host later deletes cannot be recovered
from a cursor alone. There is no background daemon: pending work retries at a
later supported hook. Closing the host permanently, killing it before a final
record is durable, or losing the machine can leave the final work unsaved.

## Disable, revoke and delete

Remove a profile from the config to stop that scope's uploads; an empty profiles
list or absent config disables all capture. Existing private pending data stays
on disk and can be removed by deleting its conversation's SQLite file. Removing
config does not delete conversations already saved in Pensieve.

When a supported hook observes capture disabled, it records that transition
without reading the transcript. Re-enabling starts at a fresh baseline and does
not import messages from the disabled interval; already-consented pending
uploads retain their original scope and can retry.

Revoke the key in Pensieve to prevent further uploads, including delayed retries.
Use Pensieve's conversation deletion controls to delete saved work. Server-side
membership, revocation, deletion and retention are rechecked on uploads and
reads; local retries cannot bypass them. A server-confirmed expired/deleted
segment (HTTP 410) erases that segment's local pending content, batches and title,
and retains a content-free tombstone. A later fresh prompt can start a new
segment; old work is not replayed into it. A revoked/forbidden key retains its
backlog for owner action while other configured scopes can continue uploading.
Unknown, unattributed local rows have no automatic age-based cleanup in this
version; they remain inside the per-conversation size bound until the owner
removes that conversation's SQLite file. Do not remove another conversation's
state to work around a rejected upload.

## Maintainer validation

Run the package tests and installed synthetic CLI probes:

```sh
python -m pytest
python3 scripts/probe_claude_hooks.py --capture
python3 scripts/probe_codex_hooks.py --persist --capture --output /tmp/pensieve-capture-start
```

The Codex probe's `--persist` writes only its synthetic conversation to Codex's
normal session store. To verify a resume with the same durable capture cursor,
use the reported thread ID and pass
`--resume ID --capture-state /tmp/pensieve-capture-start/capture-spool` with a
new output directory. Inspect only that named synthetic session. Probe config,
upload keys, model responses and HTTP services are synthetic; no user config is
loaded, no real OAuth key is read and no paid model call is made.

Live authenticated fresh-install/update checks and each desktop surface remain
separate release acceptance work. Do not publish a capture claim for an untested
host or release the plugin before compatible server support is deployed.
