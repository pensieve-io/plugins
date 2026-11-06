"""Per-client upload health beside the capture spools. Standard library only.

`health-<client>.json` records how the newest upload attempt ended, when one
last succeeded and how many attempts in a row have not. It holds outcome names
and times only: never transcript content, a key or a conversation identity.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from capture_config import MAX_CONFIG_BYTES, private_file, private_lock, save_private_json

FIELDS = ("last_attempt_at", "last_outcome", "last_success_at", "consecutive_failures")
# Local refusals stop a conversation without an upload; they say nothing about
# whether the service is reachable, so they leave the failure count alone.
LOCAL_OUTCOMES = {"unproven_start"}
LOCK_WAIT_SECONDS = 0.1


def health_path(root: Path, client: str) -> Path:
    if client not in {"claude", "codex"}:
        raise ValueError("Invalid capture client")
    return root / f"health-{client}.json"


def read_health(root: Path, client: str) -> dict | None:
    try:
        value = json.loads(private_file(health_path(root, client), MAX_CONFIG_BYTES))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError("Invalid capture health")
    return {field: value.get(field) for field in FIELDS}


def record_health(root: Path, client: str, outcome: str) -> None:
    """Best effort: a busy or damaged file never delays or fails an upload."""
    path = health_path(root, client)
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            with private_lock(path.with_suffix(".lock")):
                try:
                    previous = read_health(root, client) or {}
                except ValueError:
                    previous = {}
                now = datetime.now(timezone.utc).isoformat()
                failures = previous.get("consecutive_failures")
                failures = failures if type(failures) is int and failures >= 0 else 0
                if outcome == "uploaded":
                    failures = 0
                elif outcome not in LOCAL_OUTCOMES:
                    failures += 1
                save_private_json(
                    path,
                    {
                        "last_attempt_at": now,
                        "last_outcome": outcome,
                        "last_success_at": now
                        if outcome == "uploaded"
                        else previous.get("last_success_at"),
                        "consecutive_failures": failures,
                    },
                )
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return
            time.sleep(0.01)
        except (OSError, ValueError):
            return
