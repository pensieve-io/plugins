# Pensieve Plugins

Give your AI a shared, cited understanding of your company. The Pensieve plugin
connects it to your company's context layer and bundles three things:

- **MCP tools** to read the company map, follow citations to sources and contribute
  knowledge through Pensieve.
- **Role skills** that give the agent a way to work as your Chief of Staff, Head
  of Product or Head of Growth.
- **Context hooks** that load company context automatically, refresh it as the
  company map changes and restore it when the client shortens conversation history.

One plugin installs the tools, skills and hooks together. Sign in to your
[Pensieve workspace](https://pensieve.uk) and choose the company context you
want the agent to use. Automatic context loading requires a client that runs
plugin hooks, such as Claude Code or Codex CLI.

## Role skills

Each skill defines a role's judgement and recurring tasks, while Pensieve
provides the company knowledge it works from.

| Skill | Role | What it does |
| --- | --- | --- |
| `chief-of-staff` | Chief of Staff | Reads the whole company and tells the leadership team what changed, what needs deciding, and who owes what by when. |
| `head-of-product` | Head of Product | Turns customer evidence into the few things product should act on, and knows what has already been tried or declined. |
| `head-of-growth` | Head of Growth | Judges each channel on what it actually returned, and names the next mechanism worth trying rather than the next idea. |

## Install

**Install this plugin and sign in; you do not need to add a standalone Pensieve
connector as well.** A connector on its own supplies MCP access without native
skills or hooks. In Claude, the directory connector and plugin can coexist
without duplicating tools when they use the same server URL. Other clients may
handle extra manual connections differently.

[Open Claude plugins](https://claude.ai/customize/plugins) ·
[Open ChatGPT plugins](https://chatgpt.com/plugins) ·
[Client setup guide](https://docs.pensieve.uk/mcp-server/clients)

These links open setup screens. Use the Git marketplace instructions below to
install this package; a link does not install or authenticate it automatically.

### Codex CLI

Run these commands in a terminal:

```sh
codex plugin marketplace add pensieve-io/plugins
codex plugin add pensieve@pensieve
codex mcp login pensieve
```

The first command registers this catalogue, the second installs the complete
plugin, and the third signs you in. Complete the browser sign-in, enable the
plugin's hooks if prompted, then start a new chat.

### Claude Code

Run these in your terminal:

```sh
claude plugin marketplace add pensieve-io/plugins
claude plugin install pensieve@pensieve
```

Open Claude Code, run `/mcp` to sign in to Pensieve, and enable the plugin's hooks.
Start a new conversation to use the installed plugin.

For both command-line clients, `python3` (3.9 or newer) must be available to the
client's command runner for the bundled hook helper.

### Desktop and workspace clients

**Claude web, Desktop and Cowork** (paid plans with plugins enabled):

1. Open [Claude plugins](https://claude.ai/customize/plugins). In Cowork, open
   the Cowork tab first, then **Customize → Plugins**.
2. Choose **+ → Add marketplace → Add from a repository**.
3. Paste `https://github.com/pensieve-io/plugins` and sync the marketplace.
4. Open the Pensieve marketplace, install **Pensieve**, sign in when prompted
   and start a new conversation.

**ChatGPT desktop / Codex desktop:** with the
[Codex CLI](https://learn.chatgpt.com/docs/codex/cli) installed on the same
computer, run the three Codex commands above. Restart the desktop app, open
**Plugins**, select the Pensieve marketplace and enable the installed plugin.
Review any hook permissions, then start a new chat in Work or Codex.

**Managed ChatGPT workspaces:** an admin opens **Admin → Plugins → Add → Import
marketplace**, enters `https://github.com/pensieve-io/plugins` as **Source**, and
leaves **Path** and **Branch** empty. After import, make Pensieve available to
the workspace. Members open **Plugins**, choose their workspace and install
Pensieve. Complete sign-in when prompted. A plugin marked **Desktop only** must
be installed and used in the desktop app.

These routes use our public GitHub marketplace. Add it first; publishing a
GitHub repository does not add a plugin to either client's public directory.
The catalogue and plugin are both named `pensieve`. A settings link opens the
host's setup screen; it does not install the plugin for you.

Claude lists its MCP connector and full plugin separately. OpenAI uses one
universal public plugin directory shared by ChatGPT and Codex; our public
submission should contain both the MCP connection and these skills. See
[directory distribution and branding](docs/distribution.md) for the canonical
listing fields and publishing process. Git publication and directory approval
are separate steps.

The components available depend on the client and workspace settings:

| Client | Installation and skills | Automatic context hooks | Verification |
| --- | --- | --- | --- |
| Codex CLI | Git marketplace; bundled MCP connection and skills | Supported by the packaged Codex adapter | Synthetic probes on 0.154.0; live install/update pending |
| Claude Code | Git marketplace; bundled MCP connection and skills | Supported by the packaged Claude adapter | Synthetic probes on 2.1.267; live install/update pending |
| Codex desktop app | Plugin marketplace; availability depends on account/workspace | Requires the app to run the packaged hooks and helper | Pending |
| ChatGPT Desktop / managed workspace | Workspace admins can import this GitHub marketplace; app access and authentication are separate | Package import alone does not establish hook execution | Pending |
| Claude Desktop Chat / web chat | Plugin skills and connectors where enabled | Claude documents hooks as unavailable in Chat | Pending |
| Claude Cowork | Custom Git marketplace or plugin upload; skills and connectors | Claude documents hook support; this adapter still needs a live check | Pending |
| Other MCP clients | Connect the hosted MCP service; install skills separately where supported | Requires a compatible hook runner | Client-specific |

Connecting only through MCP does not install skill files or lifecycle hooks.
ChatGPT imports that declare MCP servers can be marked **Desktop only**, even
when the server is remote. See [OpenAI's marketplace import guide](https://learn.chatgpt.com/docs/enterprise/plugin-management)
and [Claude's plugin support guide](https://support.claude.com/en/articles/13837440-use-plugins-in-claude)
for host capabilities. The table records package verification as of 15 September
2026; it does not imply production or desktop acceptance.

Follow the [client setup guide](https://docs.pensieve.uk/mcp-server/clients)
for MCP connections and standing instructions. Developers can run the
[client probes and live acceptance checks](docs/client-probes.md) before publication.

## Updates

New skills and hooks arrive as updates to the same `pensieve` plugin. Refresh
the marketplace and update the installed plugin, then start a new conversation
and enable any newly requested hooks. Uninstalling first is unnecessary.

In Claude Code, open `/plugin` → **Marketplaces** → **pensieve** →
**Enable auto-update** to receive updates automatically. Third-party
marketplaces have auto-update off by default. See
[Claude Code's update guide](https://code.claude.com/docs/en/discover-plugins#configure-auto-updates).

For a local Codex Git marketplace, refresh and install the latest bundle:

```sh
codex plugin marketplace upgrade pensieve
codex plugin add pensieve@pensieve
```

Desktop clients manage marketplace syncing separately. Use your client's
marketplace sync or plugin update controls; automatic updates depend on its
settings. Receiving the package also requires a host that supports its hooks.

## Automatic company context

Before the first response, the hooks load a briefing containing the selected
company's overview, top-level topics, available source trees and guidance for
further reading. The agent uses MCP tools to open the detail and its sources.

The plugin checks for an updated briefing before subsequent prompts and
restores it after the client compacts the conversation. Your last explicit
context choice is remembered for new conversations; each existing conversation
keeps its own selection.

The context hooks send delivery receipts for the briefing the client received.
Optional [work conversation capture](docs/conversation-capture.md) requires browser
approval for each installation and context. The installed plugin offers approval
on first use; **Clients → Your conversation sharing** can restart it. No setup
skill, credential download or terminal command is needed. New visible work
belongs to the selected context for team handoffs. Previous chats are not imported.

## Other ways to use the skills

Scheduled tasks inherit installed skills, so a role can run on a cadence — a Monday operating brief, a weekly customer-signal review — with the destination and schedule named in the task, never in the skill.

`npx skills add pensieve-io/plugins` installs the skill files alone into harnesses
that read Agent Skills. MCP clients connected to the
[Pensieve server](https://docs.pensieve.uk/mcp-server/clients) are also offered
each skill as a prompt and a `skill://` resource.

The [hosted skill index](https://pensieve.uk/.well-known/agent-skills/index.json)
publishes checksums for the application release's pinned skill snapshot. That
snapshot can lag this repository's latest revision; its checksums do not verify
an arbitrary latest Git install.

## Docs

Each skill's page shows the file in full, with setup for the scheduled agent step by step:

- [Build your AI team](https://docs.pensieve.uk/agents)
- [Directory distribution and shared branding](docs/distribution.md)
- [Hire a Chief of Staff](https://docs.pensieve.uk/agents/chief-of-staff)
- [Hire a Head of Product](https://docs.pensieve.uk/agents/head-of-product)
- [Hire a Head of Growth](https://docs.pensieve.uk/agents/head-of-growth)

## Contributing

This repository is the source of truth for the installable Pensieve plugin.
Edit skills, client manifests, MCP connection configuration, hooks and the local
helper here. Claude and Codex share the skills and helper, with separate
manifests and hook adapters where their clients require different formats.

Pensieve's hosted MCP server, authentication and company data remain in the
application repository. Its backend and docs import a pinned copy of the skills
from this repository; they do not publish or overwrite the plugin.

See [CONTRIBUTING.md](CONTRIBUTING.md) for checks, client probes and release order.

### Connect conversations

The plugin offers browser approval after its first authenticated native context
hook. Approve saving and company sharing for that installation and context; the
helper installs its upload key privately. Declining keeps MCP tools available
and does not trigger repeated prompts. Reconnect from **Clients → Your
conversation sharing** and continue in the agent with the same context selected.
Normal hooks complete pending setup and retry authorised uploads. V1 saves new
conversations only; the 90-day transcript lifetime is not a backfill window.
