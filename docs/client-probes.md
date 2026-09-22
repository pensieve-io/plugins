# Client hook probes

## Claude Code

Run from the repository root with Python 3.12 and the installed Claude Code CLI:

```sh
python3 scripts/probe_claude_hooks.py > /tmp/pensieve-claude-probe.json
python3 scripts/probe_claude_hooks.py --capture > /tmp/pensieve-claude-capture-probe.json
```

Verified on 15 September 2026 with Claude Code 2.1.267. The probe uses the
packaged hook adapter and helper with disposable local MCP/model endpoints
and a temporary `CLAUDE_CONFIG_DIR`. It does not load Pensieve credentials,
change personal settings or make paid model calls.

Its twelve assertions cover first-prompt grounding, the local startup primer,
conversation identity across resume and transport reconnection, grounding after
compaction and clear, ordinary-tool metadata, failed-hook retry, and receipt
acknowledgement/reset containing only the token and operation. Exit status zero
requires all twelve to pass; the JSON report records each result.

`--capture` adds a private synthetic upload config, durable local spool and
loopback upload service. Eight additional assertions cover the first user turn,
visible prompts/answers, exact-byte retry, inactivity flush, credential and
internal-payload exclusion, distinct event identities, and one segment across
the first prompt, resume and post-compaction prompt. All checks passed on the
same installed version. Claude creates a fresh transcript after `SessionStart`;
the capture helper records its proven empty baseline before that file exists.
Claude's typed compaction summaries and local command records are excluded.

## Codex CLI

These fixtures use the installed Codex executable, synthetic local MCP tools and
a local Responses server. They make no billed model calls and do not modify
the user's configuration or install a plugin. Run from the repository root:

```sh
python3 scripts/probe_codex_hooks.py --output /tmp/pensieve-hooks-start
python3 scripts/probe_codex_hooks.py --output /tmp/pensieve-hooks-compact --compact
python3 scripts/probe_codex_hooks_manifest.py pensieve --output /tmp/pensieve-hooks-manifest
python3 scripts/probe_codex_hooks_manifest.py pensieve --output /tmp/pensieve-hooks-portable --portable
```

Every output directory must be new. For a resume probe, add `--persist` to the
first runtime command, then run a second probe with `--persist --resume ID`,
using the returned `thread_id`. This explicitly writes the synthetic conversation
to the normal Codex session store; ephemeral probes do not persist it. Inspect
only the packaged session named in `mcp.jsonl`, rather than searching user chats.

### Verified on 15 September 2026

Installed `codex-cli 0.154.0`; matching upstream tag `rust-v0.154.0`, commit
`6b9826e3aa83b1a5947db50f4332cb9c65f1b340`.

| Boundary | Observed result |
| --- | --- |
| Startup | The first model request contains the bundled helper's local primer and the UserPromptSubmit MCP briefing. No startup MCP briefing runs. The deliberately different `structuredContent` marker is absent. |
| Ordinary tool identity | The hook and ordinary MCP call both received `_meta.threadId`, matching Codex's durable thread identifier. They used the same MCP process. |
| Resume | A new MCP process receives the original thread identifier, with the local primer and first-prompt briefing restored. |
| Compaction | SessionStart with `source=compact` supplies the briefing before the next model request. |
| Generated plugin | The real plugin reader found nine hooks (five grounding/receipt and four capture), the `pensieve` MCP server and all three skills using `.codex-plugin/plugin.json`. |
| Portable root manifest | Adding a valid root Agent Plugins manifest suppressed hook discovery. The portable MCP configuration still loaded; this is a hook limitation. |

The runtime fixture consumes the packaged `hooks/codex.json` and bundled
receipt helper. It substitutes only the temporary plugin path and loopback
receipt endpoint; the synthetic MCP server retains the packaged server/tool
names. Matchers, inputs, timeouts and command syntax stay unchanged. The separate
manifest probe verifies discovery; the runtime probe executes the adapters.
The process deadline allows Codex's two 45-second shutdown waits after a
completed turn. Grounding/receipt hooks retain five-second deadlines; capture
uses a shorter budget, including a one-second `SessionEnd` adapter. A timeout
preserves `events.jsonl` and `stderr.txt` for distinguishing delivery from exit.

### Optional capture runtime

```sh
python3 scripts/probe_codex_hooks.py --persist --capture --output /tmp/pensieve-capture-start
python3 scripts/probe_codex_hooks.py --persist --capture --resume ID --capture-state /tmp/pensieve-capture-start/capture-spool --output /tmp/pensieve-capture-resume
python3 scripts/probe_codex_hooks.py --persist --capture --fork ID --capture-state /tmp/pensieve-capture-start/capture-spool --output /tmp/pensieve-capture-fork
python3 scripts/probe_codex_hooks.py --persist --capture --interrupt --output /tmp/pensieve-capture-interrupt
python3 scripts/probe_codex_hooks.py --persist --capture --compact --output /tmp/pensieve-capture-compact
python3 scripts/probe_codex_hooks.py --persist --capture --context-switch code-mode --output /tmp/pensieve-capture-switch
```

Use the first command's returned thread ID in the second. Six capture assertions
cover visible prompts/answers, exact-byte retry after a synthetic 503, captured visible work,
credential exclusion, internal-payload exclusion and distinct identities.
Actual start/resume uploads used the same one segment for two distinct user
turns. The compaction run also passed. No real upload key or hosted capture
service is used by these probes.

The context-switch variant calls an ordinary source-context tool and then
`set_context` in one code-mode wrapper. It checks destination attribution and
that combined output does not cross context boundaries. On 16 September 2026,
the updated version-2 fixtures passed on installed Codex 0.154.0 for fresh
capture, resume and code-mode context switching (all ten switch checks passed).
Claude Code 2.1.273 passed all eight capture checks and twelve grounding/receipt
checks, including resumed segment identity and compaction. These probes use
synthetic services and credentials; they do not prove live personal-settings
setup, account consent or production acceptance. On 17 September, the same
Claude suite and Codex code-mode switch checks passed again with one
account-scoped setup credential shared across clients.

Codex 0.154.0 persists visible user turns as `event_msg` / `item_completed`
records whose `item.type` is `UserMessage`; they carry thread and turn IDs and
precede accepted hook context. Generic role-user `response_item` records also
contain harness instructions and are not an accepted user source. Assistant
capture permits visible commentary/final channels and excludes analysis and
reasoning records. Capture requires a local `transcript_path`; `--persist` is
therefore required for these fixtures.

### Turn capture verification — 21 September 2026

The adapter candidate was exercised on **Codex CLI 0.155.1** and
**Claude Code 2.1.278** with local synthetic services. Package tests run on
Python 3.9 and 3.12 (168 passed on each). Native upload bodies were also validated against
Pensieve #973's actual `UploadBatch` schema.

The capture probes now assert native turn identity, ordered predecessor links,
tool-call/result identity and explicit completion, alongside the existing
privacy, attribution and exact retry checks. Codex covers fresh capture,
resume, fork, compaction, code-mode context switches and SIGINT during inference.
Claude covers startup, resume, compaction, clear, fork, a visible ordinary tool,
failed grounding and a stream-control interruption. Claude's report includes
synthetic upload batches and a content-free native lifecycle inventory.

Codex's fork fixture uses the source's exact ordinal, while a later resume keeps
the source branch independent. A unit fixture additionally forks before a
captured turn's terminal event, proving the suffix is excluded. Claude's fork
fixture verifies new work is captured once with unknown parentage; no source
conversation is inferred from matching text or UUIDs. Codex interruptions carry
`interrupted`; Claude's observed interruption has no trustworthy terminal
record and remains incomplete. Neither is marked completed.

The interruption variant uses the native interrupt mechanism while the local
model fixture waits. It intentionally omits the synthetic 503 on Codex so that
the best-effort exit flush can be inspected without another resumed process.
Codex exit code 1 is expected for that case; the probe exits zero only when its
interruption assertions pass. Other variants retain the exact-byte 503 retry.
Use a new source session captured by this candidate for `--fork`, sharing its
`--capture-state`; an older plugin's spool has no ordinal index.

### Delivery acknowledgement boundary

Accepted context is a top-level JSONL `response_item` whose payload is a
`message` with role `developer`. Its `content` holds `input_text`, and
`internal_chat_message_metadata_passthrough.content_item_kinds` identifies
`hooks.additional_context`. Hook execution notifications alone do not establish
that the context was accepted.

A top-level `compacted` record replaces history. Its `guardian_history` can
contain old hook text even when `replacement_history` has dropped that text;
receipt readers must respect this boundary and avoid recursively searching every
JSON string. Ephemeral or non-local sessions can have `transcript_path=null` and
must use the conservative delivery fallback.

Handlers for the same event run concurrently. An acknowledgement helper cannot
be assumed to finish before the MCP refresh hook, so a delayed acknowledgement
may cause one safe repeat. MCP replies do not themselves acknowledge injection.

## Limits

These probes establish CLI behaviour and read-only package discovery. They do
not install the package into a desktop client, exercise live Pensieve OAuth, or
prove multiple conversations multiplexed through one desktop MCP transport.
Those remain separate delivery checks. `plugin-creator`'s older manifest
validator rejects `hooks`; the installed CLI reader and matching source are the
authority for the format exercised here.

Source boundaries: [hook MCP executor](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/core/src/hook_mcp_executor.rs),
[plugin loader](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/core-plugins/src/loader.rs#L954),
[hook dispatch](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/hooks/src/engine/dispatcher.rs),
and [PostCompact schema](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/hooks/schema/generated/post-compact.command.output.schema.json).

## Live client acceptance

Synthetic probes establish local client behaviour. Before publishing the first
hooks release, record the candidate plugin commit, deployed backend commit,
client/version, operating system, install route and result for each tested host.
Use a local package or the candidate Git branch until these checks pass.

1. **Fresh install:** authenticate through the client's normal OAuth flow, enable
   hooks, select a company and confirm grounding arrives before the first answer.
2. **Existing install:** update the marketplace/plugin without uninstalling;
   confirm sign-in, selected company, skills, branding and hook permissions.
3. **Conversation isolation:** open two conversations on different companies,
   alternate reads, and confirm each keeps its own selection, including when the
   client shares one MCP connection. Start a third conversation and verify the
   latest explicit default selection.
4. **Lifecycle:** resume, compact and clear; verify context returns where needed
   and a changed company briefing refreshes before the next answer.
5. **Delivery failure:** interrupt a briefing request and confirm a later prompt
   retries without treating undelivered context as acknowledged.

Record results separately for CLI, desktop and workspace imports. A successful
marketplace import or a displayed icon does not prove hooks executed. Where a
host does not support hooks, verify MCP and skill use and keep hook support
marked unavailable. Desktop and live OAuth checks are currently pending.

The nested-call follow-up passed 173 package tests on Python 3.9 and 3.12.
The installed Codex code-mode probe also passed with one synthetic `edit_page`
on each side of `set_context`: both captured native IDs matched the IDs observed
by the local MCP server, exactly once and in the expected context. Arguments and
combined results remain omitted. This proves native correlation, not publication
or a live authenticated write to Pensieve.
