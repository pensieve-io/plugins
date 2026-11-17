"""The two ways a member pauses saving. Standard library, Python 3.9+.

**One conversation.** The plugin's `pause`, `resume` and `status` skills give
each host a command it completes: `/pensieve:pause` in Claude Code,
`$pensieve:pause` in Codex. `conversation_command` recognises one in a hook
payload; the capture hook then acts on it and answers in the model's place, so
no agent is involved.

**The whole machine.** `pensieve pause` writes `paused.json` beside the capture
spools, holding `{"paused_at": "<ISO-8601 UTC>"}`, and `pensieve resume`
deletes it. While the file exists, no client on this machine saves transcripts
or anchors. Its presence is what counts: a file that cannot be read or parsed
still pauses saving, because failing open here would upload what the member
chose to keep local.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime
from pathlib import Path

PAUSE_FILE = "paused.json"
MAX_PAUSE_BYTES = 4096
# Skill names as each host reports them, and the command each one is.
CONVERSATION_COMMANDS = {
    "pensieve:pause": "pause",
    "pensieve:resume": "resume",
    "pensieve:status": "status",
}


def conversation_command(payload: dict, client: str) -> str | None:
    """`pause`, `resume` or `status` for this conversation, None for any other hook.

    Claude Code names the plugin command a typed line expands to, so its bare
    `/pause` alias is recognised and another plugin's `/pause` is not. Codex has
    no such event: a skill mention arrives as the prompt's own text, and only a
    prompt that is nothing but the mention counts.
    """
    event = payload.get("hook_event_name")
    if client == "claude" and event == "UserPromptExpansion":
        name = payload.get("command_name")
    elif client == "codex" and event == "UserPromptSubmit":
        prompt = payload.get("prompt")
        prompt = prompt.strip() if isinstance(prompt, str) else ""
        name = prompt[1:] if prompt.startswith("$") else None
    else:
        return None
    return CONVERSATION_COMMANDS.get(name) if isinstance(name, str) else None


def recorded_time(value: object) -> str | None:
    """The CLI's `paused_at` when it is an aware ISO-8601 time, else None."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if parsed.tzinfo is not None else None


def pause_state(root: Path) -> dict | None:
    """None while saving runs; `{"paused_at": str | None}` while it is paused."""
    path = root / PAUSE_FILE
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return {"paused_at": None}  # Unreadable: paused, failing closed.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        with os.fdopen(os.open(path, flags), "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return {"paused_at": None}
            raw = handle.read(MAX_PAUSE_BYTES + 1)
        value = json.loads(raw) if len(raw) <= MAX_PAUSE_BYTES else None
    except (OSError, ValueError, RecursionError):
        return {"paused_at": None}
    return {"paused_at": recorded_time(value.get("paused_at") if isinstance(value, dict) else None)}


def is_paused(root: Path) -> bool:
    return pause_state(root) is not None
