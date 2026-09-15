# Codex plugin hook probes

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

## Verified on 15 September 2026

Installed `codex-cli 0.154.0`; matching upstream tag `rust-v0.154.0`, commit
`6b9826e3aa83b1a5947db50f4332cb9c65f1b340`.

| Boundary | Observed result |
| --- | --- |
| Startup | The first model request contains the bundled helper's local primer and the UserPromptSubmit MCP briefing. No startup MCP briefing runs. The deliberately different `structuredContent` marker is absent. |
| Ordinary tool identity | The hook and ordinary MCP call both received `_meta.threadId`, matching Codex's durable thread identifier. They used the same MCP process. |
| Resume | A new MCP process receives the original thread identifier, with the local primer and first-prompt briefing restored. |
| Compaction | SessionStart with `source=compact` supplies the briefing before the next model request. |
| Generated plugin | The real plugin reader found five hooks, the `pensieve` MCP server and all three skills using `.codex-plugin/plugin.json`. |
| Portable root manifest | Adding a valid root Agent Plugins manifest suppressed hook discovery. The portable MCP configuration still loaded; this is a hook limitation. |

The runtime fixture consumes the packaged `hooks/codex.json` and bundled
receipt helper. It substitutes only the temporary plugin path and loopback
receipt endpoint; the synthetic MCP server retains the packaged server/tool
names. Matchers, inputs, timeouts and command syntax stay unchanged. The separate
manifest probe verifies discovery; the runtime probe executes the adapters.
The process deadline allows Codex's two 45-second shutdown waits after a
completed turn; each packaged hook keeps its five-second deadline. A timeout
preserves `events.jsonl` and `stderr.txt` for distinguishing delivery from exit.

## Delivery acknowledgement boundary

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
and [PostCompact schema](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/hooks/schema/packaged/post-compact.command.output.schema.json).
