# Pensieve role skills

A skill is a team member. Each one describes a role — what that person holds in their head, how they judge, the shifts they run and when they write something back — and leaves the company itself to [Pensieve](https://pensieve.uk), the context layer curated from your company's own sources. Give the skill to an agent connected to your context layer and you have someone who reasons about your business the way a senior hire would after years inside it, from the first run.

This is a growing collection: new roles and shifts ship to this same plugin, and installed copies pick them up automatically. The current roster:

| Skill | Role | What it does |
| --- | --- | --- |
| `chief-of-staff` | Chief of Staff | Reads the whole company and tells the leadership team what changed, what needs deciding, and who owes what by when. |
| `head-of-product` | Head of Product | Turns customer evidence into the few things product should act on, and knows what has already been tried or declined. |
| `head-of-growth` | Head of Growth | Judges each channel on what it actually returned, and names the next mechanism worth trying rather than the next idea. |

## Install

In Claude Code, Claude Desktop or Claude Cowork:

```
/plugin marketplace add pensieve-io/skills
/plugin install pensieve@pensieve
```

The plugin carries every role skill and the connection to your context layer, so one install hires the whole team and gives it the company to work from. The first call signs you in to Pensieve; you need a [Pensieve workspace](https://pensieve.uk) with your company's sources connected.

Scheduled tasks inherit installed skills, so a role can run on a cadence — a Monday operating brief, a weekly customer-signal review — with the destination and schedule named in the task, never in the skill.

Other routes: `npx skills add pensieve-io/skills` installs the skill files alone into any harness that reads Agent Skills (checksums are published at [`/.well-known/agent-skills/`](https://pensieve.uk/.well-known/agent-skills/index.json)), and any MCP client connected to the [Pensieve server](https://docs.pensieve.uk/mcp-server/clients) is offered each skill as a prompt and a `skill://` resource.

## Docs

Each skill's page shows the file in full, with setup for the scheduled agent step by step:

- [How role skills work](https://docs.pensieve.uk/skills)
- [Hire a Chief of Staff](https://docs.pensieve.uk/use-cases/chief-of-staff)
- [Hire a Head of Product](https://docs.pensieve.uk/use-cases/head-of-product)
- [Hire a Head of Growth](https://docs.pensieve.uk/use-cases/head-of-growth)

## About this repository

This repository is a published mirror: the skills are authored in Pensieve's main repository and rendered here by its generator, so versions here track what every other install surface ships. Issues and requests are welcome; pull requests will be regenerated over.
