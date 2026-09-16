# Contributing

This repository owns the installable plugin. Edit the files here directly:

- `pensieve/skills/`: role instructions in Agent Skills format.
- `pensieve/.mcp.json`: the hosted Pensieve MCP connection, without credentials.
- `pensieve/.claude-plugin/` and `pensieve/.codex-plugin/`: client manifests.
- `pensieve/assets/`: bundled Pensieve branding used by supported client fields.
- `pensieve/hooks/`: host-specific hook adapters.
- `pensieve/scripts/context_receipt.py`: the standard-library-only client helper.
- `pensieve/scripts/conversation_capture.py`: opt-in visible-conversation capture,
  private retry state and separately authorized uploads. See
  [conversation-capture.md](docs/conversation-capture.md).
- `pensieve/scripts/capture_setup.py`: private import of the account-scoped setup file from personal settings.
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
stable retry receipts, full-spool recovery and expiry tombstones. Keep the README
roster current when adding or removing a skill.

Use native host metadata. Codex's `interface` supplies artwork, descriptions,
publisher details and starter prompts; Claude's manifest uses its own supported
fields. Package tests verify that advertised artwork stays inside the plugin
and is a real PNG. The existing Claude-compatible marketplace is also accepted
by Codex and ChatGPT workspace import, so keep one catalogue.

Artwork is copied from Pensieve's existing brand files: `icon.png` from
`frontend/apps/web/public/icon-512.png`, `logo.png` from
`assets/branding/logos/logo-black.png`, and `logo-dark.png` from
`assets/branding/logos/logo-white.png`. The accent colour follows the product's
light-mode `--primary` token. Update the bundled copies deliberately when the
brand changes; the plugin has no runtime dependency on the application checkout.

Installed Claude/Codex CLI probes use synthetic local services and no paid model
calls. See [client-probes.md](docs/client-probes.md) and the
[hook/server contract](docs/hook-protocol.md). They establish client
behaviour separately from live OAuth and desktop checks.

## Release order

Conversation capture requires standalone Pensieve PR #886, including the upload
route, consent generations, personal settings and account-scoped device keys.
It has no transcript retrieval or task-ledger dependency.
Keep the capture PR in draft until that compatible service is deployed and
live client acceptance is recorded. Installing this candidate remains capture-off
without both server-side opt-in and a private account credential.

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

Local/synthetic package tests need no application release. Live tests against
`mcp.pensieve.uk` require the compatible backend to be released first. A staging
test can run earlier with a disposable package: change `.mcp.json` and the
helper's `DELIVERY_ENDPOINT` constant to the staging MCP and receipt URLs.
For capture, also change `UPLOAD_ENDPOINT` in the disposable helper and use a
staging-issued upload-only key kept in its private test config.
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
