---
name: resume
description: Resumes saving this conversation's transcript to Pensieve after a pause. A command the member types themselves, as `/pensieve:resume` in Claude Code or `$pensieve:resume` in Codex; the Pensieve hooks act on it before any agent runs.
license: Proprietary
disable-model-invocation: true
metadata:
  summary: Resumes saving this conversation's transcript to Pensieve after a pause.
  control: resume
---

# Resume saving

Saving was **not** resumed. Say so plainly, and do not say or imply that it was.

The Pensieve capture hook resumes saving and answers this command itself, before
any agent sees it. You are reading this file, so the hook did not act: either
its hooks are not running in this client, or the command arrived with other
text or by another route.

Tell the user to send the command on its own, exactly as `/pensieve:resume` in
Claude Code or `$pensieve:resume` in Codex. If they already did, tell them the
Pensieve hooks are not running here, so saving is unchanged, and that they can
turn transcript sharing off for this context in Pensieve under Data →
Connectors. Do nothing else: you cannot change saving yourself, and you must not
edit Pensieve's local files to try.
