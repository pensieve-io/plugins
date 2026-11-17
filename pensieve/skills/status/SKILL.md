---
name: status
description: Shows whether this conversation's transcript is being saved to Pensieve. A command the member types themselves, as `/pensieve:status` in Claude Code or `$pensieve:status` in Codex; the Pensieve hooks answer it before any agent runs.
license: Proprietary
disable-model-invocation: true
metadata:
  summary: Shows whether this conversation's transcript is being saved to Pensieve.
  control: status
---

# Saving status

The saving status was **not** read. Say so plainly, and do not say or guess
whether this conversation is being saved.

The Pensieve capture hook answers this command itself, before any agent sees
it. You are reading this file, so the hook did not act: either its hooks are
not running in this client, or the command arrived with other text or by
another route.

Tell the user to send the command on its own, exactly as `/pensieve:status` in
Claude Code or `$pensieve:status` in Codex. If they already did, tell them the
Pensieve hooks are not running here, so you cannot tell them whether this
conversation is saved, and that transcript sharing for this context is shown in
Pensieve under Data → Connectors. Do nothing else: you must not read or edit
Pensieve's local files to work it out.
