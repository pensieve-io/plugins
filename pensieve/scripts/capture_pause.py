"""The machine-wide saving pause, shared with the `pensieve` CLI.

`pensieve pause` writes `paused.json` beside the capture spools, holding
`{"paused_at": "<ISO-8601 UTC>"}`, and `pensieve resume` deletes it. While the
file exists, no client on this machine saves transcripts or anchors. Its
presence is what counts: a file that cannot be read or parsed still pauses
saving, because failing open here would upload what the member chose to keep
local. Standard library, Python 3.9+.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime
from pathlib import Path

PAUSE_FILE = "paused.json"
MAX_PAUSE_BYTES = 4096


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
