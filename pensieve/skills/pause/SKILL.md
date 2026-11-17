---
name: pause
description: Pauses saving this conversation's transcript to Pensieve, from this point on. A command the member types themselves, as `/pensieve:pause` in Claude Code or `$pensieve:pause` in Codex; the Pensieve hooks act on it before any agent runs.
license: Proprietary
disable-model-invocation: true
metadata:
  summary: Pauses saving this conversation's transcript to Pensieve, from this point on.
  control: pause
---

# Pause saving

Saving was **not** paused. Say so plainly, and do not say or imply that it was.

The Pensieve capture hook pauses saving and answers this command itself, before
any agent sees it. You are reading this file, so the hook did not act: either
its hooks are not running in this client, or the command arrived with other
text or by another route.

Tell the user to send the command on its own, exactly as `/pensieve:pause` in
Claude Code or `$pensieve:pause` in Codex. If they already did, tell them the
Pensieve hooks are not running here, so saving is unchanged, and that they can
turn transcript sharing off for this context in Pensieve under Data →
Connectors. Do nothing else: you cannot change saving yourself, and you must not
edit Pensieve's local files to try.
