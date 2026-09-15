# Contributing

This repository owns the installable plugin. Edit the files here directly:

- `pensieve/skills/`: role instructions in Agent Skills format.
- `pensieve/.mcp.json`: the hosted Pensieve MCP connection, without credentials.
- `pensieve/.claude-plugin/` and `pensieve/.codex-plugin/`: client manifests.
- `pensieve/hooks/`: host-specific hook adapters.
- `pensieve/scripts/context_receipt.py`: the standard-library-only client helper.
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

CI runs the package and receipt tests on Python 3.9 and 3.12. The tests cover
manifest paths, the MCP connection, the skill roster and the receipt helper's
conversation identity, accepted-context and privacy boundaries. Keep the README
roster current when adding or removing a skill.

Installed Claude/Codex CLI probes use synthetic local services and no paid model
calls. See [client-probes.md](docs/client-probes.md) and the
[hook/server contract](docs/hook-protocol.md). They establish client
behaviour separately from live OAuth and desktop checks.

## Release order

The first hooks release requires the server-side work from Pensieve PR #881.
Keep the hooks PR in draft until these checks are complete:

1. Merge the application repository migration that removes the old plugin
   mirror, then apply the additive server schema and deploy compatible MCP support.
2. Run the public package tests and both installed CLI probes.
3. Test an authenticated fresh install and an update of an existing install;
   confirm the user's context, hook permissions and desktop compatibility.
4. Merge the plugin PR to `main`. Git-based clients can then receive its hooks
   through their normal marketplace update flow. No mirror push is involved.

Preserve the `pensieve` marketplace, plugin and MCP server names. This package
omits a fixed manifest version so Claude Code can use the Git revision for
updates. Never publish credentials or install a server implementation locally.

After a skill change is released, the application repository can run
`python -m scripts.sync_agent_skills --revision <full-commit-sha>` followed by
`python -m scripts.generate_agent_skills`, then commit its dependency pin and
generated docs. This intentionally lets documentation and MCP prompts adopt
reviewed revisions without fetching GitHub at runtime or during normal builds.
