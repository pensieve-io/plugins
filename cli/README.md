# Pensieve CLI

`pensieve` sets Pensieve up on a machine and puts every Pensieve tool in a
shell. It installs the plugin into Claude Code and Codex CLI, signs in, and
then runs whatever the MCP server offers — search, read, save, edit — as
commands, for agents and scripts that have no MCP connection. It also pulls a
repository's Pensieve docs into local Markdown.

It needs Node 18 or newer and has no runtime dependencies.

```sh
npx @pensieve/cli <command>       # once published
node cli/bin/pensieve.js <command> # from this repository
```

## Set up a machine

```sh
pensieve setup [--claude] [--codex] [--no-plugin]
```

One command, safe to run again:

1. installs the plugin (MCP tools, hooks and skills) into each of Claude Code
   and Codex CLI found on `PATH`, as `install` below does;
2. signs the CLI in, opening your browser, unless it already is;
3. lists your contexts through the server, which proves the connection.

It ends by printing what only you can do (Claude Code's `/mcp` sign-in and
enabling hooks). `--no-plugin` skips step 1.

Chat and desktop apps (Claude, ChatGPT) have no shell and keep their
connectors in account settings, so the CLI can neither run in them nor set
them up: add Pensieve there as a custom MCP server, following the
[client setup guide](https://docs.pensieve.uk/mcp-server/clients).

## Install the plugin

```sh
pensieve install [--claude] [--codex] [--dry-run]
pensieve uninstall [--dry-run]
```

`install` looks for `claude` and `codex` on `PATH` (or uses only the clients you
name) and runs the commands from the [plugin README](../README.md#install),
streaming their output:

| Client | Commands |
| --- | --- |
| Claude Code | `claude plugin marketplace add pensieve-io/plugins`, `claude plugin install pensieve@pensieve` |
| Codex CLI | `codex plugin marketplace add pensieve-io/plugins`, `codex plugin add pensieve@pensieve`, `codex mcp login pensieve` |

A step whose effect already exists (the `pensieve` marketplace is configured,
`pensieve@pensieve` is installed, or Codex is already signed in to the
`pensieve` server) is skipped. A client that already has Pensieve from another
marketplace (`pensieve@synced`, say) is left alone entirely, since a second
copy would run every hook twice. Everything the CLI did run is
written to `~/.pensieve/install-receipt.json` with the CLI version and times.
Claude Code still needs one manual step: open it, run `/mcp`, choose
`plugin:pensieve:pensieve` and authenticate, then enable the plugin's hooks.

`uninstall` reads the receipt and runs the inverse of each recorded step,
newest first — `claude plugin uninstall pensieve@pensieve`,
`claude plugin marketplace remove pensieve`, `codex mcp logout pensieve`,
`codex plugin remove pensieve@pensieve`, `codex plugin marketplace remove pensieve`
— then deletes the receipt. It never removes anything the receipt does not
list; a step that fails stays in the receipt for a retry. These inverse
commands come from each client's own `--help`; the plugin README does not
document uninstalling.

## Sign in

```sh
pensieve login [--token-stdin] [--reregister]
pensieve logout
```

`login` signs the CLI in to Pensieve with OAuth 2.1 (authorisation code with
PKCE, S256) as a public client. It discovers the authorisation server from the
MCP server's metadata, registers a client dynamically (cached per issuer in
`~/.pensieve/oauth-clients.json`), listens once on `127.0.0.1` for the
redirect, opens your browser and prints the URL, checks `state`, and stores
the tokens in `~/.pensieve/credentials.json` (directory 0700, file 0600).
Expired access tokens refresh automatically.

Without a browser, pipe an access token in: `pensieve login --token-stdin < token.txt`.
It is used until it expires and is not refreshed.

This sign-in belongs to the CLI only. Claude Code and Codex keep their own
plugin sign-in, and `logout` does not touch it.

| Variable | Effect |
| --- | --- |
| `PENSIEVE_MCP_URL` | MCP server (default `https://mcp.pensieve.uk/mcp`), for development |
| `PENSIEVE_CONTEXT_ID` | Default context for every tool command |
| `PENSIEVE_OAUTH_SCOPE` | Scopes to request (default: `openid email profile offline_access`, where advertised) |
| `PENSIEVE_OAUTH_RESOURCE=1` | Send the RFC 8707 `resource` parameter (off by default) |
| `PENSIEVE_CONFIG_DIR` | Where credentials and the receipt live (default `~/.pensieve`) |

## Go off the record

```sh
pensieve pause
pensieve resume
```

`pause` is a machine-wide switch for the plugin's transcript saving. While
paused, the Pensieve plugin saves no transcripts and records no commit or PR
anchors from any Claude Code or Codex conversation on this machine, and nothing
from the paused period is uploaded after resuming. Company briefings and MCP
tools keep working.

It writes `{"paused_at": "<ISO-8601 UTC>"}` atomically to `paused.json` in the
plugin's capture state directory (`PENSIEVE_CAPTURE_STATE`, else
`~/.local/state/pensieve/capture`), as a 0600 file in a 0700 directory.
Pausing again reports the original time. `resume` deletes the file, and says
so when there was nothing to resume. Any file there counts as paused, even one
that cannot be read, matching the hooks' fail-closed reading; `doctor` shows
which. The switch takes effect with plugin versions whose hooks read the file.

## Use Pensieve's tools

```sh
pensieve tools [--json]
pensieve <tool> [options] [--json]
pensieve <tool> --help
```

Every tool the MCP server offers your account is a command of the same name,
with hyphens for underscores: `pensieve save-data`, `pensieve edit-page`,
`pensieve search-changes`. The CLI holds no list of them. It reads the
server's own tool schemas on each call, so it matches the MCP tools exactly,
reads and writes alike, and a tool the server adds works here the same day.
Calls run as you, under the same permissions as over MCP.

```sh
pensieve save-data --content "Acme renewed for two years on 4 November." --context 578
pensieve edit-page --help
pensieve create-page --title "Renewals" --purpose-file purpose.md --context 578
git diff | pensieve save-data --content-file - --context 578
```

| Form | Meaning |
| --- | --- |
| `--top-k 3` | An option from the tool's schema, typed as the schema says |
| `--context 578` | Shorthand for `--context-id`; `PENSIEVE_CONTEXT_ID` is the default |
| `--include-evidence` | A boolean option (`--include-evidence=false` to turn one off) |
| `--trees a --trees b` | An array option, repeated, or one JSON array |
| `--body-file page.md` | Any option's value read from a file; `-` reads stdin |
| `--args '{"k": "v"}'` | A JSON object of arguments |
| one bare word or phrase | The tool's only required option |

Each call is independent, as over MCP: nothing remembers a context between
commands, so two agents on one machine can never redirect each other.

### Shorthand for reading

```sh
pensieve contexts [--json]
pensieve search <query> [--context N] [--tree SELECTOR] [--top-k N] [--json]
pensieve read <ref-or-url> [--context N] [--around N] [--tree SELECTOR] [--cursor C] [--json]
pensieve tree [--context N] [--tree SELECTOR] [--parent REF] [--depth N] [--json]
pensieve tree --list [--context N] [--json]
```

Friendlier spellings of `list_contexts`, `search`, `read`, `get_tree` and
`list_trees`. Each prints the tool's text; `--json` prints the raw MCP result.
`--context` may be omitted when you belong to one context. A long `read` ends
with the cursor that continues it; pass it back with `--cursor`.

## Prime an agent

```sh
pensieve prime [--write]
```

Claude Code and Codex with the plugin are primed by its hooks. Any other
harness that reads `AGENTS.md` or `CLAUDE.md` and can run a shell needs to be
told Pensieve exists. `prime` prints those standing instructions; `--write`
puts them between `<!-- pensieve:start -->` and `<!-- pensieve:end -->` in
`AGENTS.md` and `CLAUDE.md` where they exist at the repository root, creating
`AGENTS.md` when neither does, and refreshes the block in place on later runs.

The same file is read by sessions with the MCP tools and sessions without, so
the instructions name both routes and let the agent choose: the MCP tools when
the session has them, the CLI only when it has none.

## Pull a repository's docs

```sh
pensieve docs pull [--context N] [--tree SELECTOR] [--out DIR] [--pointer] [--max-pages N]
```

Inside a git repository, `docs pull` reads `origin`, normalises it to
`owner/repo`, and picks the Pensieve repository tree whose selector starts
with `owner/repo@` (preferring the current branch when several match; use
`--tree` to choose). It outlines the tree, reads every Page (four at a time,
up to `--max-pages`, default 500) and writes Markdown mirroring the
hierarchy, plus `index.md`.

- Output goes to `.pensieve/docs/` at the repository root by default, which is
  added to `.gitignore` unless git already ignores it.
- File names are slugs of the Page names, unique per directory, so a Page
  title can never write outside the output directory.
- A re-pull replaces the previous one. A directory whose `index.md` the CLI
  did not write is never cleared.
- `index.md` records the revision Pensieve last processed and your local
  `HEAD`. When they differ, the pull warns: the docs may lag the code.
- `--pointer` adds a short note, between `<!-- pensieve-docs:start -->` and
  `<!-- pensieve-docs:end -->` markers, to `AGENTS.md` and `CLAUDE.md` where
  they exist, telling agents where the docs are and that they may lag. Running
  it again refreshes the note in place.

This is a local copy for agents to read offline. It is not the GitHub mirror.

## Check the setup

```sh
pensieve doctor
```

Reports the CLI and Node versions; for each client, whether it is on `PATH`,
whether a `pensieve@…` plugin is installed (`claude plugin list --json`,
`codex plugin list`), what this CLI installed, and the plugin's capture health
file (`health-<client>.json` in `~/.local/state/pensieve/capture`, or
`PENSIEVE_CAPTURE_STATE`) when the installed plugin writes one; whether
transcript saving is paused, and since when; then the CLI sign-in and its
expiry, and whether the MCP server answers `/health`.

## Development

```sh
node --test cli/test   # or: cd cli && npm test
```

Tests stub `fetch` and `spawn`; none touch the network. Node 21 and newer no
longer expand a directory passed to `node --test`, so `cli/test/package.json`
points that path at `cli/test-all.js`, which loads every suite.
