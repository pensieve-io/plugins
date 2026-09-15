# Pensieve plugin

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

```
/plugin marketplace add pensieve-io/plugins
/plugin install pensieve@pensieve
```

Complete the Pensieve sign-in when prompted and enable the plugin's hooks.
Start a new conversation to use the installed plugin.

For both command-line clients, `python3` (3.9 or newer) must be available to the
client's command runner for the bundled hook helper.

### Desktop and other clients

Where your client supports custom plugin marketplaces, add
`https://github.com/pensieve-io/plugins` and install **Pensieve**. The catalogue
and plugin are both named `pensieve`; the same bundle supplies the MCP
connection and role skills.

Automatic context loading also requires the host to run the plugin's hooks.
An ordinary ChatGPT or Claude chat connected through MCP does not gain those
hooks or install skill files. Follow the [client setup guide](https://docs.pensieve.uk/mcp-server/clients)
for MCP connections and standing instructions.

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

The hooks send delivery receipts for the briefing the client received.
Conversation text and local file contents are not uploaded.

## Other ways to use the skills

Scheduled tasks inherit installed skills, so a role can run on a cadence — a Monday operating brief, a weekly customer-signal review — with the destination and schedule named in the task, never in the skill.

Other routes: `npx skills add pensieve-io/plugins` installs the skill files alone into any harness that reads Agent Skills (checksums are published at [`/.well-known/agent-skills/`](https://pensieve.uk/.well-known/agent-skills/index.json)), and any MCP client connected to the [Pensieve server](https://docs.pensieve.uk/mcp-server/clients) is offered each skill as a prompt and a `skill://` resource.

## Docs

Each skill's page shows the file in full, with setup for the scheduled agent step by step:

- [Build your AI team](https://docs.pensieve.uk/agents)
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
