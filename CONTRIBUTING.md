# Contributing

This repository owns the installable plugin. Edit the files here directly:

- `pensieve/skills/`: role instructions in Agent Skills format.
- `pensieve/.mcp.json`: the hosted Pensieve MCP connection, without credentials.
- `pensieve/.claude-plugin/` and `pensieve/.codex-plugin/`: client manifests.
- `pensieve/assets/`: bundled Pensieve branding used by supported client fields.
- `docs/distribution.md`: canonical directory metadata, listing copy and submission checks.
- `pensieve/hooks/`: host-specific hook adapters.
- `pensieve/scripts/context_briefing.py`: authenticated command hook, MCP sign-in enrolment and Claude call binding.
- `pensieve/scripts/context_receipt.py`: bounded native delivery receipt recognition.
- `pensieve/scripts/conversation_capture.py`: file checkpoints and immutable upload outbox.
- `pensieve/scripts/capture_adapters.py` and `capture_state.py`: native parsing and shared capture phases.
- `pensieve/scripts/capture_protocol.py`: public compatibility guard before private requests. See
  [conversation-capture.md](docs/conversation-capture.md).
- `pensieve/scripts/capture_onboarding.py`: asks once, on an Approve/Deny page, whether to share transcripts; preferences are managed in Data → Connectors.
- `pensieve/scripts/capture_pairing.py`: private credential exchange completed by normal hooks; no manual setup command or setup skill is shipped.
- `tests/` and `scripts/probe_*`: package tests and synthetic client probes.

The application repository owns the hosted MCP implementation, authentication,
database and server-side briefing/delivery endpoints. It imports reviewed skills
at a full Git commit SHA with file hashes. Nothing in that repository generates
or publishes this plugin.

## Checks

The installed helper requires Python 3.9 or newer and has no package dependencies.
For development, use a virtual environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install 'pytest>=8,<9' 'PyYAML>=6,<7' ruff
python -m pytest
ruff check .
ruff format --check .
```

CI runs the package, receipt and capture tests on Python 3.9 and 3.12. The tests cover
manifest paths, the MCP connection, the skill roster and the receipt helper's
conversation identity, accepted-context and privacy boundaries. Capture tests
also cover account/company isolation, disabled intervals, private storage,
stable retry receipts, full-spool recovery, indefinite receipts and deletion tombstones. Keep the README
roster current when adding or removing a skill.

Use native host metadata. Codex's `interface` supplies artwork, descriptions,
publisher details and starter prompts; Claude's manifest uses its own supported
fields. Package tests verify that advertised artwork stays inside the plugin
and is a real PNG. The existing Claude-compatible marketplace is also accepted
by Codex and ChatGPT workspace import, so keep one catalogue.

Artwork uses `assets/branding/logos/logo-fill-grey-rounded.png` from the
application repository, copied unchanged to `pensieve/assets/icon.png`.
`composerIcon`, `logo` and `logoDark` all reference this one white mark on its
own grey background. Keep the background: some client surfaces, including
ChatGPT's plugin detail MCP row, use only `logo` even in dark mode and add no
background of their own. The same asset must remain legible in both themes.
The accent colour follows the product's light-mode `--primary` token. Update
the bundled copy deliberately; the plugin has no runtime dependency on the
application checkout.

Installed Claude/Codex CLI probes use synthetic local services and no paid model
calls. See [client-probes.md](docs/client-probes.md) and the
[hook/server contract](docs/hook-protocol.md). They establish client
behaviour separately from live OAuth and desktop checks.

## Release order

The script-briefing change replaces a native MCP hook with authenticated HTTP
endpoints and removes the MCP tool. Coordinate the backend and plugin releases:
the new plugin requires `/hooks/briefing`, `/hooks/tool-binding` and
`/hooks/enrolment`, with pairings registered by the signed-in MCP call (exchange
status `registered`); old installed hooks cannot keep calling a removed tool. Run fresh-install and existing-key upgrade acceptance before
publishing. Do not silently grant old upload-only keys read access. Transcript
sharing must remain optional throughout this upgrade.

Merging this change to `main` publishes the new hooks to Git marketplace installs
and updates. Keep the plugin PR unmerged until the compatible application
schema/API/MCP is deployed and fresh-install plus existing-device reconnection
acceptance has passed using the candidate branch. Record the deployed application
SHA and tested plugin SHA before merging.

The historical capture-only protocol guard allowed plugin #11 to merge before
application #996 deployed; it does not make script briefings safe to publish
before their endpoints exist. A new helper checks API and MCP capability manifests before sending
private pairing/upload requests and waits with exact queued work intact when the
contract is unavailable. Do not remove this guard, downgrade queued envelopes or
claim capture works merely because both repositories merged.

Release the complete app/API/MCP/worker stack and its canonical-body migration
using the application release ledger. Then run:

```sh
python3 scripts/check_capture_compatibility.py
```

The manual **Capture compatibility** workflow runs the same unauthenticated probe.
Both services must advertise protocol 1, supported clients and upload bounds. This
probe transfers no private data and checks a declared contract; follow it with
an authenticated fresh installation and existing-install update, MCP sign-in pairing,
real upload acknowledgement and member read. Package tests and synthetic native
probes exercise different boundaries and do not replace that acceptance.

Older installed helpers lack this guard. The coordinated retention/body migration
still holds ingress and drains old workers while preserving consent and queued
work. Toggling consent off/on rotates generation and is not a migration mechanism.

The first hooks release requires the server-side work from Pensieve PR #881.
Keep the hooks PR in draft until these checks are complete:

1. Merge the application repository migration that removes the old plugin
   mirror and the hosted support PR. Merging application code does not deploy it:
   stage and release compatible MCP support through the application's normal
   pipeline, including the additive schema migration.
2. Run the public package tests and both installed CLI probes.
3. Test an authenticated fresh install and an update of an existing install;
   use the candidate branch/local package before public publication. Confirm the
   user's context, hook permissions and desktop compatibility using the
   [live acceptance checks](docs/client-probes.md#live-client-acceptance).
4. Merge the plugin PR to `main`. Git-based clients can then receive its hooks
   through their normal marketplace update flow. No mirror push is involved.

Preserve the `pensieve` marketplace, plugin and MCP server names. This package
omits a fixed manifest version so Claude Code can use the Git revision for
updates. Never publish credentials or install a server implementation locally.

Public directory releases also follow [distribution.md](docs/distribution.md).
Keep Claude's connector and plugin identities aligned; OpenAI takes one combined
MCP-and-skills submission for ChatGPT and Codex. Directory imports must preserve
the required adapter/helper and pass native hook checks before claiming the same
automatic context behaviour as the Git package. OpenAI's imported skill snapshot
requires a new scan or upload for updates; a Git push does not update it live.

Local/synthetic package tests need no application release. Live tests against
`mcp.pensieve.uk` require the compatible backend to be released first. A staging
test can run earlier with a disposable package: change `.mcp.json` and the
helpers' `BRIEFING_ENDPOINT`, `BINDING_ENDPOINT`, `ENROLMENT_ENDPOINT` and
`DELIVERY_ENDPOINT` constants to the staging MCP, briefing, binding, enrolment and
receipt URLs.
For capture, also change `conversation_capture.UPLOAD_ENDPOINT` and
`capture_pairing.API_BASE` in that disposable copy, and point
`capture_onboarding.CONSENT_PAGE` at the staging app host. Sign in to the staging
MCP so its first Pensieve tool call registers a device key in an isolated private test config.
Do not point any part of this disposable setup at production; check every service
location before running the hooks.
Passing a staging URL to `--endpoint` alone is rejected; that override accepts
only the fixed service URL or HTTP loopback. Production endpoints in the
shipped package stay fixed.

After a skill change is released, the application repository can run
`python -m scripts.sync_agent_skills --revision <full-commit-sha>` followed by
`python -m scripts.generate_agent_skills`, then commit its dependency pin and
generated docs. This intentionally lets documentation and MCP prompts adopt
reviewed revisions without fetching GitHub at runtime or during normal builds.
The current hosted importer accepts only self-contained `SKILL.md` files and
rejects companion references, scripts or assets. Extend hosted import and
delivery before adopting an attachment-backed skill there; the plugin format
itself supports companion files.
